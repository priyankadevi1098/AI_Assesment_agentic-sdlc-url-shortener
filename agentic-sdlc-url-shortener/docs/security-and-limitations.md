# Security and limitations

## Local execution is not sandboxed

Generated code is only ever executed after an explicit manual review and
execution approval (see `docs/architecture.md`), and only via a fixed,
tool-owned command list (`py -3 -m py_compile ...`, `py -3 -m unittest ...`
run through `validation.run_fixed_command`) — **never** a command taken from
model output. But once such a command starts, it runs with the full
privileges of the current user: file system access, network access, and any
credentials available in that environment. There is no Docker container, no
OS-level sandbox, and no network isolation in this build.

The process timeout and bounded output capture in `validation.py` exist to
keep *this tool's* bookkeeping finite — they stop this tool from hanging or
from buffering unbounded output. They are not a security boundary: a
malicious or buggy generated program can still do real damage to the local
machine within its timeout window, and a timeout does not retroactively undo
anything it already did.

**Only run this tool against requirements and (for brownfield) projects you
trust**, in an environment where you accept that risk. Do not point it at
production credentials, production data, or a machine you cannot afford to
have modified by an untrusted program.

## Rollback scope

`workspace.snapshot_candidate` / `restore_snapshot` rewrite the *files in the
candidate workspace* back to their last snapshotted state when a repair fails
validation. This is file-level rollback only. It cannot:

- Undo a network call generated code already made.
- Undo a write to a file outside the candidate workspace.
- Undo any other real-world side effect of code that has already run.

A failed run never replaces an approved baseline — but "never replaces" means
the *file contents this tool staged*, not every effect the code may have had
while it ran.

## Audit log is local evidence, not tamper-proof compliance storage

`report.append_event` writes an append-only JSONL log to a local file. It is
useful, traceable evidence of what this tool did and when. It is **not**
cryptographically signed, write-once at the storage layer, or protected
against a user (or another program with file access) editing the file
afterward. Do not represent it as tamper-proof compliance evidence without
additional controls this project does not implement.

## Backend data transfer

Selecting any backend other than a fully air-gapped local one transmits the
requirement text and (for brownfield) the brownfield project's allowlisted
source to that backend. See `docs/backend-setup.md` for exactly what is sent,
and the mandatory destination-approval prompt before the first transmission.
API credentials are read only from environment variables; this tool never
asks for, logs, or displays a credential, and sanitizes credentials out of
every error message it raises (`llm.py`).

## Honest reporting

- MTTR (`report.compute_metrics`) is reported as unavailable
  (`mttr_available: false`, `mean_time_to_recovery_seconds: null`) whenever no
  recorded failure has a matching recovery event — never estimated, never
  defaulted to zero.
- CLI backend adapters report themselves unsupported until their real,
  version-specific non-interactive flags have been verified against an
  installed CLI (see `docs/backend-setup.md`); an installed executable alone
  is never treated as proof of a usable, permission-restricted assistant.
- A live LLM/CLI smoke test is only ever reported as having happened if it
  actually ran against a real, user-configured endpoint in this environment.
  Where that did not happen, this project's docs and run output say so
  explicitly (`unverified`), rather than imply broader testing occurred.

## Scope boundaries

- This tool reasons about URL-shortener-shaped greenfield/brownfield Python
  projects produced by itself, by design — it is not a general-purpose,
  arbitrary-repository code agent.
- No model-generated shell command is ever executed. No deployment, no
  publish/GitHub-upload step, and no production-data access are in scope.
- Concurrency, scale, and abuse controls for the *generated* URL-shortener
  service itself are intentionally minimal (SQLite + the standard-library
  HTTP server) to keep the prototype explainable; this is documented, not
  hidden.
