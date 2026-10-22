"""Trusted single-owner client for the bounded four-server experiment."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import ERASURE_THRESHOLD, METADATA_RESPONSES, METADATA_VOTES, SERVER_COUNT, WRITE_ACKS
from .codec import decode_data, encode_data, run_codec
from .model import (
    InvalidRecord, b64, block_message, checkpoint_relation, key_bytes, load_public,
    owner_id, public_bytes, sign_update, unb64, update_hash, validate_checkpoint,
    validate_file_id, validate_history, validate_update,
)

MAX_FILE = 1024 * 1024
MAX_HTTP = 8 * 1024 * 1024


class QuorumError(RuntimeError):
    pass


class UncertainCommit(RuntimeError):
    pass


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".bfr-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if os.name == "posix":
                os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def initialize(state: Path) -> str:
    key_path = state / "owner.key"
    if key_path.exists():
        raise ValueError("owner key already exists")
    key = Ed25519PrivateKey.generate()
    pub = public_bytes(key)
    oid = owner_id(pub)
    _atomic(key_path, key_bytes(key))
    _atomic(state / "owner.pub", (pub.hex() + "\n").encode("ascii"))
    return oid


def trust_owner(state: Path, pub_path: Path) -> str:
    try:
        raw = bytes.fromhex(pub_path.read_text(encoding="ascii").strip())
    except (ValueError, OSError) as error:
        raise ValueError("owner public key must be a 32-byte hexadecimal file") from error
    if len(raw) != 32:
        raise ValueError("owner public key must be 32 bytes")
    oid = owner_id(raw)
    _atomic(state / "trusted" / (oid + ".pub"), (raw.hex() + "\n").encode("ascii"))
    return oid


def _private(state: Path) -> Ed25519PrivateKey:
    raw = (state / "owner.key").read_bytes()
    if len(raw) != 32:
        raise ValueError("owner private key is malformed")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _owner_public(state: Path, owner: str | None) -> tuple[str, bytes]:
    if owner is None:
        pub = bytes.fromhex((state / "owner.pub").read_text(encoding="ascii").strip())
        return owner_id(pub), pub
    if not isinstance(owner, str) or len(owner) != 64 or any(c not in "0123456789abcdef" for c in owner):
        raise ValueError("owner identifier must be 64 lowercase hexadecimal characters")
    path = state / "trusted" / (owner + ".pub")
    if not path.exists():
        own = state / "owner.pub"
        if own.exists():
            candidate = bytes.fromhex(own.read_text(encoding="ascii").strip())
            if owner_id(candidate) == owner:
                return owner, candidate
        raise ValueError(f"owner {owner} is not pinned in this client state")
    pub = bytes.fromhex(path.read_text(encoding="ascii").strip())
    if owner_id(pub) != owner:
        raise ValueError("pinned owner key does not match its namespace")
    return owner, pub


def _urls(values: list[str]) -> list[str]:
    import ipaddress
    if len(values) != SERVER_COUNT:
        raise ValueError("exactly four server URLs are required")
    normalized = []
    ports = set()
    for value in values:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("server URLs must be plain HTTP loopback addresses")
        if parsed.port is None or not 1 <= parsed.port <= 65535 or parsed.port in ports:
            raise ValueError("server ports must be four distinct valid ports")
        ports.add(parsed.port)
        normalized.append(f"http://127.0.0.1:{parsed.port}")
    return normalized


def _request(url: str, path: str, payload: dict | None = None, timeout: float = 0.8) -> dict:
    data = None if payload is None else _json_bytes(payload)
    request = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"} if data is not None else {}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_HTTP + 1)
    if len(raw) > MAX_HTTP:
        raise ValueError("server response exceeds the client limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("server response is not a JSON object")
    return value


def _pending_path(state: Path, owner: str, file_id: str) -> Path:
    return state / "pending" / owner / (file_id + ".json")


def _checkpoint_path(state: Path, owner: str, file_id: str) -> Path:
    return state / "checkpoints" / owner / (file_id + ".json")


@contextmanager
def _os_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            if lock_file.tell() == lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _mutation_lock(state: Path, owner: str, file_id: str):
    """Serialize local CLI mutations across threads and processes per file."""
    with _os_file_lock(state / "locks" / owner / (file_id + ".lock")):
        yield


@contextmanager
def _checkpoint_lock(state: Path, owner: str, file_id: str):
    """Serialize checkpoint read/validate/write without nesting mutation locks."""
    with _os_file_lock(state / "checkpoint-locks" / owner / (file_id + ".lock")):
        yield


def _history_votes(urls: list[str], owner: str, file_id: str) -> list[dict]:
    answers = 0
    groups: dict[str | None, list[list[dict]]] = {}
    heads: dict[str | None, int] = {}
    for url in urls:
        try:
            response = _request(url, "/v1/meta?" + urllib.parse.urlencode({"owner": owner, "file": file_id}))
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            continue
        answers += 1
        history = response.get("history")
        try:
            if not isinstance(history, list):
                raise InvalidRecord("server metadata has no history array")
            validate_history(history, owner, file_id)
            key = None if not history else update_hash(history[-1])
            groups.setdefault(key, []).append(history)
            heads[key] = 0 if not history else history[-1]["body"]["version"]
        except (InvalidRecord, KeyError, TypeError):
            continue
    if answers < METADATA_RESPONSES:
        raise QuorumError(f"metadata needs {METADATA_RESPONSES} server responses; received {answers}")
    # Select the freshest owner-signed history with q-b matching witnesses.
    # Stale replicas may still answer; an exact-head majority across all
    # answers would reject a valid newer head when old versions are split.
    qualified = [key for key, histories in groups.items() if len(histories) >= METADATA_VOTES]
    if not qualified:
        raise QuorumError("metadata has no head supported by two servers")
    newest_version = max(heads[key] for key in qualified)
    newest = [key for key in qualified if heads[key] == newest_version]
    if len(newest) != 1:
        raise QuorumError("metadata has divergent heads at the newest witnessed version")
    return groups[newest[0]][0]


def _history_all(urls: list[str], owner: str) -> dict[str, list[dict]]:
    answers = []
    for url in urls:
        try:
            response = _request(url, "/v1/list?" + urllib.parse.urlencode({"owner": owner}))
            answers.append(response)
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            continue
    if len(answers) < METADATA_RESPONSES:
        raise QuorumError(f"metadata listing needs {METADATA_RESPONSES} server responses; received {len(answers)}")
    file_ids = set()
    for response in answers:
        files = response.get("files")
        if isinstance(files, list):
            file_ids.update(file_id for file_id in files if isinstance(file_id, str))
    result = {}
    for file_id in sorted(file_ids):
        try:
            history = _history_votes(urls, owner, file_id)
            if history:
                result[file_id] = history
        except QuorumError:
            continue
    return result


def _history_checkpoint(state: Path, owner: str, file_id: str, history: list[dict]) -> None:
    with _checkpoint_lock(state, owner, file_id):
        checkpoint_path = _checkpoint_path(state, owner, file_id)
        if not history:
            if checkpoint_path.exists():
                raise InvalidRecord("servers no longer provide the reader's witnessed file history")
            return
        hashes = [update_hash(update) for update in history]
        if checkpoint_path.exists():
            saved = validate_checkpoint(json.loads(checkpoint_path.read_text(encoding="utf-8")))
            if saved["owner"] != owner or saved["file"] != file_id or saved["head"] not in hashes:
                raise InvalidRecord("signed server history does not extend the reader's saved checkpoint")
            saved_index = saved["version"] - 1
            if saved_index >= len(hashes) or hashes[:saved["version"]] != saved["chain"] or hashes[saved_index] != saved["head"]:
                raise InvalidRecord("saved reader checkpoint is malformed")
        _atomic(checkpoint_path, _json_bytes({
            "owner": owner,
            "file": file_id,
            "version": len(history),
            "head": hashes[-1],
            "chain": hashes,
            "history": history,
        }))


def _history_for_write(urls: list[str], state: Path, owner: str, file_id: str) -> list[dict]:
    try:
        history = _history_votes(urls, owner, file_id)
        _history_checkpoint(state, owner, file_id, history)
        return history
    except QuorumError:
        checkpoint_path = _checkpoint_path(state, owner, file_id)
        if not checkpoint_path.exists():
            raise
        checkpoint = validate_checkpoint(json.loads(checkpoint_path.read_text(encoding="utf-8")))
        if checkpoint["owner"] != owner or checkpoint["file"] != file_id or "history" not in checkpoint:
            raise InvalidRecord("offline write requires a matching signed local history checkpoint")
        return checkpoint["history"]


def _manifest_share(body: dict, pub: bytes, share_id: int, stripe_index: int, encoded: str) -> bytes:
    block = unb64(encoded, 128 * 1024)
    descriptor = next((item for item in body["stripes"][stripe_index]["blocks"] if item["share_id"] == share_id), None)
    if descriptor is None or hashlib.sha256(block).hexdigest() != descriptor["sha256"]:
        raise InvalidRecord("share bytes do not match the signed digest")
    load_public(pub).verify(
        unb64(descriptor["signature"], 128),
        block_message(body["owner"], body["file"], body["version"], stripe_index, share_id, descriptor["sha256"]),
    )
    return block


def validate_share_response(response: dict, signed: dict, share_id: int, pub: bytes, stripe_index: int) -> bytes:
    if response.get("update") != signed or response.get("share_id") != share_id:
        raise InvalidRecord("server returned another version or share identifier")
    blocks = response.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != len(signed["body"]["stripes"]):
        raise InvalidRecord("server returned the wrong number of stripes")
    return _manifest_share(signed["body"], pub, share_id, stripe_index, blocks[stripe_index])


def _collect_shares(urls: list[str], history: list[dict], pub: bytes, stripe_index: int, responses: dict[int, dict]) -> tuple[list[bytes], list[int]]:
    signed = history[-1]
    body = signed["body"]
    version = body["version"]
    shares: dict[int, bytes] = {}
    for expected_id, url in enumerate(urls):
        try:
            # A response rejected for this stripe must not stay cached for later stripes.
            response = responses.pop(expected_id, None)
            if response is None:
                response = _request(url, "/v1/block?" + urllib.parse.urlencode({
                    "owner": body["owner"], "file": body["file"], "version": str(version),
                }))
            shares[expected_id] = validate_share_response(response, signed, expected_id, pub, stripe_index)
            responses[expected_id] = response
        except (OSError, ValueError, InvalidRecord, KeyError, TypeError, StopIteration, urllib.error.URLError, TimeoutError):
            continue
        except Exception:
            continue
    if len(shares) < ERASURE_THRESHOLD:
        raise QuorumError(f"stripe {stripe_index} needs {ERASURE_THRESHOLD} distinct signed shares; received {len(shares)}")
    share_ids = sorted(shares)[:ERASURE_THRESHOLD]
    return [shares[share_id] for share_id in share_ids], share_ids


async def _encode_file(data: bytes, private_key: Ed25519PrivateKey, file_id: str, parent: str | None, version: int) -> tuple[dict, list[list[str]]]:
    owner_pub = public_bytes(private_key)
    owner = owner_id(owner_pub)
    stripes = []
    encoded_by_server = [[] for _ in range(SERVER_COUNT)]
    for offset in range(0, len(data), 128 * 1024):
        chunk = data[offset:offset + 128 * 1024]
        shares, _share_size = await encode_data(chunk)
        blocks = []
        for share_id, block in enumerate(shares):
            digest = hashlib.sha256(block).hexdigest()
            from .model import b64 as encode_b64
            sig = private_key.sign(block_message(owner, file_id, version, len(stripes), share_id, digest))
            blocks.append({"share_id": share_id, "sha256": digest, "signature": encode_b64(sig)})
            encoded_by_server[share_id].append(encode_b64(block))
        stripes.append({"length": len(chunk), "blocks": blocks})
    body = {
        "public_key": owner_pub.hex(), "owner": owner, "file": file_id,
        "version": version, "parent": parent, "action": "put", "size": len(data), "stripes": stripes,
    }
    return body, encoded_by_server


def _make_delete(private_key: Ed25519PrivateKey, file_id: str, parent: str | None, version: int) -> dict:
    pub = public_bytes(private_key)
    return {
        "public_key": pub.hex(), "owner": owner_id(pub), "file": file_id,
        "version": version, "parent": parent, "action": "delete", "size": 0, "stripes": [],
    }


async def _decode_stripes(body: dict, shares_per_stripe: list[tuple[list[bytes], list[int]]]) -> bytes:
    contents = []
    for stripe, (shares, share_ids) in zip(body["stripes"], shares_per_stripe):
        contents.append(await decode_data(shares, share_ids, stripe["length"]))
    return b"".join(contents)[:body["size"]]


def _deliver(urls: list[str], state: Path, pending: dict) -> int:
    update = pending["update"]
    body = validate_update(update)
    valid_acks = set()
    history = pending["history"]
    for share_id, url in enumerate(urls):
        request = {
            "history": history,
            "update": update,
            "share_id": share_id,
            "blocks": pending["blocks"][share_id],
        }
        try:
            response = _request(url, "/v1/put", request)
            if response.get("stored") is True and response.get("share_id") == share_id:
                valid_acks.add(share_id)
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            continue
    if len(valid_acks) < WRITE_ACKS:
        raise UncertainCommit(f"only {len(valid_acks)} of {WRITE_ACKS} required acknowledgments arrived; retry the same pending operation")
    path = _pending_path(state, body["owner"], body["file"])
    _history_checkpoint(state, body["owner"], body["file"], history)
    path.unlink(missing_ok=True)
    return len(valid_acks)


def _put_locked(urls: list[str], state: Path, file_id: str, source: Path, create_only: bool = False) -> dict:
    validate_file_id(file_id)
    if not source.is_file() or source.stat().st_size > MAX_FILE:
        raise ValueError("input must be a regular file no larger than 1 MiB")
    data = source.read_bytes()
    if len(data) != source.stat().st_size:
        raise ValueError("input changed while it was being read")
    private = _private(state)
    pub = public_bytes(private)
    owner = owner_id(pub)
    if _pending_path(state, owner, file_id).exists():
        raise UncertainCommit("this file has an unresolved pending mutation; retry it first")
    history = _history_for_write(urls, state, owner, file_id)
    if create_only and history:
        raise FileExistsError(file_id)
    parent = None if not history else update_hash(history[-1])
    version = 1 if not history else history[-1]["body"]["version"] + 1
    body, blocks = run_codec(_encode_file(data, private, file_id, parent, version))
    signed = sign_update(body, private)
    validate_update(signed)
    full_history = history + [signed]
    pending = {"history": full_history, "update": signed, "blocks": blocks}
    pending_path = _pending_path(state, owner, file_id)
    _atomic(pending_path, _json_bytes(pending))
    acks = _deliver(urls, state, pending)
    return {"owner": owner, "file": file_id, "version": version, "sha256": hashlib.sha256(data).hexdigest(), "acknowledgments": acks}


def put(urls: list[str], state: Path, file_id: str, source: Path, create_only: bool = False) -> dict:
    validate_file_id(file_id)
    owner = owner_id(public_bytes(_private(state)))
    with _mutation_lock(state, owner, file_id):
        return _put_locked(urls, state, file_id, source, create_only)


def create(urls: list[str], state: Path, file_id: str, source: Path) -> dict:
    return put(urls, state, file_id, source, create_only=True)


def retry(urls: list[str], state: Path, file_id: str) -> dict:
    validate_file_id(file_id)
    private = _private(state)
    owner = owner_id(public_bytes(private))
    with _mutation_lock(state, owner, file_id):
        return _retry_locked(urls, state, file_id, owner)


def _retry_locked(urls: list[str], state: Path, file_id: str, owner: str) -> dict:
    path = _pending_path(state, owner, file_id)
    if not path.exists():
        raise ValueError("no pending operation exists for this file")
    pending = json.loads(path.read_text(encoding="utf-8"))
    body = validate_update(pending["update"])
    validate_history(pending["history"], owner, file_id)
    acks = _deliver(urls, state, pending)
    return {"owner": owner, "file": file_id, "version": body["version"], "acknowledgments": acks, "retried": True}


def _delete_locked(urls: list[str], state: Path, file_id: str) -> dict:
    validate_file_id(file_id)
    private = _private(state)
    pub = public_bytes(private)
    owner = owner_id(pub)
    if _pending_path(state, owner, file_id).exists():
        raise UncertainCommit("this file has an unresolved pending mutation; retry it first")
    history = _history_for_write(urls, state, owner, file_id)
    if not history or history[-1]["body"]["action"] == "delete":
        raise FileNotFoundError(file_id)
    version = history[-1]["body"]["version"] + 1
    signed = sign_update(_make_delete(private, file_id, update_hash(history[-1]), version), private)
    pending = {"history": history + [signed], "update": signed, "blocks": [[] for _ in urls]}
    _atomic(_pending_path(state, owner, file_id), _json_bytes(pending))
    acks = _deliver(urls, state, pending)
    return {"owner": owner, "file": file_id, "version": version, "action": "delete", "acknowledgments": acks}


def delete(urls: list[str], state: Path, file_id: str) -> dict:
    validate_file_id(file_id)
    owner = owner_id(public_bytes(_private(state)))
    with _mutation_lock(state, owner, file_id):
        return _delete_locked(urls, state, file_id)


def read(urls: list[str], state: Path, file_id: str, owner: str | None = None) -> tuple[bytes, dict]:
    validate_file_id(file_id)
    owner, pub = _owner_public(state, owner)
    history = _history_votes(urls, owner, file_id)
    _history_checkpoint(state, owner, file_id, history)
    if not history:
        raise FileNotFoundError(file_id)
    body = history[-1]["body"]
    if body["action"] == "delete":
        raise FileNotFoundError(file_id)
    all_shares = []
    responses: dict[int, dict] = {}
    for stripe_index in range(len(body["stripes"])):
        all_shares.append(_collect_shares(urls, history, pub, stripe_index, responses))
    data = run_codec(_decode_stripes(body, all_shares))
    return data, {"owner": owner, "file": file_id, "version": body["version"], "sha256": hashlib.sha256(data).hexdigest()}


def list_files(urls: list[str], state: Path, owner: str | None = None) -> list[dict]:
    owner, _pub = _owner_public(state, owner)
    histories = _history_all(urls, owner)
    result = []
    for file_id, history in sorted(histories.items()):
        body = history[-1]["body"]
        _history_checkpoint(state, owner, file_id, history)
        if body["action"] == "put":
            result.append({"file": file_id, "version": body["version"], "size": body["size"], "action": body["action"], "head": update_hash(history[-1])})
    return result


def _get_shares_for_version(urls: list[str], history: list[dict], pub: bytes, version_update: dict, stripe_index: int) -> tuple[list[bytes], list[int]]:
    body = version_update["body"]
    shares = {}
    for expected_id, url in enumerate(urls):
        try:
            response = _request(url, "/v1/block?" + urllib.parse.urlencode({
                "owner": body["owner"], "file": body["file"], "version": str(body["version"]),
            }))
            shares[expected_id] = validate_share_response(response, version_update, expected_id, pub, stripe_index)
        except Exception:
            continue
    if len(shares) < ERASURE_THRESHOLD:
        raise QuorumError(f"version {body['version']} stripe {stripe_index} has fewer than two valid shares")
    ids = sorted(shares)[:ERASURE_THRESHOLD]
    return [shares[i] for i in ids], ids


async def _repair_payloads(urls: list[str], history: list[dict], pub: bytes, target: int) -> list[dict]:
    payloads = []
    first_version = max(1, history[-1]["body"]["version"] - 7)
    # Install the selected head first. A failure while restoring older
    # retained shares must not leave metadata advertising an unreadable head.
    for signed in reversed(history[first_version - 1:]):
        body = signed["body"]
        if body["action"] == "delete":
            payloads.append({"history": history, "update": signed, "share_id": target, "blocks": []})
            continue
        blocks = []
        for stripe_index, stripe in enumerate(body["stripes"]):
            shares, ids = _get_shares_for_version(urls, history, pub, signed, stripe_index)
            contents = await decode_data(shares, ids, stripe["length"])
            encoded, _size = await encode_data(contents)
            target_block = encoded[target]
            _manifest_share(body, pub, target, stripe_index, b64(target_block))
            blocks.append(b64(target_block))
        payloads.append({"history": history, "update": signed, "share_id": target, "blocks": blocks})
    return payloads


def repair(urls: list[str], state: Path, file_id: str, target: int, owner: str | None = None) -> dict:
    validate_file_id(file_id)
    if target not in range(SERVER_COUNT):
        raise ValueError("target server must be 0..3")
    owner, pub = _owner_public(state, owner)
    history = _history_votes(urls, owner, file_id)
    _history_checkpoint(state, owner, file_id, history)
    if not history:
        raise FileNotFoundError(file_id)
    # Repair must obey the same monotonic reader witness as reads and writes.
    payloads = run_codec(_repair_payloads(urls, history, pub, target))
    repaired = []
    for payload in payloads:
        try:
            response = _request(urls[target], "/v1/repair", payload)
            if response.get("stored") is not True or response.get("share_id") != target:
                raise QuorumError("repair server rejected the verified share")
            repaired.append(payload["update"]["body"]["version"])
        except Exception as error:
            raise QuorumError(f"repair stopped at version {payload['update']['body']['version']}: {error}") from error
    return {"owner": owner, "file": file_id, "target": target, "repaired_versions": repaired}


def export_checkpoint(state: Path, owner: str, file_id: str, destination: Path) -> dict:
    validate_file_id(file_id)
    _owner_public(state, owner)
    path = _checkpoint_path(state, owner, file_id)
    if not path.exists():
        raise ValueError("reader has no checkpoint for this file")
    value = json.loads(path.read_text(encoding="utf-8"))
    _atomic(destination, _json_bytes(value))
    return value


def audit_checkpoints(left_path: Path, right_path: Path) -> dict:
    left = json.loads(left_path.read_text(encoding="utf-8"))
    right = json.loads(right_path.read_text(encoding="utf-8"))
    relation = checkpoint_relation(left, right)
    return {"owner": left["owner"], "file": left["file"], "relation": relation}
