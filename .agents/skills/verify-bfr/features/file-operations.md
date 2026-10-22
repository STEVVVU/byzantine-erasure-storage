# File operations

## Sub-features

- Create/write a synthetic file as a new signed version.
- Read/list/delete a file through the client command line.
- Open from a separate reader state after pinning the owner's public key.

## How to get to it (user POV)

Launch the local server set, then invoke the `bfr` CLI with the isolated state directory and loopback server configuration.

## Driving it with Python subprocesses

Run `python scripts/verify.py drive --run-id <unique-id>`. The driver uses non-aligned multi-stripe input, verifies the independent reader's SHA-256, and checks the signed tombstone and recreated file version.

## Gotchas

This is a bounded single-owner experiment, not a mounted POSIX filesystem or a retrofit of Tahoe's grid RPCs.
