"""Tests for executable orchestration (workflow.py)."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sdlc_agent.graph import Task, TaskGraph
from sdlc_agent.state import RunState, load_state, save_state
from sdlc_agent.validation import AcceptanceCheck
from sdlc_agent.workflow import (
    STATUS_AWAITING_EXECUTION_APPROVAL,
    STATUS_AWAITING_RELEASE_APPROVAL,
    STATUS_COMPLETED,
    STATUS_VALIDATION_FAILED,
    WorkflowContext,
    WorkflowError,
    apply_clarification_answers,
    apply_requirement_change,
    build_default_graph,
    context_from_dict,
    context_to_dict,
    ensure_execution_approved,
    ensure_ready_for_implementation,
    ensure_release_approved,
    invalidate_downstream,
    record_repair,
    request_execution_approval,
    request_release_approval,
    run_analysis_stage,
    run_decomposition_stage,
    run_design_and_risk_join,
    run_graph,
    run_implementation_stage,
)


class WorkflowTests(unittest.TestCase):
    def _context(self) -> WorkflowContext:
        return WorkflowContext(
            requirement="Build a URL shortener.",
            acceptance_criteria=("Creates short links.",),
            mode="greenfield",
        )

    def test_design_and_risk_join_before_implementation(self):
        context = self._context()
        context.analysis = {"summary": "ok", "key_requirements": []}
        context.decomposition = {"tasks": []}

        # Before the join: implementation must not be considered ready.
        with self.assertRaises(WorkflowError):
            ensure_ready_for_implementation(context)

        in_flight = []
        lock = threading.Lock()

        def fake_generate(task_payload, schema):
            with lock:
                in_flight.append(task_payload["task"])
                both_running = len(in_flight) == 2
            # Give the other stage a chance to start before this one returns,
            # proving design and risk run concurrently rather than serially.
            time.sleep(0.05)
            if task_payload["task"] == "design":
                return {"design": "layered service"}
            return {"risks": ["sqlite contention under load"]}

        persisted = []
        run_design_and_risk_join(context, fake_generate, persist=persisted.append)

        self.assertEqual(set(in_flight), {"design", "risk_assessment"})
        self.assertEqual(context.design, {"design": "layered service"})
        self.assertEqual(context.risks, {"risks": ["sqlite contention under load"]})
        self.assertEqual(len(persisted), 1)

        # After the join: implementation is ready.
        ensure_ready_for_implementation(context)

    def test_decomposition_requires_completed_analysis(self):
        context = self._context()
        with self.assertRaises(WorkflowError):
            run_decomposition_stage(context, generate=lambda *_: {"tasks": []}, persist=lambda ctx: None)

    def test_design_and_risk_require_completed_decomposition(self):
        context = self._context()
        context.analysis = {"summary": "ok", "key_requirements": []}
        with self.assertRaises(WorkflowError):
            run_design_and_risk_join(context, generate=lambda *_: {}, persist=lambda ctx: None)

    def test_analysis_stage_persists_context(self):
        context = self._context()
        persisted = []

        def fake_generate(task_payload, schema):
            return {"summary": "ok", "key_requirements": ["short links"]}

        run_analysis_stage(context, fake_generate, persist=persisted.append)

        self.assertEqual(context.analysis["summary"], "ok")
        self.assertEqual(len(persisted), 1)

    # --- Approval, execution and repair governance --------------------------

    def _fully_staged_context(self) -> WorkflowContext:
        context = self._context()
        context.candidate_hashes = {"app.py": "hash-v1"}
        return context

    def test_no_execution_before_approval(self):
        context = self._fully_staged_context()
        with self.assertRaises(Exception):
            ensure_execution_approved(context)

        request_execution_approval(context)
        ensure_execution_approved(context)  # now approved: does not raise

    def test_release_requires_separate_approval(self):
        context = self._fully_staged_context()

        # Release cannot be approved before execution is approved and validated.
        with self.assertRaises(WorkflowError):
            request_release_approval(context)

        request_execution_approval(context)
        with self.assertRaises(WorkflowError):
            request_release_approval(context)  # execution approved, but not yet validated

        context.validation_passed = True
        request_release_approval(context)
        ensure_release_approved(context)  # does not raise

    def test_changed_candidate_revokes_execution_approval(self):
        context = self._fully_staged_context()
        request_execution_approval(context)
        ensure_execution_approved(context)

        # A repair changes the candidate's hashes: the prior approval no longer matches.
        context.candidate_hashes = {"app.py": "hash-v2"}
        with self.assertRaises(Exception):
            ensure_execution_approved(context)

    def test_repair_budget_exhaustion_fails(self):
        context = self._fully_staged_context()
        request_execution_approval(context)

        record_repair(context, max_attempts=2)
        self.assertIsNone(context.execution_approval)
        request_execution_approval(context)
        record_repair(context, max_attempts=2)

        with self.assertRaises(WorkflowError):
            record_repair(context, max_attempts=2)

    # --- Resume, replan and recovery -----------------------------------------

    def _context_with_all_stages_complete(self) -> WorkflowContext:
        context = self._fully_staged_context()
        context.analysis = {"summary": "ok", "key_requirements": []}
        context.decomposition = {"tasks": []}
        context.design = {"design": "layered service"}
        context.risks = {"risks": []}
        context.validation_passed = True
        context.release_approved = True
        return context

    def test_upstream_change_invalidates_only_affected_descendants(self):
        context = self._context_with_all_stages_complete()

        invalidated = invalidate_downstream(context, "design")

        self.assertEqual(invalidated, ["design", "risks", "candidate", "validation", "release"])
        # Unaffected upstream stages are retained because their inputs are unchanged.
        self.assertEqual(context.analysis, {"summary": "ok", "key_requirements": []})
        self.assertEqual(context.decomposition, {"tasks": []})
        # Affected descendants are cleared.
        self.assertEqual(context.design, {})
        self.assertEqual(context.risks, {})
        self.assertEqual(context.candidate_hashes, {})
        self.assertFalse(context.validation_passed)
        self.assertFalse(context.release_approved)

    def test_clarification_answers_resume_invalidated_stages(self):
        context = self._context_with_all_stages_complete()
        request_execution_approval(context)

        invalidated = apply_clarification_answers(context, {"storage": "sqlite"})

        self.assertEqual(context.answers, {"storage": "sqlite"})
        self.assertEqual(invalidated, list(context.invalidated_stages))
        self.assertIn("analysis", invalidated)
        self.assertEqual(context.analysis, {})
        self.assertEqual(context.decomposition, {})
        self.assertIsNone(context.execution_approval)

    def test_upstream_requirement_change_invalidates_only_affected_descendants(self):
        context = self._context_with_all_stages_complete()
        original_revision = context.plan_revision

        invalidated = apply_requirement_change(context, "Build a URL shortener with expiration.")

        self.assertEqual(context.requirement, "Build a URL shortener with expiration.")
        self.assertEqual(context.plan_revision, original_revision + 1)
        self.assertIn("analysis", invalidated)
        self.assertEqual(context.analysis, {})

    def test_run_implementation_stage_stages_generated_files_and_clears_approval(self):
        import tempfile
        from pathlib import Path

        context = self._context()
        context.decomposition = {"tasks": []}
        context.design = {"design": "layered service"}
        context.risks = {"risks": []}
        context.execution_approval = object()  # any prior approval must be cleared by (re)staging

        def fake_generate(task_payload, schema):
            self.assertEqual(task_payload["task"], "implementation")
            return {"files": [{"relative_path": "app.py", "content": "print('hello')"}]}

        persisted = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_implementation_stage(context, fake_generate, Path(tmp_dir), persist=persisted.append)
            self.assertEqual((Path(tmp_dir) / "app.py").read_text(encoding="utf-8"), "print('hello')")

        self.assertIn("app.py", context.candidate_hashes)
        self.assertIsNone(context.execution_approval)
        self.assertEqual(len(persisted), 1)

    def test_run_implementation_stage_requires_design_and_risk_join(self):
        context = self._context()
        with self.assertRaises(WorkflowError):
            run_implementation_stage(context, generate=lambda *_: {}, candidate_root=".", persist=lambda ctx: None)


def _fake_generate(task_payload, schema):
    """Deterministic scripted transport, keyed by task type, for run_graph tests."""
    task = task_payload["task"]
    if task == "analysis":
        return {"summary": "URL shortener", "key_requirements": ["shorten", "redirect"]}
    if task == "decomposition":
        return {"tasks": [{"task_id": "impl", "stage_type": "implementation"}]}
    if task == "design":
        return {"design": "stdlib sqlite3-backed URL shortener"}
    if task == "risk_assessment":
        return {"risks": ["sqlite write contention under concurrent load"]}
    if task == "implementation":
        return {"files": [{"relative_path": "app.py", "content": "print('ok')\n"}]}
    raise AssertionError(f"unscripted task: {task!r}")


def _full_pipeline_graph() -> TaskGraph:
    """A graph exercising every stage type ``run_graph`` understands — the
    same canonical pipeline the CLI builds via ``workflow.build_default_graph``,
    with the governance gates the real ``graph.TaskGraph`` validator requires
    upstream of ``implementation`` and ``release``."""
    return build_default_graph()


class RunGraphTests(unittest.TestCase):
    """Tests proving the generic graph driver itself sequences, pauses and
    resumes a validated ``graph.TaskGraph`` end-to-end — not merely the
    individually hand-joined stage functions."""

    def _context(self) -> WorkflowContext:
        return WorkflowContext(
            requirement="Build a URL shortener.",
            acceptance_criteria=("Creates short links.",),
            mode="greenfield",
        )

    def test_run_graph_sequences_stages_and_pauses_at_both_gates_then_completes(self):
        graph = _full_pipeline_graph()
        context = self._context()
        persisted = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir)

            # First call: runs analysis -> decomposition -> design/risk join ->
            # implementation staging, then pauses because nothing has approved
            # execution of the freshly staged candidate yet.
            outcome = run_graph(
                graph, context, _fake_generate, candidate_root, persist=persisted.append,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
            self.assertEqual(outcome.status, STATUS_AWAITING_EXECUTION_APPROVAL)
            self.assertTrue(context.analysis)
            self.assertTrue(context.decomposition)
            self.assertTrue(context.design)
            self.assertTrue(context.risks)
            self.assertIn("app.py", context.candidate_hashes)
            self.assertFalse(context.validation_passed)

            # Resuming without approval pauses again at the same gate, without
            # re-running already-completed stages (no new candidate hash churn).
            previous_hashes = dict(context.candidate_hashes)
            outcome_again = run_graph(
                graph, context, _fake_generate, candidate_root, persist=persisted.append,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
            self.assertEqual(outcome_again.status, STATUS_AWAITING_EXECUTION_APPROVAL)
            self.assertEqual(context.candidate_hashes, previous_hashes)

            # The human grants execution approval; the driver now runs real
            # validation and pauses at the release gate.
            request_execution_approval(context)
            outcome = run_graph(
                graph, context, _fake_generate, candidate_root, persist=persisted.append,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
            self.assertEqual(outcome.status, STATUS_AWAITING_RELEASE_APPROVAL)
            self.assertTrue(context.validation_passed)

            # The human grants release-readiness approval; the walk completes.
            request_release_approval(context)
            outcome = run_graph(
                graph, context, _fake_generate, candidate_root, persist=persisted.append,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
            self.assertEqual(outcome.status, STATUS_COMPLETED)
            self.assertTrue(context.release_approved)

    def test_run_graph_reports_validation_failure_without_pausing_as_approval(self):
        graph = _full_pipeline_graph()
        context = self._context()

        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir)
            run_graph(graph, context, _fake_generate, candidate_root, persist=lambda ctx: None)
            request_execution_approval(context)

            def _always_false(_candidate_dir: Path) -> bool:
                return False

            outcome = run_graph(
                graph, context, _fake_generate, candidate_root, persist=lambda ctx: None,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
                acceptance_checks=[AcceptanceCheck(name="always_false", check=_always_false)],
            )
            self.assertEqual(outcome.status, STATUS_VALIDATION_FAILED)
            self.assertFalse(context.validation_passed)

    def test_run_graph_resumes_from_state_persisted_to_disk(self):
        """Proves resumability is backed by persisted ``state.json``, not merely
        by holding the same Python object across calls."""
        graph = _full_pipeline_graph()
        context = self._context()

        call_count = {"n": 0}

        def _counting_generate(task_payload, schema):
            call_count["n"] += 1
            return _fake_generate(task_payload, schema)

        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir) / "candidate"
            candidate_root.mkdir()
            state_dir = Path(tmp_dir) / "run"
            state_dir.mkdir()
            state_path = state_dir / "state.json"

            state = RunState(run_id="run1", request_name="req", mode="greenfield", backend="api")

            def _persist(ctx: WorkflowContext) -> None:
                state.stages = context_to_dict(ctx)
                save_state(state_path, state)

            outcome = run_graph(graph, context, _counting_generate, candidate_root, persist=_persist)
            self.assertEqual(outcome.status, STATUS_AWAITING_EXECUTION_APPROVAL)
            calls_so_far = call_count["n"]
            self.assertGreater(calls_so_far, 0)

            # Simulate a fresh process: reload context from disk only.
            reloaded_state = load_state(state_path)
            resumed_context = context_from_dict(reloaded_state.stages)
            self.assertEqual(resumed_context.candidate_hashes, context.candidate_hashes)
            self.assertFalse(resumed_context.validation_passed)

            # Grant approval on the reloaded context and resume the walk; the
            # already-completed stages must not re-invoke generate.
            request_execution_approval(resumed_context)
            outcome = run_graph(
                graph, resumed_context, _counting_generate, candidate_root, persist=_persist,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
            self.assertEqual(outcome.status, STATUS_AWAITING_RELEASE_APPROVAL)
            self.assertEqual(call_count["n"], calls_so_far)  # no re-generation of completed stages

            request_release_approval(resumed_context)
            outcome = run_graph(
                graph, resumed_context, _counting_generate, candidate_root, persist=_persist,
                validation_commands=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
            self.assertEqual(outcome.status, STATUS_COMPLETED)

    def test_run_graph_runs_only_analysis_for_an_analysis_only_graph(self):
        graph = TaskGraph([Task("analysis", "analysis")])
        context = self._context()
        persisted = []

        outcome = run_graph(graph, context, _fake_generate, ".", persist=persisted.append)

        self.assertEqual(outcome.status, STATUS_COMPLETED)
        self.assertTrue(context.analysis)
        self.assertEqual(context.decomposition, {})
        self.assertFalse(context.candidate_hashes)


if __name__ == "__main__":
    unittest.main()
