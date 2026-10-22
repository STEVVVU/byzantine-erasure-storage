# Repository guide

This checkout preserves Tahoe-LAFS upstream at `f002fd0d27d9a3da5c0da162b10725838353bd63`. Keep the upstream `README.rst`, source, history, and license files intact. The bounded BFR experiment is implemented separately under `bfr/`, `scripts/`, and `coursework/bfr_tests/`; it is not wired into Tahoe's grid APIs.

## Architecture and trust boundary

The BFR codec adapter uses Tahoe's CRSEncoder and CRSDecoder. The client performs owner-signed single-writer mutations and quorum reads across four loopback HTTP servers. The client state directory holds the owner's signing key, pending operations, and reader checkpoints. A dedicated per-file OS lock serializes checkpoint read/validate/write across client processes; it is separate from the mutation lock. Server roots hold signed metadata and erasure shares. Do not use real user files or non-loopback servers in verification. The model assumes one honest owner writer per file and a trusted client state directory; it does not claim full SUNDR fork consistency or Byzantine writer tolerance.

## Environment and checks

Use an isolated Python 3.11 environment, install the hash-locked runtime with `python -m pip install --require-hashes -r requirements.lock`, and set `PYTHONPATH=src` when invoking Tahoe modules. The Tahoe smoke checks are `python -m allmydata --version` and `python -m twisted.trial allmydata.test.test_codec`. The deterministic protocol tests are `python -m unittest discover -s coursework/bfr_tests -v`.

For real-product process verification, follow `.agents/skills/verify-bfr/SKILL.md`. The stable driver is `scripts/verify.py`; run `python scripts/verify.py all --run-id <unique-id>` from the repository root. It owns only loopback processes and its run-specific state. It records a terminal pass/fail verdict and preserves evidence after cleanup under `.agents/runs/<unique-id>/evidence/`. CI also runs its post-launch failure injection with a separate run ID to prove failure verdict and process cleanup.

The pinned, read-only CI entry points are push to `master`, pull request, and manual `workflow_dispatch` in `.github/workflows/bfr-checks.yml`.
