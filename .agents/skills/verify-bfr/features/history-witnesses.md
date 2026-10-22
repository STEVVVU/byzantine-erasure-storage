# History witnesses

## Sub-features

- Persist each reader's accepted signed owner-history head.
- Reject a validly signed history that does not extend that reader's saved checkpoint.
- Compare exported checkpoints to reveal different signed histories.
- Reject a missing server history if the reader still has a checkpoint for that file.
- Serialize checkpoint validation and persistence per file across threads and processes so stale concurrent observations cannot roll back the saved head.
- Reject repair metadata older than the saved reader checkpoint before sending any repair RPC.

## How to get to it (user POV)

Read using a pinned owner key and persistent reader state, then compare two reader checkpoints.

## Driving it with Python subprocesses

Run python scripts/verify.py drive --run-id <unique-id>. The verifier reads once, restarts the reader process, then exercises rollback and divergent-head checks using its four local servers. Deterministic tests force older-before-newer checkpoint interleavings within one process and across separate client processes.

## Gotchas

This per-owner witness is inspired by SUNDR. It is not full SUNDR fork consistency, and a new reader or lost trusted state cannot detect history omitted before its first checkpoint.
