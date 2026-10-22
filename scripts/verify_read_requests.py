"""Prove exact 1 MiB reconstruction and read-local HTTP reuse with real servers."""

import argparse
import hashlib
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bfr import client
from scripts.verify import Run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    assert shutil.disk_usage(ROOT).free >= 10 * 1024**3
    run = Run(args.run_id)
    if run.path.exists():
        parser.error("choose a fresh run id")
    failure = None
    try:
        run.launch()
        run.doctor()
        writer = run.scratch / "writer"
        owner = run.cli(writer, "keygen")["owner"]
        expected = bytes((i * 37 + 11) % 251 for i in range(1024 * 1024))
        source = run.scratch / "input"
        source.write_bytes(expected)
        created = run.cli(writer, "create", "sample", str(source))
        assert created["acknowledgments"] == 4
        with patch("bfr.client._request", wraps=client._request) as requests:
            actual, metadata = client.read(run.urls(), writer, "sample", owner)
        paths = [call.args[1] for call in requests.call_args_list]
        counts = {"metadata": sum(path.startswith("/v1/meta?") for path in paths),
                  "blocks": sum(path.startswith("/v1/block?") for path in paths),
                  "total": len(paths)}
        assert actual == expected and metadata["sha256"] == hashlib.sha256(expected).hexdigest()
        assert counts == {"metadata": 4, "blocks": 4, "total": 8}, counts
        run.save_event("read_request_proof", {"bytes": len(actual), "sha256": metadata["sha256"],
                                             "requests": counts, "network_and_codec_stubbed": False})
        print(run.events_path)
    except BaseException as error:
        failure = error
    cleanup = None
    cleanup_failure = None
    try:
        cleanup = run.cleanup()
    except BaseException as error:
        cleanup_failure = error
        if failure is None:
            failure = error
    passed = failure is None and cleanup is not None and cleanup["passed"]
    run.finalize_verdict(passed, failure, cleanup, cleanup_failure)
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__)
    assert passed, "request proof or owned cleanup failed"


if __name__ == "__main__":
    main()
