"""Frozen, scheduled-arrival benchmark of the real four-process CLI experiment."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import pstats
import io
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from verify import ROOT, Run, _atomic_json
import psutil


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def latency_summary(values):
    return {"samples": len(values), "p50_seconds": percentile(values, .5), "p95_seconds": percentile(values, .95),
            "p99_seconds": percentile(values, .99) if len(values) >= 100 else None,
            "min_seconds": min(values) if values else None, "max_seconds": max(values) if values else None,
            "method": "nearest-rank empirical quantiles; p99 omitted below100 observations"}


def load_outcomes(samples):
    assert all(not (s["correct"] and s["expected_rejection"]) for s in samples)
    groups = {"completed": [s for s in samples if s["correct"]],
              "rejected": [s for s in samples if s["expected_rejection"]],
              "errors": [s for s in samples if not (s["correct"] or s["expected_rejection"])]}
    return {"offered": len(samples), **{name: len(rows) for name, rows in groups.items()},
            "timeouts": sum(s["timed_out"] for s in samples),
            "latency_by_outcome": {name: latency_summary([s["scheduled_latency_seconds"] for s in rows]) for name, rows in groups.items()}}


def expected_quorum_rejection(value):
    if value["returncode"] != 2 or value["timed_out"]:
        return False
    try:
        error = json.loads(value.get("stderr", "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    return isinstance(error, dict) and error.get("error") == "metadata needs 3 server responses; received 2"


def resources(run):
    return {str(s["share_id"]): {"rss_bytes": psutil.Process(s["active_pid"]).memory_info().rss,
                                 "cpu_seconds": sum(psutil.Process(s["active_pid"]).cpu_times()[:2])}
            for s in run.ownership()["servers"] if s["active_pid"]}


def trusted_codec(source, root, output, prepare=False):
    """Same Tahoe codec and fsync disk writes, without signatures, quorum or HTTP."""
    sys.path.insert(0, str(ROOT / "src"))
    from bfr.client import _atomic
    from bfr.codec import decode_data, encode_data, run_codec, STRIPE_CAPACITY

    async def roundtrip():
        recovered = bytearray()
        if prepare:
            data = source.read_bytes()
            _atomic(root / "metadata.json", json.dumps({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}).encode())
        metadata = json.loads((root / "metadata.json").read_text())
        for offset in range(0, metadata["size"], STRIPE_CAPACITY):
            stripe_size = min(STRIPE_CAPACITY, metadata["size"] - offset)
            if prepare:
                stripe = data[offset:offset + stripe_size]
                shares, _ = await encode_data(stripe)
                for i, share in enumerate(shares):
                    _atomic(root / f"{offset}-{i}", share)
            recovered.extend(await decode_data([(root / f"{offset}-{i}").read_bytes() for i in (0, 1)], [0, 1], stripe_size))
        _atomic(output, bytes(recovered))
        assert hashlib.sha256(recovered).hexdigest() == metadata["sha256"]
    run_codec(roundtrip())


def command(args, receipt, timeout=60):
    started = time.perf_counter()
    wrapped = ["/usr/bin/time", "-f", "%M %U %S", "-o", str(receipt)] + args if os.name == "posix" else args
    try:
        process = subprocess.Popen(wrapped, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=os.name == "posix")
        _, stderr = process.communicate(timeout=timeout)
        usage = receipt.read_text().splitlines()[-1].split() if receipt.exists() else []
        return {"seconds": time.perf_counter() - started, "returncode": process.returncode,
                "rss_kib": int(usage[0]) if len(usage) == 3 else None,
                "cpu_seconds": float(usage[1]) + float(usage[2]) if len(usage) == 3 else None,
                "stderr": stderr[-1000:], "timed_out": False}
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        return {"seconds": time.perf_counter() - started, "returncode": None, "timed_out": True}


def benchmark(run, repeats, smoke):
    writer = run.scratch / "writer"
    owner = run.cli(writer, "keygen")["owner"]
    sizes = [4096] if smoke else [4096, 131072, 1048576]
    cases = []
    logical_bytes = 0
    for size in sizes:
        source = run.scratch / f"input-{size}"
        source.write_bytes(bytes((i * 37 + 11) % 251 for i in range(size)))
        codec_root = run.scratch / f"codec-{size}"
        prepared = command([sys.executable, str(Path(__file__)), "--codec-source", str(source), "--codec-root", str(codec_root), "--codec-output", str(codec_root / "initial"), "--codec-prepare"], run.evidence / f"prepare-{size}.usage")
        assert prepared["returncode"] == 0
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        file_id = f"sample-{size}"
        write = run.cli(writer, "create", file_id, str(source))
        assert write["acknowledgments"] == 4
        logical_bytes += size
        before = sum(p.stat().st_size for s in run.ownership()["servers"] for p in Path(s["root"]).rglob("*") if p.is_file())
        cases.append({"kind": "storage", "size": size, "server_bytes_cumulative": before,
                      "logical_bytes_cumulative": logical_bytes, "storage_amplification": before / logical_bytes})
        for fault in ([0] if smoke else [0, 1, 2]):
            for server_id in range(4 - fault, 4):
                run.stop(server_id)
            try:
                for concurrency in ([1] if smoke else [1, 4, 8]):
                    rate = concurrency * 4
                    for repeat in range(repeats):
                        sides = ["trusted_codec", "bfr"] if repeat % 2 == 0 else ["bfr", "trusted_codec"]
                        for side in sides:
                            if side == "trusted_codec" and fault:
                                continue
                            count = 2 if smoke else max(32, int(rate * 10))
                            tag = f"{size}-{fault}-{concurrency}-{repeat}-{side}"
                            batch = run.scratch / tag
                            batch.mkdir()
                            epoch = time.perf_counter()
                            before_resources = resources(run)

                            def request(index):
                                scheduled = epoch + index / rate
                                time.sleep(max(0, scheduled - time.perf_counter()))
                                output = batch / f"out-{index}"
                                if side == "bfr":
                                    args = [sys.executable, str(ROOT / "scripts/bfr.py"), "--state", str(writer), "--servers", *run.urls(), "read", file_id, str(output), "--owner", owner]
                                else:
                                    args = [sys.executable, str(Path(__file__)), "--codec-source", str(source), "--codec-root", str(codec_root), "--codec-output", str(output)]
                                try:
                                    value = command(args, run.evidence / f"{tag}-{index}.usage")
                                except Exception as error:
                                    value = {"returncode": None, "timed_out": isinstance(error, TimeoutError), "error": str(error), "error_type": type(error).__name__}
                                value["scheduled_latency_seconds"] = time.perf_counter() - scheduled
                                value["index"] = index
                                target = output
                                value["correct"] = value["returncode"] == 0 and target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
                                value["expected_rejection"] = side == "bfr" and fault == 2 and expected_quorum_rejection(value) and not target.exists()
                                return value

                            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                                samples = list(pool.map(request, range(count)))
                            elapsed = time.perf_counter() - epoch
                            outcomes = load_outcomes(samples)
                            correct, errors = outcomes["completed"], outcomes["errors"]
                            case = {"kind": "reads", "size": size, "fault_servers": fault, "concurrency": concurrency,
                                    "offered_rate": rate, "repeat": repeat, "side": side, **outcomes, "elapsed_seconds": elapsed,
                                    "useful_bytes_per_second": size * correct / elapsed, "samples": samples,
                                    "owned_servers_before": before_resources, "owned_servers_after": resources(run)}
                            cases.append(case)
                            _atomic_json(run.evidence / "samples.json", cases)
                            assert errors == 0, case
                            assert correct == (0 if fault == 2 else count), case
                            shutil.rmtree(batch)
            finally:
                for server_id in range(4 - fault, 4):
                    run.start(server_id)
        for repeat in range(repeats):
            run.stop(3)
            missed = f"repair-{size}-{repeat}"
            started = time.perf_counter()
            written = run.cli(writer, "create", missed, str(source))
            write_seconds = time.perf_counter() - started
            assert written["acknowledgments"] == 3
            logical_bytes += size
            started = time.perf_counter()
            run.start(3)
            recovery = time.perf_counter() - started
            started = time.perf_counter()
            repaired = run.cli(writer, "repair", missed, "--target", "3", "--owner", owner)
            repair_time = time.perf_counter() - started
            assert 1 in repaired["repaired_versions"]
            result = run.scratch / f"repaired-{size}"
            run.cli(writer, "read", missed, str(result), "--owner", owner)
            assert hashlib.sha256(result.read_bytes()).hexdigest() == digest
            cases.append({"kind": "recovery", "size": size, "repeat": repeat, "one_down_create_seconds": write_seconds, "restart_ready_seconds": recovery, "repair_seconds": repair_time, "correct": True})

    source = run.scratch / f"input-{sizes[0]}"
    args = [sys.executable, "-m", "cProfile", "-o", str(run.evidence / "client-read.prof"), str(ROOT / "scripts/bfr.py"), "--state", str(writer), "--servers", *run.urls(), "read", f"sample-{sizes[0]}", str(run.scratch / "profile-output"), "--owner", owner]
    result = command(args, run.evidence / "profile.usage")
    assert result["returncode"] == 0
    assert (run.scratch / "profile-output").read_bytes() == source.read_bytes()
    profile_text = io.StringIO()
    pstats.Stats(str(run.evidence / "client-read.prof"), stream=profile_text).sort_stats("cumulative").print_stats(40)
    (run.evidence / "client-read-profile.txt").write_text(profile_text.getvalue(), encoding="utf-8")
    if not smoke:
        started = time.perf_counter()
        stable = []
        while time.perf_counter() - started < 60:
            output = run.scratch / "stable-output"
            sample = command([sys.executable, str(ROOT / "scripts/bfr.py"), "--state", str(writer), "--servers", *run.urls(), "read", f"sample-{sizes[0]}", str(output), "--owner", owner], run.evidence / "stability.usage")
            sample["correct"] = sample["returncode"] == 0 and output.read_bytes() == source.read_bytes()
            stable.append(sample)
            assert sample["correct"]
        cases.append({"kind": "stability", "policy": "closed-loop one outstanding CLI; saturation sweep above uses scheduled arrivals", "duration_seconds": time.perf_counter() - started, "offered": len(stable), "completed": len(stable), "errors": 0, "samples": stable})
    _atomic_json(run.evidence / "samples.json", cases)
    comparisons = []
    for size in sizes:
        for concurrency in ([1] if smoke else [1, 4, 8]):
            for side in ("trusted_codec", "bfr"):
                values = [c["useful_bytes_per_second"] for c in cases if c["kind"] == "reads" and c["size"] == size and c["concurrency"] == concurrency and c["fault_servers"] == 0 and c["side"] == side]
                comparisons.append({"size": size, "concurrency": concurrency, "side": side, "runs": len(values), "median_bytes_per_second": statistics.median(values), "range_bytes_per_second": [min(values), max(values)]})
    _atomic_json(run.evidence / "summary.json", {"status": "passed", "smoke_only": smoke, "comparisons": comparisons})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--codec-source", type=Path)
    parser.add_argument("--codec-root", type=Path)
    parser.add_argument("--codec-output", type=Path)
    parser.add_argument("--codec-prepare", action="store_true")
    args = parser.parse_args()
    if args.codec_source:
        trusted_codec(args.codec_source, args.codec_root, args.codec_output, args.codec_prepare)
        return
    if not args.run_id:
        parser.error("--run-id is required")
    assert shutil.disk_usage(ROOT).free >= 10 * 1024 ** 3
    run = Run(args.run_id)
    if run.path.exists():
        parser.error("choose a fresh run id; existing run state is never cleaned by this invocation")
    try:
        run.launch()
        run.doctor()
        _atomic_json(run.evidence / "environment.json", {"source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "python": sys.version, "packages": subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True).splitlines(), "platform": platform.platform(), "cpu_count": os.cpu_count(), "cpu": platform.processor(), "memory_bytes": psutil.virtual_memory().total, "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None, "seed": 1729, "repeats": 1 if args.smoke else 5, "cache": "warm server data and OS cache; fresh CLI per request", "baseline_scope": "trusted local disk same Tahoe n4/k2 codec reads, no HTTP/signature/quorum; not an equivalent security system", "command": sys.argv})
        benchmark(run, 1 if args.smoke else 5, args.smoke)
    except BaseException as error:
        _atomic_json(run.evidence / "benchmark-failure.json", {"status": "failed", "error_type": type(error).__name__, "error": str(error)[:1000]})
        raise
    finally:
        cleanup = run.cleanup()
        assert cleanup["passed"], cleanup


if __name__ == "__main__":
    main()
