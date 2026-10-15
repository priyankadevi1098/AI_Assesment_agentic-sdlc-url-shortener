# Implementation notes

Notes on choices made while building phases 5-8 (approval/execution/repair,
resume/replan/rollback, release evidence, and verification), for a reviewer
who wants to understand *why* something is shaped the way it is, not just
what it does.

## Why approval binds to a hash triple, not just the diff

Binding `ExecutionApproval` to `(candidate_hashes, requirement_hash,
plan_revision)` rather than only to the diff a human saw means three
independent things all have to stay frozen for an approval to remain valid:
the exact bytes about to run, the requirement those bytes were supposed to
satisfy, and which plan revision produced them. A model could otherwise
regenerate byte-identical files from a *different* requirement after a resume
and smuggle them past review under the same-looking diff; tying the hash to
the requirement closes that gap without the reviewer needing to notice.

## Why `invalidate_downstream` is table-driven, not ad hoc

A fixed `STAGE_ORDER` tuple plus one function that clears "this stage and
everything after it" was chosen over scattering invalidation logic across
each call site (e.g. "if requirement changes, also clear design, also clear
candidate_hashes, also clear..."). The single table is also the thing a test
(`test_upstream_change_invalidates_only_affected_descendants`) can assert
against directly, and the thing a future stage addition only has to touch in
one place.

## Why `run_implementation_stage` always clears prior approval

Staging writes new candidate files. Even if by coincidence the new bytes
hashed identically to a previous staged version (vanishingly unlikely, but
not provably impossible to rule out for a model-controlled response), always
clearing approval on (re)staging means the invariant "approval review happens
after the file you're about to run exists" never depends on a hash
coincidence not happening.

## Why validation.py's commands are injected via a `runner` parameter

Every test in `test_validation.py` that exercises `run_fixed_command`'s
timeout and bounded-output paths does so by injecting a fake `runner`, rather
than coaxing a real `subprocess.run` into timing out or overflowing a buffer.
The real subprocess path (default `runner=subprocess.run`) is still exercised
directly in `test_end_to_end.py`, where real compile/test/acceptance
validation against real generated files is the point of the test. This split
keeps the fast unit tests deterministic and the one place that needs a real
process actually uses one.

## Why acceptance checks shell out to a fresh interpreter instead of
`importlib`-ing the candidate in-process

Running `python -c "...; import app; ..."` as a subprocess, rather than
`importlib.import_module`-ing the candidate module into the test process,
keeps "model-generated code only ever executes after explicit approval, in
its own process" true even inside the validation/acceptance-check layer
itself — there is no path by which an acceptance check accidentally imports
untrusted code into the orchestrator's own long-lived process, where a
module-level side effect could affect later stages of the same run.

## Why the end-to-end fixture application is hand-written, not model output

`tests/test_end_to_end.py`'s `APP_SOURCE_V1` / `APP_SOURCE_V2_WITH_EXPIRATION`
are fixed strings in the test file, standing in for what a real backend would
return. The point of these tests is to prove the *pipeline* — staging,
hashing, approval, real compile/test, real independent acceptance checks,
brownfield preservation — behaves correctly end to end; a fixed, readable
fixture application makes the test's assertions about "existing links still
resolve after migration" legible to a reviewer, instead of also depending on
what a live model happens to produce on a given day.

## What "verified" means for CLI backends, repeated here on purpose

`docs/backend-setup.md` already states this, but it is worth repeating where
an implementer might look next: no CLI adapter in this codebase is backed by
a confirmed real-world non-interactive/inference-only invocation. Every
example in this project's tests and docs of a CLI or live-API backend
"running" is a deterministic fake transport, not a live call. Nothing in this
codebase should be read as a claim that a live Copilot/Claude Code/Codex CLI
or a live LLM API endpoint was exercised during this build.
