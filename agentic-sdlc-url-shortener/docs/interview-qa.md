# Interview Q&A

Short, direct answers to questions an interviewer reviewing this assessment
is likely to ask.

**Q: Why is generated code only run after a manual approval, instead of
automatically?**
A: The user explicitly declined Docker-level isolation for this build, and
generated code otherwise has the current user's full file/network/credential
access once run. A manual review-and-approve gate, bound to the exact bytes
about to execute, is the control that substitutes for sandboxing here. It is
documented as not equivalent to a sandbox (`docs/security-and-limitations.md`)
— it adds human judgment before execution, it does not contain what runs
after approval.

**Q: What stops approval from being reused after a repair changes the code?**
A: `ExecutionApproval` is bound to the exact candidate file hashes, a hash of
the requirement text, and the plan revision (`validation.py`,
`workflow.request_execution_approval`). `workflow.record_repair`
unconditionally clears the approval on the context whenever a repair is
recorded, and `ensure_execution_approved` re-verifies the full triple before
anything is allowed to run. A changed hash, requirement, or revision makes the
prior approval fail that check — see
`WorkflowTests.test_changed_candidate_revokes_execution_approval`.

**Q: Why are model-generated tests not treated as sufficient validation?**
A: A model can generate a test suite that technically passes while the
behavior it's supposed to prove is still broken (e.g. asserting an
unconditional truth instead of exercising the real function). This project
runs the model's own generated tests as one real signal, but also runs
independent, tool-authored acceptance checks (`validation.AcceptanceCheck` /
`run_acceptance_checks`) against the stated acceptance criteria, owned and
written by this tool, not the model. `ValidationTests.
test_generated_tests_cannot_mask_broken_redirect` demonstrates exactly this:
a cheating generated test reports success while the independent check
correctly reports failure against the same broken implementation.

**Q: How does resuming after a clarification or requirement change avoid
re-running everything?**
A: `workflow.invalidate_downstream` walks a fixed stage order (`analysis ->
decomposition -> design -> risks -> candidate -> validation -> release`) and
clears only the changed stage and everything after it; stages upstream of the
change are left exactly as they were, because their inputs are unchanged and
re-deriving them would add cost without changing their result. See
`WorkflowTests.test_upstream_change_invalidates_only_affected_descendants`.

**Q: What does rollback actually protect, and what does it not protect?**
A: `workspace.snapshot_candidate`/`restore_snapshot` protect the *files on
disk in the candidate workspace*: if a repair fails validation, the candidate
is restored exactly to its last approved snapshot rather than left in a
broken, half-repaired state, and a failed run never replaces an approved
baseline. It does not and cannot undo real-world side effects that already-run
generated code caused (network calls, writes outside the candidate
workspace). This boundary is stated explicitly, not implied, in
`docs/security-and-limitations.md`.

**Q: Why does MTTR sometimes say "unavailable" instead of a number?**
A: Because reporting a fabricated or estimated recovery time would be
dishonest telemetry. `report.compute_metrics` only computes a mean time to
recovery from events where a recorded failure has a matching, later recovery
event in the same run's log; if no failure was recovered, it reports
`mttr_available: false` and `mean_time_to_recovery_seconds: null` rather than
defaulting to zero or omitting the field silently.

**Q: Has this actually been tested against a live Copilot/Claude Code/Codex
CLI or a live LLM API?**
A: No. Every CLI backend adapter is covered by fixture/unit tests with a fake
subprocess runner. Copilot CLI reports itself unsupported on its own
documented evidence; Claude Code CLI and Codex CLI have a verified profile
built from each CLI's official public documentation, not a real installed
instance (`docs/backend-setup.md`). The `api` backend is additionally covered
end to end by `tests/test_cli_integration.py`, which drives the real
`py -3 -m sdlc_agent` CLI as a subprocess against a local loopback HTTP
fixture server — proving the full `--backend api` wiring without ever calling
a real network endpoint or LLM provider. This is stated plainly rather than
implied otherwise, per this project's own honesty requirement — see
`docs/security-and-limitations.md`'s "Honest reporting" section.

**Q: Why standard library only, no agent framework?**
A: This was an explicit constraint from the outset: keep the orchestrator
small and auditable, with explicit stage functions, a validated dependency
graph, and plain JSON state files, rather than depend on a third-party
agent-framework runtime whose internals the reviewer would also have to trust.

**Q: What would you add first if this moved past an assessment prototype?**
A: In priority order: (1) a live, authenticated run against Claude Code CLI or
Codex CLI — both have a verified, evidence-backed inference-only profile in
`cli_backends.py`/`docs/backend-setup.md`, but neither is installed in the
build environment, so today that profile rests on each CLI's official public
documentation rather than a real invocation (Copilot CLI stays unsupported on
its own documented evidence, not a gap to close); (2) a real sandbox/container
boundary around generated-code execution, replacing the manual-approval-only
control; (3) concurrency/robustness hardening for the generated SQLite-backed
service itself, which is intentionally minimal in this build. The generic
`workflow.run_graph(...)` driver that walks a validated `graph.TaskGraph`
end to end — analysis, decomposition, the design/risk join, implementation,
both approval gates, real local validation — is already implemented, wired
behind the CLI's `run`/`resume` subcommands, and proven by
`tests/test_workflow.py` and the subprocess-level
`tests/test_cli_integration.py`.
