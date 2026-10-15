"""Executable orchestration: analysis, decomposition, and a concurrent
design/risk-assessment join that must complete before implementation starts.

Each stage persists its output into the shared ``WorkflowContext`` through an
injected ``persist`` callback, so the caller controls exactly how/where state is
written (``state.py``) without this module depending on disk layout.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import prompts
from .graph import Task, TaskGraph
from .validation import (
    AcceptanceCheck,
    ApprovalMismatchError,
    ExecutionApproval,
    create_execution_approval,
    run_acceptance_checks,
    run_fixed_command,
    verify_execution_approval,
)
from .workspace import FileChange, stage_changes

DEFAULT_MAX_REPAIR_ATTEMPTS = 3

# Stage names in dependency order. Invalidating a stage invalidates everything
# from it onward: resuming after an upstream change must never silently keep
# downstream outputs whose inputs no longer match.
STAGE_ORDER = ("analysis", "decomposition", "design", "risks", "candidate", "validation", "release")


class WorkflowError(RuntimeError):
    """Raised when a workflow stage cannot proceed given the current context."""


@dataclass
class WorkflowContext:
    """Shared context carried and persisted across workflow stages."""

    requirement: str
    acceptance_criteria: tuple
    mode: str
    analysis: Dict[str, Any] = field(default_factory=dict)
    decomposition: Dict[str, Any] = field(default_factory=dict)
    design: Dict[str, Any] = field(default_factory=dict)
    risks: Dict[str, Any] = field(default_factory=dict)
    answers: Dict[str, Any] = field(default_factory=dict)
    candidate_hashes: Dict[str, str] = field(default_factory=dict)
    plan_revision: int = 0
    repair_attempts: int = 0
    execution_approval: Optional[ExecutionApproval] = None
    validation_passed: bool = False
    release_approved: bool = False
    invalidated_stages: List[str] = field(default_factory=list)


GenerateFn = Callable[[dict, dict], dict]
PersistFn = Callable[[WorkflowContext], None]


def run_analysis_stage(context: WorkflowContext, generate: GenerateFn, persist: PersistFn) -> WorkflowContext:
    """Run the analysis stage and persist the shared context."""
    task_payload = prompts.build_analysis_prompt(context.requirement, context.acceptance_criteria)
    context.analysis = generate(task_payload, prompts.ANALYSIS_SCHEMA)
    persist(context)
    return context


def run_decomposition_stage(context: WorkflowContext, generate: GenerateFn, persist: PersistFn) -> WorkflowContext:
    """Run the decomposition stage and persist the shared context.

    Raises:
        WorkflowError: the analysis stage has not completed yet.
    """
    if not context.analysis:
        raise WorkflowError("decomposition requires a completed analysis stage")
    task_payload = prompts.build_decomposition_prompt(context.analysis)
    context.decomposition = generate(task_payload, prompts.DECOMPOSITION_SCHEMA)
    persist(context)
    return context


def run_design_and_risk_join(context: WorkflowContext, generate: GenerateFn, persist: PersistFn) -> WorkflowContext:
    """Run design and risk-assessment concurrently, joining both before returning.

    Implementation must never start from only one of the two; this function does
    not return (and does not persist) until both futures have resolved.

    Raises:
        WorkflowError: the decomposition stage has not completed yet.
    """
    if not context.decomposition:
        raise WorkflowError("design/risk stages require a completed decomposition stage")

    design_payload = prompts.build_design_prompt(context.decomposition)
    risk_payload = prompts.build_risk_prompt(context.decomposition)

    with ThreadPoolExecutor(max_workers=2) as executor:
        design_future = executor.submit(generate, design_payload, prompts.DESIGN_SCHEMA)
        risk_future = executor.submit(generate, risk_payload, prompts.RISK_SCHEMA)
        context.design = design_future.result()
        context.risks = risk_future.result()

    persist(context)
    return context


def ensure_ready_for_implementation(context: WorkflowContext) -> None:
    """Raise unless both design and risk assessment have completed — the join gate.

    Raises:
        WorkflowError: design or risk assessment (or both) have not completed.
    """
    if not context.design or not context.risks:
        raise WorkflowError("implementation cannot start before design and risk assessment join")


def requirement_hash(requirement: str) -> str:
    """Return a stable hash of the requirement text, used to bind execution approval."""
    import hashlib
    return hashlib.sha256(requirement.encode("utf-8")).hexdigest()


def run_implementation_stage(context: WorkflowContext, generate: GenerateFn, candidate_root: Path,
                              persist: PersistFn) -> WorkflowContext:
    """Generate a model file bundle, validate and stage it, and record the staged
    candidate's file hashes. Staging always invalidates any prior execution
    approval — a fresh approval is required for the newly staged candidate.

    Raises:
        WorkflowError: design/risk assessment has not completed (the join gate).
        WorkspaceValidationError: the generated bundle violates a workspace rule.
    """
    ensure_ready_for_implementation(context)
    task_payload = prompts.build_implementation_prompt(context.decomposition, context.design)
    response = generate(task_payload, prompts.FILE_BUNDLE_SCHEMA)
    changes = [
        FileChange(relative_path=entry["relative_path"], content=entry["content"])
        for entry in response["files"]
    ]
    context.candidate_hashes = stage_changes(changes, candidate_root)
    context.execution_approval = None
    persist(context)
    return context


# --- Approval, execution and repair governance -----------------------------


def request_execution_approval(context: WorkflowContext) -> ExecutionApproval:
    """Grant execution approval bound to the context's exact current candidate
    hashes, requirement and plan revision, recording it on the context.

    Raises:
        WorkflowError: no candidate has been staged yet.
    """
    if not context.candidate_hashes:
        raise WorkflowError("cannot approve execution before candidate files are staged")
    approval = create_execution_approval(
        context.candidate_hashes, requirement_hash(context.requirement), context.plan_revision,
    )
    context.execution_approval = approval
    return approval


def ensure_execution_approved(context: WorkflowContext) -> None:
    """Raise unless the context's current candidate state matches a granted approval.

    Raises:
        ApprovalMismatchError: see ``validation.verify_execution_approval``.
    """
    verify_execution_approval(
        context.execution_approval, context.candidate_hashes,
        requirement_hash(context.requirement), context.plan_revision,
    )


def record_repair(context: WorkflowContext, *, max_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS) -> int:
    """Record that a repair produced a new diff: invalidate execution approval and
    require a fresh one before the changed candidate runs.

    Returns:
        The repair attempt count after this repair.

    Raises:
        WorkflowError: the bounded repair-attempt budget is exhausted.
    """
    context.repair_attempts += 1
    if context.repair_attempts > max_attempts:
        raise WorkflowError(f"repair budget of {max_attempts} attempts exhausted")
    context.execution_approval = None
    context.validation_passed = False
    return context.repair_attempts


def request_release_approval(context: WorkflowContext) -> None:
    """Record release-readiness approval — separate from, and only possible after,
    a successful execution approval and validation. Never deploys or publishes.

    Raises:
        WorkflowError: execution has not been approved and validated successfully.
    """
    if context.execution_approval is None or not context.validation_passed:
        raise WorkflowError(
            "release-readiness approval requires a completed, approved and validated execution"
        )
    context.release_approved = True


def ensure_release_approved(context: WorkflowContext) -> None:
    """Raise unless release-readiness has been explicitly approved.

    Raises:
        WorkflowError: release has not been approved.
    """
    if not context.release_approved:
        raise WorkflowError("release has not been approved")


# --- Resume, replan and recovery --------------------------------------------


def invalidate_downstream(context: WorkflowContext, changed_stage: str) -> List[str]:
    """Invalidate ``changed_stage`` and every stage downstream of it in
    ``STAGE_ORDER``, leaving unaffected upstream stages untouched.

    Returns:
        The list of stage names invalidated by this call.

    Raises:
        WorkflowError: ``changed_stage`` is not a recognised stage name.
    """
    if changed_stage not in STAGE_ORDER:
        raise WorkflowError(f"unknown stage: {changed_stage!r}")

    invalidated = list(STAGE_ORDER[STAGE_ORDER.index(changed_stage):])
    for stage in invalidated:
        if stage == "analysis":
            context.analysis = {}
        elif stage == "decomposition":
            context.decomposition = {}
        elif stage == "design":
            context.design = {}
        elif stage == "risks":
            context.risks = {}
        elif stage == "candidate":
            context.candidate_hashes = {}
        elif stage == "validation":
            context.validation_passed = False
        elif stage == "release":
            context.release_approved = False

    if "candidate" in invalidated or "validation" in invalidated:
        context.execution_approval = None

    context.invalidated_stages.extend(invalidated)
    return invalidated


def apply_clarification_answers(context: WorkflowContext, answers: Dict[str, Any]) -> List[str]:
    """Incorporate resume-time clarification answers and invalidate every stage
    from analysis onward, since answers change requirement understanding.

    Returns:
        The list of stage names invalidated by this call.
    """
    context.answers = dict(answers)
    return invalidate_downstream(context, "analysis")


def apply_requirement_change(context: WorkflowContext, new_requirement: str) -> List[str]:
    """Apply a changed upstream requirement on resume and invalidate every stage
    from analysis onward, since the requirement is the input to analysis.

    Returns:
        The list of stage names invalidated by this call.
    """
    context.requirement = new_requirement
    context.plan_revision += 1
    return invalidate_downstream(context, "analysis")


# --- Context (de)serialization, for persisted-state resume ------------------


def context_to_dict(context: WorkflowContext) -> dict:
    """Serialize a ``WorkflowContext`` to a plain JSON-safe dict, suitable for
    ``state.RunState.stages`` so a run can stop and resume from ``state.json``."""
    return asdict(context)


def context_from_dict(data: Dict[str, Any]) -> WorkflowContext:
    """Reconstruct a ``WorkflowContext`` previously serialized by ``context_to_dict``.

    Raises:
        WorkflowError: ``data`` is missing a required context field.
    """
    data = dict(data)
    approval_data = data.pop("execution_approval", None)
    try:
        context = WorkflowContext(
            requirement=data["requirement"],
            acceptance_criteria=tuple(data["acceptance_criteria"]),
            mode=data["mode"],
            analysis=data.get("analysis", {}),
            decomposition=data.get("decomposition", {}),
            design=data.get("design", {}),
            risks=data.get("risks", {}),
            answers=data.get("answers", {}),
            candidate_hashes=data.get("candidate_hashes", {}),
            plan_revision=data.get("plan_revision", 0),
            repair_attempts=data.get("repair_attempts", 0),
            validation_passed=data.get("validation_passed", False),
            release_approved=data.get("release_approved", False),
            invalidated_stages=list(data.get("invalidated_stages", [])),
        )
    except KeyError as exc:
        raise WorkflowError(f"persisted context is missing required field: {exc}") from exc

    if approval_data:
        context.execution_approval = ExecutionApproval(
            candidate_hashes=dict(approval_data["candidate_hashes"]),
            requirement_hash=approval_data["requirement_hash"],
            plan_revision=approval_data["plan_revision"],
        )
    return context


def build_default_graph() -> TaskGraph:
    """Build the canonical governed pipeline graph the CLI drives through
    ``run_graph`` for every run: analysis -> decomposition -> the design/risk
    join -> a governance gate -> implementation -> validation -> a second
    governance gate -> release. The pipeline's own gated shape is fixed by
    this tool, not by the model; a model-produced decomposition informs task
    *content* within each stage, not whether an approval gate is required.
    """
    return TaskGraph([
        Task("analysis", "analysis"),
        Task("decomposition", "decomposition", depends_on=("analysis",)),
        Task("design", "design", depends_on=("decomposition",)),
        Task("risk", "risk_assessment", depends_on=("decomposition",)),
        Task("gate1", "governance_gate", depends_on=("design", "risk")),
        Task("implementation", "implementation", depends_on=("gate1",)),
        Task("validation", "validation", depends_on=("implementation",)),
        Task("gate2", "governance_gate", depends_on=("validation",)),
        Task("release", "release", depends_on=("gate2",)),
    ])


# --- Generic graph driver -----------------------------------------------


# Graph-driver pause/completion verdicts. Never conflated with "completed":
# the CLI (step 10) must print one of these honestly, never "completed" for a
# paused or failed run.
STATUS_AWAITING_EXECUTION_APPROVAL = "awaiting_execution_approval"
STATUS_AWAITING_RELEASE_APPROVAL = "awaiting_release_approval"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_COMPLETED = "completed"


@dataclass
class GraphRunOutcome:
    """The verdict of one ``run_graph`` call: where the walk stopped and why."""

    status: str
    context: WorkflowContext


def run_graph(graph: TaskGraph, context: WorkflowContext, generate: GenerateFn, candidate_root: Path,
              persist: PersistFn, *, validation_commands: Sequence[Sequence[str]] = (),
              acceptance_checks: Sequence[AcceptanceCheck] = ()) -> GraphRunOutcome:
    """Walk an arbitrary validated ``graph.TaskGraph`` end-to-end: analysis,
    decomposition, the parallel design/risk join, implementation staging,
    pausing for execution approval, real validation, and pausing for release
    approval.

    The stages actually driven are derived from the stage types present in
    ``graph`` (not a fixed hardcoded pipeline), so this is generic over any
    graph that passed ``TaskGraph`` validation — a graph with only an analysis
    task runs only analysis; a graph with implementation also requires and
    waits for execution approval before validation.

    Resumable: ``context`` may already carry completed-stage outputs (for
    example, reloaded from persisted ``state.json`` via ``context_from_dict``);
    already-completed stages are skipped and the walk continues from the next
    pending one. Approval itself is never granted here — call
    ``request_execution_approval``/``request_release_approval`` externally
    (the human decision) and call this function again to proceed past the gate.

    Returns:
        A ``GraphRunOutcome`` whose ``status`` is one of
        ``STATUS_AWAITING_EXECUTION_APPROVAL``, ``STATUS_AWAITING_RELEASE_APPROVAL``,
        ``STATUS_VALIDATION_FAILED`` or ``STATUS_COMPLETED`` — never a bare
        "completed" string for a run that is actually paused or failed.
    """
    stage_types = {task.stage_type for task in graph.tasks().values()}

    if "analysis" in stage_types and not context.analysis:
        run_analysis_stage(context, generate, persist)

    if "decomposition" in stage_types and not context.decomposition:
        run_decomposition_stage(context, generate, persist)

    if ({"design", "risk_assessment"} & stage_types) and not (context.design and context.risks):
        run_design_and_risk_join(context, generate, persist)

    if "implementation" in stage_types and not context.candidate_hashes:
        run_implementation_stage(context, generate, candidate_root, persist)

    requires_execution_gate = "implementation" in stage_types or "validation" in stage_types
    if requires_execution_gate and not context.validation_passed:
        try:
            ensure_execution_approved(context)
        except ApprovalMismatchError:
            persist(context)
            return GraphRunOutcome(status=STATUS_AWAITING_EXECUTION_APPROVAL, context=context)

    if "validation" in stage_types and not context.validation_passed:
        for command in validation_commands:
            result = run_fixed_command(command, candidate_root)
            if result.timed_out or result.exit_code != 0:
                persist(context)
                return GraphRunOutcome(status=STATUS_VALIDATION_FAILED, context=context)
        if acceptance_checks:
            acceptance_results = run_acceptance_checks(candidate_root, acceptance_checks)
            if not all(result.passed for result in acceptance_results):
                persist(context)
                return GraphRunOutcome(status=STATUS_VALIDATION_FAILED, context=context)
        context.validation_passed = True
        persist(context)

    if "release" in stage_types:
        try:
            ensure_release_approved(context)
        except WorkflowError:
            persist(context)
            return GraphRunOutcome(status=STATUS_AWAITING_RELEASE_APPROVAL, context=context)

    persist(context)
    return GraphRunOutcome(status=STATUS_COMPLETED, context=context)
