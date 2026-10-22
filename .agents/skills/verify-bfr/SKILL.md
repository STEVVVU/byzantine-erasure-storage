---
name: verify-bfr
description: Drive the BFR storage CLI and four loopback server processes; use to verify file operations, quorum faults, repair, or reader history.
---

# Verify the BFR storage experiment

For the maintained benchmark path and its frozen workload, follow [features/metrics.md](features/metrics.md). The manual `metrics.yml` workflow runs it on an isolated Ubuntu runner. Current accepted metrics cover the 4 KiB profile and stability stage; the added 128 KiB and 1 MiB diagnostic stages remain draft coverage until a hosted run completes.

The verifier launches only its own four loopback server processes and uses a fresh per-run state directory. Never point a run at a real Tahoe node or user data.

## Launch

Run `python scripts/verify.py launch --run-id <unique-id>`. The driver starts four independent disk-backed servers on loopback and records their PIDs and roots under `.agents/runs/<run-id>/`. It waits for each server's read-only `/health` response. Run from the repository root with the isolated Python 3.11 environment installed from `requirements.lock`.

## Doctor

Run `python scripts/verify.py doctor --run-id <unique-id>`. It checks each recorded PID, verifies that the owned process answers on its assigned loopback port, and confirms the expected protocol version. It does not change server data.

## Drive

Run `python scripts/verify.py drive --run-id <unique-id>`. This uses the real `bfr` command interface and the servers' JSON-over-HTTP RPCs with generated files and temporary state. It covers normal round trips, quorum faults, uncertain retry, restart, repair, and persisted reader history. Never use externally sourced files or non-loopback addresses.

## Evidence

Run `python scripts/verify.py evidence --run-id <unique-id>`. Preserve `evidence.json`, per-server logs, and the command transcript under `.agents/runs/<run-id>/`. Evidence records request results, file hashes, process identities, and disk state; successful states are checked after the client exits.

## Cleanup

Run `python scripts/verify.py cleanup --run-id <unique-id>`. It terminates only PIDs recorded by Launch, waits for those PIDs to exit, and removes only that run's server roots and scratch state. It preserves the evidence directory. After cleanup, check that `evidence.json` remains and every recorded process has exited.

## Helpers

`python scripts/verify.py --help` lists supported phases and options. The same driver is used by local verification and Linux CI. A full run is `python scripts/verify.py all --run-id <unique-id>`.

The negative-control run is `python scripts/verify.py all --run-id <unique-id> --inject-failure-after-launch`. It intentionally exits nonzero after starting the four servers. Its evidence must show a terminal verdict with `passed: false`, successful cleanup, no owned processes alive, and retained evidence. CI runs this with a separate run ID from the successful journey.
