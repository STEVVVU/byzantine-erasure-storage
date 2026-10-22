"""One loopback-only disk-backed storage server for the BFR experiment."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import PROTOCOL_VERSION, SERVER_COUNT
from .model import InvalidRecord, b64, canonical, unb64, update_hash, validate_history, validate_update

MAX_REQUEST = 8 * 1024 * 1024
OWNER_ID = re.compile(r"^[0-9a-f]{64}$")


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".bfr-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(canonical(value))
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


def _read_json(path: Path):
    with path.open("rb") as source:
        raw = source.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise InvalidRecord("stored record exceeds request limit")
    return json.loads(raw)


class Handler(BaseHTTPRequestHandler):
    server_version = "BFR/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *args):
        print("rpc " + (_format % args), flush=True)

    @property
    def root(self) -> Path:
        return self.server.root  # type: ignore[attr-defined]

    @property
    def share_id(self) -> int:
        return self.server.share_id  # type: ignore[attr-defined]

    def _send(self, status: HTTPStatus, value) -> None:
        payload = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _query(self, required: tuple[str, ...]) -> dict[str, str]:
        values = parse_qs(urlsplit(self.path).query, strict_parsing=True)
        result = {}
        for name in required:
            parts = values.get(name)
            if not parts or len(parts) != 1:
                raise InvalidRecord("missing or duplicate query field")
            result[name] = parts[0]
        owner, file_id = result.get("owner"), result.get("file")
        if owner is not None and not OWNER_ID.fullmatch(owner):
            raise InvalidRecord("invalid owner identifier")
        if file_id is not None and (not file_id or len(file_id) > 64 or file_id in (".", "..") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", file_id)):
            raise InvalidRecord("invalid file identifier")
        return result

    def _metadata_path(self, owner: str, file_id: str) -> Path:
        return self.root / "metadata" / owner / (file_id + ".json")

    def _share_path(self, owner: str, file_id: str, version: int) -> Path:
        return self.root / "shares" / owner / file_id / (str(version) + ".json")

    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path == "/v1/health":
                return self._send(HTTPStatus.OK, {"protocol": PROTOCOL_VERSION, "share_id": self.share_id, "pid": os.getpid()})
            if path == "/v1/meta":
                query = self._query(("owner", "file"))
                record_path = self._metadata_path(query["owner"], query["file"])
                if not record_path.exists():
                    return self._send(HTTPStatus.OK, {"history": []})
                return self._send(HTTPStatus.OK, _read_json(record_path))
            if path == "/v1/list":
                query = self._query(("owner",))
                owner_path = self.root / "metadata" / query["owner"]
                files = []
                if owner_path.exists():
                    files = sorted(record_path.stem for record_path in owner_path.glob("*.json"))
                if len(files) > 1024:
                    raise InvalidRecord("listing exceeds the 1024-file experiment limit")
                return self._send(HTTPStatus.OK, {"files": files})
            if path == "/v1/block":
                query = self._query(("owner", "file", "version"))
                version = int(query["version"])
                if not 1 <= version <= 256:
                    raise InvalidRecord("version is outside the bounded range")
                record_path = self._share_path(query["owner"], query["file"], version)
                if not record_path.exists():
                    return self._send(HTTPStatus.NOT_FOUND, {"error": "share unavailable"})
                return self._send(HTTPStatus.OK, _read_json(record_path))
            return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
        except (InvalidRecord, ValueError, json.JSONDecodeError) as error:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except OSError:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "local storage unavailable"})

    def _parse_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if not raw_length or not raw_length.isdecimal() or int(raw_length) > MAX_REQUEST:
            raise InvalidRecord("request body exceeds limit")
        raw = self.rfile.read(int(raw_length))
        if len(raw) != int(raw_length):
            raise InvalidRecord("truncated request body")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise InvalidRecord("request body must be a JSON object")
        return value

    def _install(self, value: dict, allow_repair: bool) -> None:
        signed = value.get("update")
        body = validate_update(signed)
        key = (body["owner"], body["file"])
        with self.server.file_locks_guard:  # type: ignore[attr-defined]
            lock = self.server.file_locks.setdefault(key, threading.Lock())  # type: ignore[attr-defined]
        with lock:
            self._install_unlocked(value, allow_repair)

    def _install_unlocked(self, value: dict, allow_repair: bool) -> None:
        history = value.get("history")
        if not isinstance(history, list) or not history:
            raise InvalidRecord("update history cannot be empty")
        validate_history(history)
        signed = value.get("update")
        body = validate_update(signed)
        validate_history(history, body["owner"], body["file"])
        if body["owner"] != history[-1]["body"]["owner"] or body["file"] != history[-1]["body"]["file"] or signed not in history:
            raise InvalidRecord("update is not in supplied signed history")
        if body["action"] == "put":
            share_id = value.get("share_id")
            blocks = value.get("blocks")
            if share_id != self.share_id or not isinstance(blocks, list) or len(blocks) != len(body["stripes"]):
                raise InvalidRecord("request does not carry this server's share")
            for stripe_index, encoded in enumerate(blocks):
                block = unb64(encoded, 128 * 1024)
                descriptor = next(item for item in body["stripes"][stripe_index]["blocks"] if item["share_id"] == share_id)
                if len(block) != (body["stripes"][stripe_index]["length"] + 1) // 2:
                    raise InvalidRecord("share length does not match stripe")
                import hashlib
                if hashlib.sha256(block).hexdigest() != descriptor["sha256"]:
                    raise InvalidRecord("share digest does not match signed manifest")
            share_record = {"share_id": share_id, "blocks": blocks, "update": signed}
        else:
            share_record = None

        owner, file_id, version = body["owner"], body["file"], body["version"]
        metadata_path = self._metadata_path(owner, file_id)
        current_history = []
        if metadata_path.exists():
            current_history = _read_json(metadata_path).get("history", [])
            validate_history(current_history, owner, file_id)
        preserve_newer_history = False
        if current_history:
            current_version = current_history[-1]["body"]["version"]
            if current_version > version:
                if signed not in current_history:
                    raise InvalidRecord("server has a newer conflicting history")
                preserve_newer_history = True
            elif current_version == version and current_history[-1] != history[-1]:
                raise InvalidRecord("conflicting signed update at this version")
            elif current_history != history[:len(current_history)]:
                raise InvalidRecord("incoming history does not extend server history")
        elif version != 1 and history[0]["body"]["version"] != 1:
            raise InvalidRecord("history does not start at version one")

        if share_record is not None:
            share_path = self._share_path(owner, file_id, version)
            if preserve_newer_history and not share_path.exists() and not allow_repair:
                raise InvalidRecord("stale retry has no durable share record")
            if share_path.exists() and not allow_repair:
                try:
                    if _read_json(share_path) != share_record:
                        raise InvalidRecord("different data already stored for this version")
                except (ValueError, OSError, json.JSONDecodeError):
                    raise InvalidRecord("existing share is corrupt; use repair")
            else:
                _atomic_json(share_path, share_record)
        if not preserve_newer_history:
            _atomic_json(metadata_path, {"history": history})
        prune_before = max(1, history[-1]["body"]["version"] - 7)
        shares_path = self.root / "shares" / owner / file_id
        if shares_path.exists():
            for old in shares_path.glob("*.json"):
                try:
                    if int(old.stem) < prune_before:
                        old.unlink()
                except ValueError:
                    continue

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path not in ("/v1/put", "/v1/repair"):
                return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            self._install(self._parse_body(), allow_repair=path == "/v1/repair")
            self._send(HTTPStatus.OK, {"stored": True, "share_id": self.share_id})
        except (InvalidRecord, ValueError, KeyError, TypeError, json.JSONDecodeError, StopIteration) as error:
            self._send(HTTPStatus.CONFLICT, {"error": str(error)})
        except OSError:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "local storage unavailable"})


def serve(root: Path, host: str, port: int, share_id: int) -> None:
    import ipaddress
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("BFR servers bind only to loopback")
    if share_id not in range(SERVER_COUNT):
        raise ValueError("share id must be in 0..3")
    root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.root = root  # type: ignore[attr-defined]
    server.share_id = share_id  # type: ignore[attr-defined]
    server.file_locks = {}  # type: ignore[attr-defined]
    server.file_locks_guard = threading.Lock()  # type: ignore[attr-defined]
    print(json.dumps({"event": "ready", "host": host, "port": port, "share_id": share_id, "pid": os.getpid()}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--share-id", type=int, required=True)
    args = parser.parse_args(argv)
    serve(args.root, args.host, args.port, args.share_id)


if __name__ == "__main__":
    main()
