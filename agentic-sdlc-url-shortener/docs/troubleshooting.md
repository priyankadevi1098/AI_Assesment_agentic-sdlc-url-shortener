# Troubleshooting

## "request rejected: <field>: ..."

The request JSON failed `request.parse_request` validation. The message names
the offending field (`name`, `mode`, `requirement`, `acceptance_criteria`, or
`answers`). Fix that field in your request file — nothing downstream
re-validates, so the message is always precise about what to change.

## "backend selection failed: multiple compatible backends are available"

More than one CLI adapter reported itself usable under `--backend auto` and
no `--backend-preference` was given. The tool will not silently choose which
assistant/provider receives your requirement or source — pass
`--backend-preference copilot|claude-code|codex`, or pick an explicit
`--backend` value instead of `auto`.

## "backend selection failed: ... is not usable: ..."

An explicitly requested CLI backend (or `api`) failed preflight. For a CLI,
this almost always means its non-interactive, inference-only flags have not
yet been verified in this codebase (see `docs/backend-setup.md` — the
`VERIFIED_CLI_CAPABILITIES` registry starts empty on purpose). For `api`, it
means `LLM_MODEL`/`LLM_API_KEY`/`LLM_BASE_URL` are not correctly configured in
the environment — see the table in `docs/backend-setup.md`.

## "destination not approved"

You (or a script running non-interactively without `--yes`) declined, or were
never asked, to approve sending content to the selected backend. This is
intentional: requirement/source text is a data transfer to that destination,
and it requires an explicit yes.

## Execution approval keeps getting invalidated

`ensure_execution_approved` re-verifies the exact candidate file hashes, the
requirement hash, and the plan revision on every check. If any of the
following changed since you approved execution, a fresh approval is required
— this is deliberate, not a bug:

- A repair produced a new diff (`workflow.record_repair` always clears the
  prior approval).
- The requirement changed on resume (`apply_requirement_change`).
- Clarification answers were supplied on resume
  (`apply_clarification_answers`).

Re-run `show` to inspect the new diff/candidate, then approve again.

## "repair budget of N attempts exhausted"

`workflow.record_repair` enforces a bounded number of repair attempts per run
(`DEFAULT_MAX_REPAIR_ATTEMPTS` in `workflow.py`). This stops a stuck
model/backend loop from iterating forever. Once exhausted, the run reports an
explicit failure; it never falls back to a canned "success".

## A compile/test command timed out

`validation.run_fixed_command` reports `timed_out=True` and `exit_code=None`
rather than hanging indefinitely. Remember: the timeout bounds this tool's own
bookkeeping, not the safety of what the command already did while it ran — see
`docs/security-and-limitations.md`.

## MTTR shows "unavailable"

This is correct, not a defect, whenever no recorded failure in a run's event
log has a matching recovery event. `report.compute_metrics` never estimates or
defaults MTTR to zero; it reports it as unavailable so a real recovery-time
measurement and "we never had to recover" are never confused in a summary.

## Running the test suite

```powershell
py -3 -m unittest discover -s tests -q
py -3 -m compileall -q sdlc_agent
```

`tests/test_end_to_end.py` runs real `py -3` subprocess compile/test/acceptance
checks against generated files on disk (via a deterministic fake model
transport) — if it fails, check that `py -3` on `PATH` resolves to a Python
3.11+ interpreter, since the generated candidate code targets that version.
