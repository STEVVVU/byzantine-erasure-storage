# Byzantine erasure storage

This repository preserves the full Tahoe-LAFS source tree and Git history at upstream revision [`f002fd0d27d9a3da5c0da162b10725838353bd63`](https://github.com/tahoe-lafs/tahoe-lafs/tree/f002fd0d27d9a3da5c0da162b10725838353bd63). The original [Tahoe README](README.rst), source files, and both upstream license texts remain in place. The coursework code is a separate, bounded protocol experiment under [`bfr/`](bfr/) that imports Tahoe's `CRSEncoder` and `CRSDecoder`; it does not claim to modify Tahoe's grid protocol.

The experiment stores one signed file history across four disk-backed loopback servers. With `n=4`, `b=1`, and `f=2`, it uses two Tahoe erasure shares to reconstruct a stripe, requires three distinct acknowledgments for a mutation, and requires three metadata responses with two matching signed heads. Each owner signs its version history and each stripe's four share digests. The reader verifies those signatures and pins the accepted history in its own persistent state.

## Run it

Use Python 3.11 and install the hash-locked requirements in an isolated environment:

```sh
python -m pip install --require-hashes -r requirements.lock
```

Start four separate servers in four terminals, replacing each storage path with a directory you control:

```sh
python scripts/bfr.py server --root ./data/server-0 --port 17800 --share-id 0
python scripts/bfr.py server --root ./data/server-1 --port 17801 --share-id 1
python scripts/bfr.py server --root ./data/server-2 --port 17802 --share-id 2
python scripts/bfr.py server --root ./data/server-3 --port 17803 --share-id 3
```

Create an owner key, create a file, and read it back:

```sh
python scripts/bfr.py --state ./data/owner keygen
python scripts/bfr.py --state ./data/owner create sample ./input.bin
python scripts/bfr.py --state ./data/owner read sample ./output.bin
python scripts/bfr.py --state ./data/owner list
python scripts/bfr.py --state ./data/owner put sample ./replacement.bin
python scripts/bfr.py --state ./data/owner delete sample
```

If a write reports an `uncertain` outcome, keep the client state and run `retry` for that file. It resends the exact signed update and share bytes saved before the first send. Do not start another mutation for that file until the retry resolves.

An independent reader uses its own state directory and pins the owner's `owner.pub` file with `trust-owner`. A reader checkpoint detects a server history rolled back behind the last accepted signed version. `export-checkpoint` and `audit-checkpoints` compare two readers' signed histories.

To repair a server that missed recent updates, run `repair FILE --target N --owner OWNER_ID` after it is back online. Repair reconstructs its recent retained shares from two authenticated peer shares and checks the result against the original owner's signed digest before storing it.

## Verify

Run the complete native process journey with:

```sh
python scripts/verify.py all --run-id local-check
```

It starts four loopback-only servers with temporary disk roots, runs the CLI as an owner and independent reader, exercises quorum faults and restarts, then stops only those recorded processes. JSON evidence and server transcripts remain under `.agents/runs/local-check/evidence/` after scratch data is removed. The project-local [verification skill](.agents/skills/verify-bfr/SKILL.md) documents the individual Launch, Doctor, Drive, Evidence, and Cleanup phases.

The pinned GitHub workflow repeats the Tahoe CLI and codec checks, deterministic protocol checks, and the same process journey on Ubuntu. Its artifact contains the server logs and evidence for each run.

The native Windows journey and its post-launch failure control have passed locally. Both retained terminal verdicts and successful cleanup of their four owned processes. Hosted Linux verification is pending; local success is not a Linux result.

## Coursework and research

The bounded protocol follows the quorum and erasure-storage ideas in the [Stanford CS244b Byzantine RAID project](https://www.scs.stanford.edu/24sp-cs244b/projects/Byzantine_Fault-tolerant_RAID-like_Filesystem.pdf). Persistent reader checkpoints and explicit history comparison adapt part of [SUNDR's signed-history approach](https://www.usenix.org/legacy/events/osdi04/tech/full_papers/li_j/li_j.pdf). These mechanisms are separate from Tahoe's native grid protocol and do not establish the full guarantees of either paper.

## Protocol and limits

- A mutation is a full-file version up to 1 MiB. Each file can have at most 256 signed metadata versions; servers retain share bytes for the latest eight versions.
- Writers are single-owner and use one trusted client state at a time. A saved local signed history allows that owner to form an update when fewer than three metadata servers answer; the write remains explicitly uncertain until three servers acknowledge the identical operation.
- Readers need a metadata quorum before selecting a version. They then accept only two distinct shares matching that version's owner-signed manifest. Repair re-encodes the same deterministic Tahoe shares and retains the original signatures.
- The checkpoint exchange is a small SUNDR-inspired history witness. It detects rollback for a reader that still has trusted state and can report divergent exported histories. It is not full SUNDR fork consistency or multi-writer linearizability.
- The experiment uses plain HTTP bound to literal loopback addresses. Server files and owner private keys are stored unencrypted. It does not provide confidentiality, authenticated owner enrollment, a production security audit, a FUSE mount, or changes to Tahoe's native grid interfaces.

Tahoe-LAFS is provided under its existing GPL-2.0-or-later or TGPPL-1.0 licensing terms; consult [COPYING.GPL](COPYING.GPL), [COPYING.TGPPL.rst](COPYING.TGPPL.rst), and the notices in the upstream files before redistribution.
