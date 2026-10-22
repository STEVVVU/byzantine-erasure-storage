#!/usr/bin/env python3
"""Launch, drive, and clean an isolated four-process BFR verification run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNS = ROOT / ".agents" / "runs"


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class Run:
    def __init__(self, run_id: str):
        if not run_id or len(run_id) > 64 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in run_id):
            raise ValueError("run id must use only letters, digits, hyphen, and underscore")
        self.run_id = run_id
        self.path = RUNS / run_id
        self.evidence = self.path / "evidence"
        self.scratch = self.path / "scratch"
        self.ownership_path = self.path / "ownership.json"
        self.events_path = self.evidence / "evidence.json"
        self.events = _read_json(self.events_path) if self.events_path.exists() else {"run_id": run_id, "events": []}

    def save_event(self, name: str, value) -> None:
        self.events["events"].append({"name": name, "result": value})
        _atomic_json(self.events_path, self.events)

    def ownership(self) -> dict:
        return _read_json(self.ownership_path)

    def save_ownership(self, value: dict) -> None:
        _atomic_json(self.ownership_path, value)

    def launch(self) -> None:
        if self.ownership_path.exists():
            raise ValueError("run id has already been launched")
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.scratch.mkdir(parents=True, exist_ok=False)
        servers = []
        for share_id in range(4):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            servers.append({"share_id": share_id, "port": port, "root": str(self.scratch / f"server-{share_id}"), "pids": [], "active_pid": None})
        self.save_ownership({"run_id": self.run_id, "servers": servers})
        self.save_event("launch_started", {"server_count": len(servers), "host": "127.0.0.1"})
        for server in servers:
            self.start(server["share_id"])
        self.save_event("launch", {"servers": [{"share_id": item["share_id"], "port": item["port"], "pid": item["active_pid"]} for item in self.ownership()["servers"]]})

    def _command(self, server: dict) -> list[str]:
        return [
            sys.executable, str(ROOT / "scripts" / "bfr.py"), "server",
            "--root", server["root"], "--host", "127.0.0.1", "--port", str(server["port"]),
            "--share-id", str(server["share_id"]),
        ]

    def _pid_is_owned(self, server: dict, pid: int) -> bool:
        try:
            process = psutil.Process(pid)
            args = [part.replace("\\", "/").lower() for part in process.cmdline()]
            return (
                str(ROOT / "scripts" / "bfr.py").replace("\\", "/").lower() in args
                and "server" in args
                and "--share-id" in args and str(server["share_id"]) in args
                and "--port" in args and str(server["port"]) in args
                and server["root"].replace("\\", "/").lower() in args
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def _health(self, server: dict) -> dict:
        url = f"http://127.0.0.1:{server['port']}/v1/health"
        with urllib.request.urlopen(url, timeout=0.4) as response:
            payload = response.read(2048)
        result = json.loads(payload)
        if result.get("protocol") != 1 or result.get("share_id") != server["share_id"]:
            raise RuntimeError("server health response has the wrong protocol or share id")
        return result

    def start(self, share_id: int) -> None:
        ownership = self.ownership()
        server = ownership["servers"][share_id]
        log_path = self.evidence / f"server-{share_id}.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(self._command(server), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        server["launch_pid"] = process.pid
        server["pids"].append(process.pid)
        server["active_pid"] = None
        self.save_ownership(ownership)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"owned server {share_id} exited during launch")
            try:
                health = self._health(server)
                service_pid = health.get("pid")
                if isinstance(service_pid, int) and self._pid_is_owned(server, service_pid):
                    if service_pid not in server["pids"]:
                        server["pids"].append(service_pid)
                    server["active_pid"] = service_pid
                    self.save_ownership(ownership)
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.05)
        raise TimeoutError(f"owned server {share_id} did not become ready")

    def doctor(self) -> None:
        ownership = self.ownership()
        checked = []
        for server in ownership["servers"]:
            pid = server["active_pid"]
            launch_pid = server.get("launch_pid")
            if not pid or not launch_pid or not self._pid_is_owned(server, pid) or not self._pid_is_owned(server, launch_pid):
                raise RuntimeError(f"server {server['share_id']} has no live owned PID")
            health = self._health(server)
            if health.get("pid") != pid:
                raise RuntimeError(f"server {server['share_id']} port answers from another process")
            checked.append({"share_id": server["share_id"], "pid": pid, "protocol": health["protocol"]})
        self.save_event("doctor", {"servers": checked, "result": "ready"})

    def stop(self, share_id: int) -> None:
        ownership = self.ownership()
        server = ownership["servers"][share_id]
        pid = server["active_pid"]
        if not pid:
            return
        if not self._pid_is_owned(server, pid):
            raise RuntimeError(f"refusing to stop PID {pid}; it is not the recorded BFR server")
        for owned_pid in (pid, server.get("launch_pid")):
            if owned_pid and psutil.pid_exists(owned_pid):
                if not self._pid_is_owned(server, owned_pid):
                    raise RuntimeError(f"refusing to stop PID {owned_pid}; it is not the recorded BFR process")
                try:
                    psutil.Process(owned_pid).terminate()
                    psutil.Process(owned_pid).wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
                except psutil.TimeoutExpired as error:
                    raise RuntimeError(f"owned server PID {owned_pid} did not stop") from error
        server["active_pid"] = None
        server["launch_pid"] = None
        self.save_ownership(ownership)

    def urls(self) -> list[str]:
        return [f"http://127.0.0.1:{server['port']}" for server in self.ownership()["servers"]]

    def cli(self, state: Path, command: str, *args: str, expected: int = 0) -> dict | str:
        cmd = [sys.executable, str(ROOT / "scripts" / "bfr.py"), "--state", str(state), "--servers", *self.urls(), command, *map(str, args)]
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=20)
        detail = {"command": command, "args": [str(arg) for arg in args], "returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        self.save_event("cli", detail)
        if result.returncode != expected:
            raise RuntimeError(f"{command} returned {result.returncode}, expected {expected}: {result.stdout} {result.stderr}")
        if expected != 0:
            return result.stderr.strip()
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else ""

    def _copy(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def drive(self) -> None:
        writer, reader = self.scratch / "writer", self.scratch / "reader"
        writer.mkdir()
        reader.mkdir()
        payload1 = bytes(((i * 37 + 11) % 251 for i in range(128 * 1024 + 37)))
        input1 = self.scratch / "sample-v1.bin"
        input1.write_bytes(payload1)
        first = self.cli(writer, "keygen")
        owner = first["owner"]
        self.cli(reader, "trust-owner", str(writer / "owner.pub"))
        version1 = self.cli(writer, "create", "sample", str(input1))
        if version1["version"] != 1 or version1["acknowledgments"] < 3:
            raise AssertionError("initial create did not reach the 3-ack quorum")
        output = self.scratch / "reader-v1.bin"
        self.cli(reader, "read", "sample", str(output), "--owner", owner)
        if output.read_bytes() != payload1:
            raise AssertionError("independent reader did not reconstruct version one")
        cp1 = self.scratch / "reader-v1.checkpoint.json"
        self.cli(reader, "export-checkpoint", "sample", str(cp1), "--owner", owner)
        self.cli(writer, "create", "sample", str(input1), expected=2)

        payload2 = bytes(((i * 19 + 3) % 253 for i in range(73_019)))
        input2 = self.scratch / "sample-v2.bin"
        input2.write_bytes(payload2)
        version2 = self.cli(writer, "put", "sample", str(input2))
        if version2["version"] != 2:
            raise AssertionError("put did not advance the signed version")
        output2 = self.scratch / "reader-v2.bin"
        self.cli(reader, "read", "sample", str(output2), "--owner", owner)
        if output2.read_bytes() != payload2:
            raise AssertionError("reader mixed versions or reconstructed wrong bytes")
        cp2 = self.scratch / "reader-v2.checkpoint.json"
        self.cli(reader, "export-checkpoint", "sample", str(cp2), "--owner", owner)
        relation = self.cli(writer, "audit-checkpoints", str(cp1), str(cp2))
        if relation["relation"] != "right-extends-left":
            raise AssertionError("checkpoint comparison did not recognize the signed extension")

        metadata_paths = []
        for server in self.ownership()["servers"]:
            path = Path(server["root"]) / "metadata" / owner / "sample.json"
            metadata_paths.append((path, path.read_bytes()))
            old = json.loads(path.read_text(encoding="utf-8"))["history"][0]
            _atomic_json(path, {"history": [old]})
        try:
            self.cli(reader, "read", "sample", str(self.scratch / "rollback.bin"), "--owner", owner, expected=2)
        finally:
            for path, original in metadata_paths:
                path.write_bytes(original)
        self.save_event("rollback_rejected", {"checkpoint": 2, "server_history": 1, "result": "rejected"})

        servers = self.ownership()["servers"]
        share_root = Path(servers[0]["root"]) / "shares" / owner / "sample"
        old_share, current_share = share_root / "1.json", share_root / "2.json"
        current_backup = self.scratch / "server-0-v2.json"
        self._copy(current_share, current_backup)
        self._copy(old_share, current_share)
        self.stop(1)
        try:
            replayed_output = self.scratch / "replayed-share.bin"
            self.cli(reader, "read", "sample", str(replayed_output), "--owner", owner)
            if replayed_output.read_bytes() != payload2:
                raise AssertionError("reader accepted or mixed a previous-version share")
        finally:
            self._copy(current_backup, current_share)
            self.start(1)
        self.save_event("share_faults", {"server_0": "old signed share replayed at current version", "server_1": "stopped", "valid_shares": [2, 3], "result": "current bytes reconstructed"})

        self.stop(3)
        one_down = self.scratch / "one-down.bin"
        one_down.write_bytes(b"created with one server stopped" * 17)
        created_down = self.cli(writer, "create", "one-down", str(one_down))
        if created_down["acknowledgments"] != 3:
            raise AssertionError("write with one stopped server did not reach exactly three acknowledgments")
        self.start(3)
        repaired = self.cli(writer, "repair", "one-down", "--target", "3", "--owner", owner)
        if 1 not in repaired["repaired_versions"]:
            raise AssertionError("repair did not install the missed signed version")
        repaired_output = self.scratch / "repaired.bin"
        self.cli(reader, "read", "one-down", str(repaired_output), "--owner", owner)
        if repaired_output.read_bytes() != one_down.read_bytes():
            raise AssertionError("repaired server did not preserve the signed share")

        uncertain_v1 = self.scratch / "pending-v1.bin"
        uncertain_v1.write_bytes(b"initial pending file" * 31)
        self.cli(writer, "create", "pending", str(uncertain_v1))
        self.stop(2)
        self.stop(3)
        self.cli(reader, "read", "pending", str(self.scratch / "offline-read.bin"), "--owner", owner, expected=2)
        self.save_event("metadata_quorum_failure", {"available_responses": 2, "required_responses": 3, "result": "read rejected"})
        uncertain_v2 = self.scratch / "pending-v2.bin"
        uncertain_v2.write_bytes(b"same signed intent after restart" * 23)
        self.cli(writer, "put", "pending", str(uncertain_v2), expected=3)
        pending_path = writer / "pending" / owner / "pending.json"
        if not pending_path.exists():
            raise AssertionError("uncertain mutation did not persist its pending intent")
        pending_before = pending_path.read_bytes()
        self.start(2)
        self.start(3)
        retried = self.cli(writer, "retry", "pending")
        if retried["version"] != 2 or not retried["retried"] or pending_path.exists():
            raise AssertionError("identical retry did not resolve and clear the pending intent")
        self.save_event("uncertain_retry", {"pending_sha256": hashlib.sha256(pending_before).hexdigest(), "resolved_version": retried["version"], "result": "same pending operation committed"})
        pending_output = self.scratch / "pending-v2-out.bin"
        self.cli(reader, "read", "pending", str(pending_output), "--owner", owner)
        if pending_output.read_bytes() != uncertain_v2.read_bytes():
            raise AssertionError("retried operation did not persist through restart")

        tombstone = self.cli(writer, "delete", "sample")
        if tombstone["version"] != 3:
            raise AssertionError("delete was not recorded as monotonic tombstone")
        listed = self.cli(reader, "list", "--owner", owner)
        if any(entry["file"] == "sample" for entry in listed):
            raise AssertionError("list exposed a tombstoned file")
        recreate = self.cli(writer, "put", "sample", str(input1))
        if recreate["version"] != 4:
            raise AssertionError("recreation did not advance past the tombstone")
        recreated_output = self.scratch / "recreated.bin"
        self.cli(reader, "read", "sample", str(recreated_output), "--owner", owner)
        if recreated_output.read_bytes() != payload1:
            raise AssertionError("recreated file contents are incorrect")

        self.cli(reader, "read", "no-such-file", str(self.scratch / "missing.bin"), "--owner", owner, expected=2)
        self.doctor()
        self.save_event("drive", {"result": "all normal and fault journeys passed", "owner": owner, "files": ["sample", "one-down", "pending"]})

    def evidence_phase(self) -> None:
        if not self.events_path.exists():
            raise RuntimeError("verification evidence was not recorded")
        logs = sorted(path.name for path in self.evidence.glob("server-*.log"))
        if len(logs) != 4 or any((self.evidence / name).stat().st_size == 0 for name in logs):
            raise RuntimeError("the four server transcripts are incomplete")
        self.save_event("evidence", {"events": len(self.events["events"]), "server_logs": logs, "path": str(self.evidence)})

    def cleanup(self) -> dict:
        errors = []
        if self.ownership_path.exists():
            try:
                ownership = self.ownership()
                for server in ownership["servers"]:
                    for pid in reversed(server["pids"]):
                        if not psutil.pid_exists(pid):
                            continue
                        if not self._pid_is_owned(server, pid):
                            errors.append(f"refusing to terminate unrelated/reused PID {pid}")
                            continue
                        try:
                            psutil.Process(pid).terminate()
                            psutil.Process(pid).wait(timeout=5)
                        except psutil.NoSuchProcess:
                            pass
                        except psutil.TimeoutExpired:
                            errors.append(f"owned process PID {pid} remained after cleanup")
                    # Update each server independently; do not leave later
                    # entries' launch PIDs stale due to loop indentation.
                    server["active_pid"] = (
                        server.get("active_pid")
                        if server.get("active_pid")
                        and psutil.pid_exists(server["active_pid"])
                        and self._pid_is_owned(server, server["active_pid"])
                        else None
                    )
                    server["launch_pid"] = (
                        server.get("launch_pid")
                        if server.get("launch_pid")
                        and psutil.pid_exists(server["launch_pid"])
                        and self._pid_is_owned(server, server["launch_pid"])
                        else None
                    )
                self.save_ownership(ownership)
            except Exception as error:
                errors.append(f"ownership cleanup failed: {type(error).__name__}: {error}")

        ownership = self.ownership() if self.ownership_path.exists() else {"servers": []}
        live = [
            pid for server in ownership["servers"] for pid in server["pids"]
            if psutil.pid_exists(pid) and self._pid_is_owned(server, pid)
        ]
        scratch_removed = not self.scratch.exists()
        if not live and not errors and self.scratch.exists():
            try:
                shutil.rmtree(self.scratch)
                scratch_removed = True
            except OSError as error:
                errors.append(f"scratch cleanup failed: {type(error).__name__}: {error}")
                scratch_removed = not self.scratch.exists()
        summary = {
            "owned_processes_alive": live,
            "scratch_removed": scratch_removed,
            "evidence_retained": self.events_path.exists(),
            "errors": errors,
            "passed": not errors and not live and scratch_removed and self.events_path.exists(),
        }
        self.save_event("cleanup", summary)
        return summary

    def finalize_verdict(
        self,
        passed: bool,
        failure: BaseException | None,
        cleanup: dict | None,
        cleanup_failure: BaseException | None,
    ) -> None:
        self.events["verdict"] = {
            "passed": passed,
            "failure": None if failure is None else {
                "type": type(failure).__name__,
                "message": str(failure),
            },
            "cleanup": cleanup,
            "cleanup_failure": None if cleanup_failure is None else {
                "type": type(cleanup_failure).__name__,
                "message": str(cleanup_failure),
            },
        }
        _atomic_json(self.events_path, self.events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("launch", "doctor", "drive", "evidence", "cleanup", "all"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--inject-failure-after-launch", action="store_true",
                        help="negative control: fail after launch and verify terminal failure evidence and cleanup")
    args = parser.parse_args(argv)
    run = Run(args.run_id)
    if args.phase == "all":
        failure = None
        cleanup_result = None
        try:
            run.launch()
            if args.inject_failure_after_launch:
                run.save_event("failure_injection", {"point": "after_launch"})
                raise RuntimeError("injected failure after launch")
            run.doctor()
            run.drive()
            run.evidence_phase()
        except BaseException as error:
            failure = error
        cleanup_failure = None
        try:
            cleanup_result = run.cleanup()
        except BaseException as error:
            cleanup_failure = error
            if failure is None:
                failure = error
        verdict_ok = failure is None and cleanup_result is not None and cleanup_result["passed"]
        try:
            run.finalize_verdict(verdict_ok, failure, cleanup_result, cleanup_failure)
        except BaseException as error:
            if failure is None:
                failure = error
        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)
        if not verdict_ok:
            raise RuntimeError("verification completed without a passing terminal verdict")
        return 0
    if args.inject_failure_after_launch:
        parser.error("--inject-failure-after-launch is supported only with the all phase")
    getattr(run, "evidence_phase" if args.phase == "evidence" else args.phase)()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
