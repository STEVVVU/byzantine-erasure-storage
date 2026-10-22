#!/usr/bin/env python3
"""Compare baseline and candidate BFR reads with native Hyperfine timings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify import Run, _atomic_json

BASELINE = "e1ce5826e82810f4154d0d7ced6906bb570ba963"
HYPERFINE_VERSION = "2.0.0"
SIZES = (4096, 131072, 1048576)
SHARED_FILES = ("bfr/server.py", "bfr/model.py", "bfr/codec.py", "src/allmydata/codec.py",
                "bfr/__init__.py", "scripts/bfr.py",
                "requirements.lock", "pyproject.toml")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_hyperfine(binary: Path, command: list[str], raw: Path) -> dict:
    command_text = " ".join(shlex.quote(part) for part in command)
    argv = [str(binary), "--shell=none", "--runs", "1", "--warmup", "0",
            "--style", "basic", "--metrics", "time_wall_clock,time_cpu,time_user,time_system,memory_peak_resident",
            "--command-name", "bfr-read", "--export-json", str(raw), command_text]
    process = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=(os.name == "posix"))
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        raise TimeoutError(f"Hyperfine invocation exceeded 60 seconds: {command_text}")
    if process.returncode:
        raise RuntimeError(f"Hyperfine exited {process.returncode}: {stderr.decode(errors='replace')[-2000:]}")
    if not raw.is_file():
        raise RuntimeError("Hyperfine did not write its native JSON export")
    result = json.loads(raw.read_text(encoding="utf-8"))
    if result.get("schema_version") != 2 or result.get("metadata", {}).get("hyperfine_version") != HYPERFINE_VERSION:
        raise RuntimeError("unexpected Hyperfine JSON schema or version")
    measured = result["results"][0]["measurements"]
    if len(measured) != 1 or measured[0].get("exit_code") != 0:
        raise RuntimeError("timed CLI invocation failed or Hyperfine did not retain one measurement")
    return {"argv": argv, "stdout": stdout.decode(errors="replace")[-1000:],
            "stderr": stderr.decode(errors="replace")[-1000:], "json": result,
            "wall_seconds": measured[0]["time_wall_clock"]["value"]}


def cli_command(checkout: Path, state: Path, urls: list[str], file_id: str,
                owner: str, output: Path) -> list[str]:
    return [sys.executable, str(checkout / "scripts" / "bfr.py"), "--state", str(state),
            "--servers", *urls, "read", file_id, str(output), "--owner", owner]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--hyperfine", type=Path, required=True)
    args = parser.parse_args()
    if shutil.disk_usage(ROOT).free < 10 * 1024**3:
        parser.error("at least 10 GiB free disk space is required")
    binary = args.hyperfine.resolve(strict=True)
    if subprocess.check_output([str(binary), "--version"], text=True).strip() != f"hyperfine {HYPERFINE_VERSION}":
        parser.error(f"expected hyperfine {HYPERFINE_VERSION}")
    candidate_sha = git("rev-parse", "HEAD")
    if candidate_sha == BASELINE or git("status", "--porcelain"):
        parser.error("candidate must be a distinct, clean published commit")
    if subprocess.run(["git", "diff", "--quiet", f"{BASELINE}..{candidate_sha}", "--", *SHARED_FILES],
                      cwd=ROOT).returncode:
        parser.error("baseline and candidate server, model, protocol, CLI, and runtime dependency files must match")

    run = Run(args.run_id)
    if run.path.exists():
        parser.error("choose a fresh run id; existing run evidence is never overwritten")
    baseline_tree = run.scratch / "baseline-worktree"
    added_worktree = False
    report = None
    try:
        run.launch()
        run.doctor()
        baseline_tree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "--detach", str(baseline_tree), BASELINE],
                       cwd=ROOT, check=True, timeout=120, capture_output=True, text=True)
        added_worktree = True
        writer = run.scratch / "writer"
        owner = run.cli(writer, "keygen")["owner"]
        state = run.scratch / "reader"
        run.cli(state, "trust-owner", str(writer / "owner.pub"))
        urls = run.urls()
        cases = []
        warmups = []
        run.evidence.mkdir(parents=True, exist_ok=True)
        for size in SIZES:
            file_id = f"read-{size}"
            source = run.scratch / f"input-{size}.bin"
            expected = bytes((i * 37 + 11) % 251 for i in range(size))
            source.write_bytes(expected)
            created = run.cli(writer, "create", file_id, str(source))
            if created["acknowledgments"] < 3:
                raise AssertionError("baseline/candidate shared file did not reach write quorum")
            expected_sha = hashlib.sha256(expected).hexdigest()
            arms = (("baseline", baseline_tree), ("candidate", ROOT))
            for name, checkout in arms:
                output = run.scratch / f"warm-{name}-{size}.bin"
                call = cli_command(checkout, state, urls, file_id, owner, output)
                warm = subprocess.run(call, cwd=ROOT, capture_output=True, text=True, timeout=60)
                if warm.returncode or not output.is_file() or output.read_bytes() != expected:
                    raise AssertionError(f"{name} warmup read failed correctness: {warm.stderr[-1000:]}")
                warmups.append({"size_bytes": size, "arm": name, "command": call,
                                "source_sha256": expected_sha, "sha256": digest(output),
                                "returncode": warm.returncode})
                _atomic_json(run.evidence / "comparison-progress.json", {
                    "status": "running", "warmups": warmups, "cases": cases})
            repeat_seconds = {"baseline": [], "candidate": []}
            for repeat in range(5):
                order = arms if repeat % 2 == 0 else tuple(reversed(arms))
                for name, checkout in order:
                    output = run.scratch / f"{name}-{size}-{repeat}.bin"
                    raw = run.evidence / f"hyperfine-{size}-{repeat}-{name}.json"
                    command = cli_command(checkout, state, urls, file_id, owner, output)
                    measured = run_hyperfine(binary, command, raw)
                    actual = output.read_bytes() if output.is_file() else None
                    actual_sha = hashlib.sha256(actual).hexdigest() if actual is not None else None
                    if actual != expected:
                        raise AssertionError(f"{name} returned wrong/missing bytes for {size} bytes")
                    repeat_seconds[name].append(measured["wall_seconds"])
                    cases.append({"size_bytes": size, "repeat": repeat, "arm": name,
                                  "source_sha256": expected_sha, "sha256": actual_sha,
                                  "command": command,
                                  "hyperfine_json": str(raw.relative_to(run.path)),
                                  "hyperfine_argv": measured["argv"],
                                  "hyperfine_json_sha256": digest(raw),
                                  "wall_seconds": measured["wall_seconds"]})
                    _atomic_json(run.evidence / "comparison-progress.json", {
                        "status": "running", "warmups": warmups, "cases": cases})
                    output.unlink()
            cases.append({"size_bytes": size, "summary": {
                name: {"runs": len(values), "median_seconds": statistics.median(values),
                       "range_seconds": [min(values), max(values)]}
                for name, values in repeat_seconds.items()}})
        version = subprocess.check_output([str(binary), "--version"], text=True).strip()
        lock_sha = digest(ROOT / "requirements.lock")
        report = {
            "status": "passed", "baseline_sha": BASELINE, "candidate_sha": candidate_sha,
            "source_hashes": {"baseline_commit": BASELINE, "candidate_commit": candidate_sha,
                              "requirements_lock_sha256": lock_sha,
                              "shared_implementation": {name: digest(ROOT / name) for name in SHARED_FILES}},
            "hyperfine_version": version, "hyperfine_path": str(binary),
            "build_model": "source Python CLI; no binary build",
            "python": sys.version, "platform": platform.platform(), "cpu_count": os.cpu_count(),
            "cpu": platform.processor(), "memory_bytes": __import__("psutil").virtual_memory().total,
            "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
            "runtime_packages": subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True).splitlines(),
            "warmup": "one successful real CLI read per arm and size outside Hyperfine timing",
            "ordering": "five alternating pairs; baseline first on even repeats, candidate first on odd repeats",
            "concurrency": 1, "checkpoint": "one shared reader state; unchanged baseline checkpoint semantics",
            "input": "(i * 37 + 11) % 251; 4096, 131072, and 1048576 bytes",
            "output_validation": "SHA-256 compared with source after every warmup and timed CLI invocation",
            "timing": "native Hyperfine wall clock around complete CLI invocation, including startup and fsynced output; separate profile excluded",
            "metrics": "Hyperfine native time, CPU, user, system, and peak resident memory selections; platform omissions retained in raw JSON",
            "warmups": warmups, "cases": cases}
    except BaseException as error:
        _atomic_json(run.evidence / "comparison-failure.json", {
            "status": "failed", "type": type(error).__name__, "error": str(error)[:2000],
            "baseline_sha": BASELINE, "candidate_sha": candidate_sha,
            "progress": str((run.evidence / "comparison-progress.json").relative_to(run.path))})
        raise
    finally:
        worktree_error = None
        if added_worktree or baseline_tree.exists():
            try:
                subprocess.run(["git", "worktree", "remove", "--force", str(baseline_tree)],
                               cwd=ROOT, check=True, timeout=120, capture_output=True, text=True)
            except BaseException as error:
                worktree_error = f"{type(error).__name__}: {error}"
        cleanup = run.cleanup()
        if report is not None:
            report["cleanup"] = cleanup
            report["worktree_cleanup_error"] = worktree_error
            report["status"] = "passed" if cleanup["passed"] and worktree_error is None else "failed"
            _atomic_json(run.evidence / "comparison.json", report)
        if not cleanup["passed"] or worktree_error is not None:
            raise RuntimeError(f"owned cleanup failed; worktree={worktree_error}; servers={cleanup}")


if __name__ == "__main__":
    main()
