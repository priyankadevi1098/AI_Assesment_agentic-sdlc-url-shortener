# Architecture

## Shape of a run

```
request.json --> analysis --> decomposition --> [design || risk_assessment] (join)
                                                        |
                                                        v
                                               implementation (file bundle)
                                                        |
                                                validate + stage into candidate/
                                                        |
                                         <<< manual execution approval (hash-bound) >>>
                                                        |
                                       real local compile + test + independent
                                           API acceptance checks (validation.py)
                                                        |
                                 pass -> <<< manual release-readiness approval >>>
                                 fail -> repair (new diff, invalidates approval,
                                          bounded attempts) or explicit failure
```

Design and risk-assessment run concurrently (`workflow.run_design_and_risk_join`,
two threads) and both must complete before `ensure_ready_for_implementation`
allows implementation to start — neither can produce the implementation
greenlight alone.

## Module map

| Module | Responsibility |
|---|---|
| `__main__.py` | CLI entry point: `run`, `show`, `resume`. Validates input, allocates a run directory, drives backend preflight/selection. |
| `request.py` | Validates the untrusted request document at the boundary; everything downstream trusts a `Request`. |
| `state.py` | Unique run directories; atomic (temp-file-then-replace) `state.json` persistence. |
| `backends.py` | Shared backend-selection contract: auto-discovery, explicit-preference tie-breaking, destination approval, mid-run-fallback prohibition, inference-only permission checks. |
| `cli_backends.py` | Copilot/Claude Code/Codex CLI adapters behind the shared protocol; each fails closed (`supported=False`) until its real non-interactive flags are verified (see `docs/backend-setup.md`). |
| `llm.py` | OpenAI-compatible HTTPS API adapter: HTTPS/loopback-HTTP config validation, bounded retries, output-size limit, sanitized errors. |
| `prompts.py` | One structured JSON task schema per stage, shared by every backend so adapters stay interchangeable. |
| `graph.py` | Validates a model-produced task graph: unique ids, legal stage types, acyclicity, and that every `implementation`/`release` task has a `governance_gate` in its transitive dependency chain. |
| `workflow.py` | Stage functions (`run_analysis_stage`, ... `run_implementation_stage`), the design/risk join gate, execution/release approval governance, repair-budget tracking, and resume/replan stage invalidation (`invalidate_downstream`, `apply_clarification_answers`, `apply_requirement_change`). |
| `workspace.py` | Bounded allowlisted brownfield reading (text only, never imported/executed); model file-bundle validation (absolute/traversal paths, symlinks, binary content, oversized changes, edits outside the candidate root); unified diffs and sha256 hashing; staging; pre-repair snapshot/restore. |
| `validation.py` | Approval binding to exact candidate hashes + requirement + plan revision; running *fixed, tool-owned* local commands with a timeout and bounded output capture (not a security boundary); independent, tool-owned API acceptance checks. |
| `report.py` | Append-only JSONL event log; honest metrics (success rate, retries, rollbacks, stage durations, MTTR reported as unavailable when nothing was recovered); JSON/Markdown run summaries. |

## Approval and repair state machine

A `workflow.WorkflowContext` carries `candidate_hashes`, `plan_revision`,
`execution_approval` and `release_approved`. The rules, enforced in code
rather than by convention:

- `request_execution_approval` binds a new `ExecutionApproval` to the context's
  *exact current* candidate hashes, a hash of the requirement text, and the
  plan revision. `ensure_execution_approved` re-verifies that triple on every
  check — change any one of them (a repair, an edited requirement, a replanned
  graph) and the prior approval no longer matches.
- `record_repair` always clears `execution_approval` and `validation_passed`
  and raises once the attempt count exceeds a bounded budget.
- `request_release_approval` requires both a current execution approval *and*
  a successful validation; it is a separate, later, human decision and never
  triggers a deploy or publish step.
- `invalidate_downstream` walks a fixed stage order
  (`analysis -> decomposition -> design -> risks -> candidate -> validation -> release`)
  and clears only the changed stage and everything after it, so unaffected
  upstream work (e.g. an unchanged `analysis`) survives a downstream repair or
  a resume-time answer that only affects design.

## Rollback

`workspace.snapshot_candidate` captures every file in the candidate workspace
before a repair is applied; `workspace.restore_snapshot` rewrites the
snapshotted content and deletes anything the repair added, if the repaired
candidate fails validation. This restores the *files on disk* to the last
approved state. It cannot undo side effects already caused by locally
executing generated code (network calls it made, files it wrote outside the
candidate workspace, etc.) — see `docs/security-and-limitations.md`.
