from __future__ import annotations

import hashlib
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bfr.codec import decode_data, encode_data, run_codec
from bfr.client import (
    QuorumError, _checkpoint_path, _deliver, _history_checkpoint, _history_votes,
    _pending_path, read, repair, validate_share_response,
)
from bfr.model import (
    InvalidRecord, b64, block_message, checkpoint_relation, owner_id, public_bytes, sign_update,
    update_hash, validate_history,
)


def signed_file(key: Ed25519PrivateKey, version: int = 1, parent: str | None = None) -> dict:
    owner_pub = public_bytes(key)
    owner = owner_id(owner_pub)
    block = b"block"
    digest = hashlib.sha256(block).hexdigest()
    descriptors = []
    for share_id in range(4):
        signature = key.sign(block_message(owner, "sample", version, 0, share_id, digest))
        descriptors.append({"share_id": share_id, "sha256": digest, "signature": b64(signature)})
    body = {
        "public_key": owner_pub.hex(), "owner": owner, "file": "sample",
        "version": version, "parent": parent, "action": "put", "size": 5,
        "stripes": [{"length": 5, "blocks": descriptors}],
    }
    return sign_update(body, key)


class ProtocolTests(unittest.TestCase):
    def test_read_reuses_full_responses_but_checks_each_stripe(self):
        key = Ed25519PrivateKey.generate()
        body = copy.deepcopy(signed_file(key)["body"])
        body["size"] = 10
        second = copy.deepcopy(body["stripes"][0])
        for descriptor in second["blocks"]:
            descriptor["signature"] = b64(key.sign(block_message(
                body["owner"], "sample", 1, 1, descriptor["share_id"], descriptor["sha256"],
            )))
        body["stripes"].append(second)
        signed = sign_update(body, key)
        responses = [{"update": signed, "share_id": i, "blocks": [b64(b"block"), b64(b"block")]} for i in range(4)]
        requests = []

        def request(url, path):
            requests.append(path)
            return {"history": [signed]} if path.startswith("/v1/meta?") else responses[int(url)]

        def decoded(awaitable):
            awaitable.close()
            return b"blockblock"

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            trusted = state / "trusted" / (body["owner"] + ".pub")
            trusted.parent.mkdir(parents=True)
            trusted.write_text(public_bytes(key).hex(), encoding="ascii")
            with mock.patch("bfr.client._request", side_effect=request), mock.patch("bfr.client.run_codec", side_effect=decoded) as decode, mock.patch("bfr.client.validate_share_response", wraps=validate_share_response) as validate:
                for reads in (1, 2):
                    data, result = read(["0", "1", "2", "3"], state, "sample", body["owner"])
                    self.assertEqual(data, b"blockblock")
                    self.assertEqual(result["version"], 1)
                    self.assertEqual(sum(path.startswith("/v1/block?") for path in requests), 4 * reads)
                    self.assertEqual(sum(path.startswith("/v1/meta?") for path in requests), 4 * reads)
                    self.assertEqual(validate.call_count, 8 * reads)
                for response in responses[:3]:
                    response["blocks"][1] = b64(b"tampered")
                with self.assertRaisesRegex(QuorumError, "stripe 1 needs 2"):
                    read(["0", "1", "2", "3"], state, "sample", body["owner"])
                self.assertEqual(decode.call_count, 2)
                self.assertEqual(validate.call_count, 24)

    def test_checkpoint_updates_do_not_rollback_under_concurrent_writers(self):
        key = Ed25519PrivateKey.generate()
        first = signed_file(key)
        second = signed_file(key, 2, update_hash(first))
        owner = first["body"]["owner"]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            _history_checkpoint(state, owner, "sample", [first])
            checkpoint_path = state / "checkpoints" / owner / "sample.json"
            older_at_atomic = threading.Event()
            allow_older_write = threading.Event()
            newer_at_atomic = threading.Event()
            real_atomic = __import__("bfr.client", fromlist=["_atomic"])._atomic
            failures = []

            def ordered_atomic(path, data):
                if path == checkpoint_path:
                    version = json.loads(data)["version"]
                    if version == 1:
                        older_at_atomic.set()
                        if not allow_older_write.wait(timeout=3):
                            raise TimeoutError("older checkpoint write was not released")
                    elif version == 2:
                        newer_at_atomic.set()
                    real_atomic(path, data)
                    return
                real_atomic(path, data)

            def update(history):
                try:
                    _history_checkpoint(state, owner, "sample", history)
                except BaseException as error:
                    failures.append(error)

            with mock.patch("bfr.client._atomic", side_effect=ordered_atomic):
                older = threading.Thread(target=update, args=([first],))
                older.start()
                self.assertTrue(older_at_atomic.wait(timeout=3))
                newer = threading.Thread(target=update, args=([first, second],))
                newer.start()
                # A separate checkpoint update must not read the old file and
                # queue its replacement while the older operation is active.
                overtook_older = newer_at_atomic.wait(timeout=0.1)
                allow_older_write.set()
                older.join(timeout=5)
                newer.join(timeout=5)
                self.assertFalse(overtook_older)
            self.assertFalse(older.is_alive() or newer.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(json.loads(checkpoint_path.read_text(encoding="utf-8"))["version"], 2)

    def test_checkpoint_lock_prevents_cross_process_rollback(self):
        key = Ed25519PrivateKey.generate()
        first = signed_file(key)
        second = signed_file(key, 2, update_hash(first))
        owner = first["body"]["owner"]
        worker = r'''
import json, sys, time
from pathlib import Path
import bfr.client as client
state, owner, history_path = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
role, entered, gate, started = sys.argv[4], Path(sys.argv[5]), Path(sys.argv[6]), Path(sys.argv[7])
history = json.loads(history_path.read_text(encoding="utf-8"))
checkpoint_path = client._checkpoint_path(state, owner, "sample")
real_atomic = client._atomic
def ordered_atomic(path, data):
    if path == checkpoint_path and json.loads(data)["version"] == (1 if role == "old" else 2):
        entered.write_text("entered", encoding="ascii")
        if role == "old":
            deadline = time.monotonic() + 8
            while not gate.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("parent did not release old checkpoint")
                time.sleep(0.005)
    return real_atomic(path, data)
client._atomic = ordered_atomic
started.write_text("started", encoding="ascii")
client._history_checkpoint(state, owner, "sample", history)
'''
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            files = Path(directory) / "signals"
            files.mkdir()
            _history_checkpoint(state, owner, "sample", [first])
            old_history = files / "old.json"
            new_history = files / "new.json"
            old_history.write_text(json.dumps([first]), encoding="utf-8")
            new_history.write_text(json.dumps([first, second]), encoding="utf-8")
            old_entered, new_entered = files / "old-entered", files / "new-entered"
            old_started, new_started = files / "old-started", files / "new-started"
            gate = files / "release-old"
            old = new = None

            def wait_for(path, timeout=4):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if path.exists():
                        return True
                    time.sleep(0.005)
                return path.exists()

            def spawn(role, history, entered, started):
                return subprocess.Popen(
                    [sys.executable, "-c", worker, str(state), owner, str(history), role,
                     str(entered), str(gate), str(started)],
                    cwd=root, env=os.environ.copy(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )

            try:
                old = spawn("old", old_history, old_entered, old_started)
                self.assertTrue(wait_for(old_entered))
                new = spawn("new", new_history, new_entered, new_started)
                self.assertTrue(wait_for(new_started))
                overtook_older = wait_for(new_entered, timeout=0.25)
                gate.write_text("release", encoding="ascii")
                old_stdout, old_stderr = old.communicate(timeout=8)
                new_stdout, new_stderr = new.communicate(timeout=8)
                self.assertEqual(old.returncode, 0, old_stdout + old_stderr)
                self.assertEqual(new.returncode, 0, new_stdout + new_stderr)
                self.assertFalse(overtook_older)
                self.assertEqual(json.loads(_checkpoint_path(state, owner, "sample").read_text(encoding="utf-8"))["version"], 2)
            finally:
                gate.touch(exist_ok=True)
                for process in (old, new):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)

    def test_latest_head_wins_when_replica_answer_are_split_across_versions(self):
        key = Ed25519PrivateKey.generate()
        old = signed_file(key)
        latest = signed_file(key, 2, update_hash(old))
        responses = iter([
            {"history": [old, latest]}, {"history": [old, latest]},
            {"history": [old]}, {"history": [old]},
        ])
        with mock.patch("bfr.client._request", side_effect=lambda *_args, **_kwargs: next(responses)):
            self.assertEqual(_history_votes(["a", "b", "c", "d"], old["body"]["owner"], "sample"), [old, latest])

    def test_two_distinct_witnessed_heads_at_same_version_are_rejected(self):
        key = Ed25519PrivateKey.generate()
        left = signed_file(key)
        left_body = copy.deepcopy(left["body"])
        descriptor = left_body["stripes"][0]["blocks"][0]
        descriptor["sha256"] = hashlib.sha256(b"different signed block").hexdigest()
        descriptor["signature"] = b64(key.sign(block_message(
            left_body["owner"], left_body["file"], 1, 0, 0, descriptor["sha256"],
        )))
        right = sign_update(left_body, key)
        answers = iter([{"history": [left]}, {"history": [left]}, {"history": [right]}, {"history": [right]}])
        with mock.patch("bfr.client._request", side_effect=lambda *_args, **_kwargs: next(answers)):
            with self.assertRaisesRegex(QuorumError, "divergent heads"):
                _history_votes(["a", "b", "c", "d"], left["body"]["owner"], "sample")

    def test_checkpoint_failure_keeps_pending_operation_for_retry(self):
        key = Ed25519PrivateKey.generate()
        update = signed_file(key)
        owner = update["body"]["owner"]
        pending = {"update": update, "history": [update], "blocks": [["x"] for _ in range(4)]}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            pending_path = _pending_path(state, owner, "sample")
            pending_path.parent.mkdir(parents=True)
            pending_path.write_text("pending", encoding="utf-8")
            with mock.patch("bfr.client._request", side_effect=lambda url, *_args, **_kwargs: {"stored": True, "share_id": int(url)}), mock.patch("bfr.client._history_checkpoint", side_effect=OSError("injected checkpoint failure")):
                with self.assertRaisesRegex(OSError, "checkpoint failure"):
                    _deliver(["0", "1", "2", "3"], state, pending)
            self.assertTrue(pending_path.exists())

    def test_missing_metadata_cannot_erase_a_saved_reader_checkpoint(self):
        key = Ed25519PrivateKey.generate()
        update = signed_file(key)
        owner = update["body"]["owner"]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            trusted = state / "trusted" / f"{owner}.pub"
            trusted.parent.mkdir(parents=True)
            trusted.write_text(public_bytes(key).hex(), encoding="ascii")
            checkpoint = {
                "owner": owner, "file": "sample", "version": 1,
                "head": update_hash(update), "chain": [update_hash(update)],
                "history": [update],
            }
            path = state / "checkpoints" / owner / "sample.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with mock.patch("bfr.client._history_votes", return_value=[]):
                with self.assertRaisesRegex(InvalidRecord, "no longer provide"):
                    read(["a", "b", "c", "d"], state, "sample", owner)

    def test_repair_rejects_history_behind_the_saved_witness_before_rpc(self):
        key = Ed25519PrivateKey.generate()
        first = signed_file(key)
        second = signed_file(key, 2, update_hash(first))
        owner = first["body"]["owner"]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            trusted = state / "trusted" / f"{owner}.pub"
            trusted.parent.mkdir(parents=True)
            trusted.write_text(public_bytes(key).hex(), encoding="ascii")
            _history_checkpoint(state, owner, "sample", [first, second])
            with mock.patch("bfr.client._history_votes", return_value=[first]), mock.patch("bfr.client._request") as request:
                with self.assertRaisesRegex(InvalidRecord, "does not extend"):
                    repair(["a", "b", "c", "d"], state, "sample", 0, owner)
                request.assert_not_called()

    def test_owner_signed_history_and_tampering(self):
        key = Ed25519PrivateKey.generate()
        first = signed_file(key)
        second = signed_file(key, 2, update_hash(first))
        self.assertEqual(validate_history([first, second]), update_hash(second))
        changed = {"body": {**first["body"], "size": 6}, "signature": first["signature"]}
        with self.assertRaisesRegex(InvalidRecord, "signature"):
            validate_history([changed])

    def test_key_substitution_duplicate_share_id_and_signature_are_rejected(self):
        key = Ed25519PrivateKey.generate()
        update = signed_file(key)
        substituted = {"body": {**update["body"], "public_key": public_bytes(Ed25519PrivateKey.generate()).hex()}, "signature": update["signature"]}
        with self.assertRaises(InvalidRecord):
            validate_history([substituted])
        duplicate = {"body": {**update["body"], "stripes": [{"length": 5, "blocks": update["body"]["stripes"][0]["blocks"][:3] + [update["body"]["stripes"][0]["blocks"][0]]}]}, "signature": update["signature"]}
        with self.assertRaises(InvalidRecord):
            validate_history([duplicate])

    def test_replayed_share_and_divergent_checkpoints_are_rejected_or_reported(self):
        key = Ed25519PrivateKey.generate()
        first = signed_file(key)
        second = signed_file(key, 2, update_hash(first))
        old_response = {"update": first, "share_id": 0, "blocks": [b64(b"block")]}
        with self.assertRaisesRegex(InvalidRecord, "another version"):
            validate_share_response(old_response, second, 0, public_bytes(key), 0)
        left = {"owner": first["body"]["owner"], "file": "sample", "version": 1, "head": "1" * 64, "chain": ["1" * 64]}
        right = {"owner": first["body"]["owner"], "file": "sample", "version": 1, "head": "2" * 64, "chain": ["2" * 64]}
        self.assertEqual(checkpoint_relation(left, right), "divergent")

    def test_tahoe_codec_encodes_and_decodes_non_aligned_input(self):
        async def exercise():
            for data in (b"x", b"odd-sized stripe" * 13, bytes(range(256)) * 512):
                shares, _size = await encode_data(data)
                recovered = await decode_data([shares[0], shares[2]], [0, 2], len(data))
                self.assertEqual(recovered, data)

        run_codec(exercise())


if __name__ == "__main__":
    unittest.main()
