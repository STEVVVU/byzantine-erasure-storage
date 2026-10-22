# Quorum and recovery

## Sub-features

- Three acknowledgments are required for a mutation.
- Reads reconstruct from two authenticated shares of the selected version.
- A pending mutation can be retried identically after restart.
- A stale server can be repaired from peer shares.
- Stale metadata replies do not defeat a newer head witnessed by the required two servers; qualified same-version divergent heads are rejected.
- Local client mutations and server installations serialize per file; failed checkpoint persistence keeps the retry intent.
- Repair checks the saved reader witness before making any target-server change.

## How to get to it (user POV)

Use the same isolated `bfr` CLI against the four local disk-backed servers.

## Driving it with Python subprocesses

Run `python scripts/verify.py drive --run-id <unique-id>`. The driver stops and restarts recorded PIDs, tests the two-share threshold, produces an uncertain write and retries its exact pending intent, then repairs a server that missed updates. Unit tests cover a split latest/stale reply set, divergent witnessed heads, and an injected checkpoint-write failure.

## Gotchas

Do not interpret an uncertain result as an aborted operation. The trusted client state must be preserved until the identical retry reaches quorum.
