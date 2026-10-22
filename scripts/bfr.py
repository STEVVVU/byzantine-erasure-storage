#!/usr/bin/env python3
"""Run the bounded BFR client or one loopback storage server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from bfr import PROTOCOL_VERSION
from bfr.client import (
    QuorumError, UncertainCommit, audit_checkpoints, create, delete, export_checkpoint,
    initialize, list_files, put, read, repair, retry, trust_owner,
)
from bfr.model import InvalidRecord

DEFAULT_SERVERS = [f"http://127.0.0.1:{port}" for port in (17800, 17801, 17802, 17803)]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bounded four-server Byzantine erasure-storage experiment")
    result.add_argument("--state", type=Path, help="trusted client state directory")
    result.add_argument("--servers", nargs=4, default=DEFAULT_SERVERS, metavar="URL", help="four loopback server URLs in share-ID order")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen", help="create a local owner key")
    trust = commands.add_parser("trust-owner", help="pin a 32-byte hex owner public-key file")
    trust.add_argument("public_key", type=Path)
    create_command = commands.add_parser("create", help="create a new signed file")
    create_command.add_argument("file")
    create_command.add_argument("source", type=Path)
    upload = commands.add_parser("put", help="write a whole file as a new signed version")
    upload.add_argument("file")
    upload.add_argument("source", type=Path)
    download = commands.add_parser("read", help="read the current signed version")
    download.add_argument("file")
    download.add_argument("destination", type=Path)
    download.add_argument("--owner")
    listing = commands.add_parser("list", help="list files for the trusted owner")
    listing.add_argument("--owner")
    remove = commands.add_parser("delete", help="write a signed tombstone")
    remove.add_argument("file")
    commands.add_parser("retry", help="retry the exact pending operation for a file")
    commands.choices["retry"].add_argument("file")
    repair_command = commands.add_parser("repair", help="reconstruct the selected server's recent shares")
    repair_command.add_argument("file")
    repair_command.add_argument("--target", required=True, type=int, choices=range(4))
    repair_command.add_argument("--owner")
    export = commands.add_parser("export-checkpoint", help="export this reader's saved checkpoint")
    export.add_argument("file")
    export.add_argument("destination", type=Path)
    export.add_argument("--owner", required=True)
    audit = commands.add_parser("audit-checkpoints", help="compare two saved reader checkpoints")
    audit.add_argument("left", type=Path)
    audit.add_argument("right", type=Path)
    server = commands.add_parser("server", help=argparse.SUPPRESS)
    server.add_argument("--root", type=Path, required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", required=True, type=int)
    server.add_argument("--share-id", required=True, type=int, choices=range(4))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "server":
        from bfr.server import serve
        serve(args.root, args.host, args.port, args.share_id)
        return 0
    if args.command == "audit-checkpoints":
        result = audit_checkpoints(args.left, args.right)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["relation"] != "divergent" else 2
    if args.state is None:
        raise ValueError("--state is required for client commands")
    if args.command == "keygen":
        print(json.dumps({"owner": initialize(args.state), "protocol": PROTOCOL_VERSION}, sort_keys=True))
    elif args.command == "trust-owner":
        print(json.dumps({"owner": trust_owner(args.state, args.public_key)}, sort_keys=True))
    else:
        from bfr.client import _urls
        urls = _urls(args.servers)
        if args.command == "create":
            result = create(urls, args.state, args.file, args.source)
        elif args.command == "put":
            result = put(urls, args.state, args.file, args.source)
        elif args.command == "read":
            data, result = read(urls, args.state, args.file, args.owner)
            from bfr.client import _atomic
            _atomic(args.destination, data)
        elif args.command == "list":
            result = list_files(urls, args.state, args.owner)
        elif args.command == "delete":
            result = delete(urls, args.state, args.file)
        elif args.command == "retry":
            result = retry(urls, args.state, args.file)
        elif args.command == "repair":
            result = repair(urls, args.state, args.file, args.target, args.owner)
        elif args.command == "export-checkpoint":
            result = export_checkpoint(args.state, args.owner, args.file, args.destination)
        else:
            raise ValueError("unknown command")
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UncertainCommit as error:
        print(json.dumps({"outcome": "uncertain", "error": str(error)}), file=sys.stderr)
        raise SystemExit(3)
    except (QuorumError, InvalidRecord, ValueError, OSError, FileNotFoundError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(2)
