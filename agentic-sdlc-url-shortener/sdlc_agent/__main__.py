"""Command-line entry point: ``py -3 -m sdlc_agent <run|show|resume> ...``.

This wires the request/state contracts, backend preflight, and the
``workflow.run_graph`` driver together: ``run`` starts the canonical governed
pipeline (``workflow.build_default_graph``) and walks it until it finishes,
fails or pauses for a human decision; ``resume`` reloads the exact same
persisted ``WorkflowContext`` from ``state.json``, applies the one action
requested (an approval, clarification answers, or a changed requirement) and
continues the walk. Every exit code and terminal message reflects the real
outcome of that walk — this entry point never prints "completed" for a run
that is actually paused or failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .backends import (
    BackendSelectionAmbiguousError,
    BackendSelector,
    DestinationApprovalRequiredError,
    NoCompatibleBackendError,
    require_destination_approval,
)
from .cli_backends import ClaudeCodeCliAdapter, CodexCliAdapter, CopilotCliAdapter
from .llm import ApiBackend, LlmConfig, LlmConfigError
from .request import RequestValidationError, load_request
from .state import RunState, StateError, create_run_directory, load_state, save_state
from .validation import AcceptanceCheck
from .workflow import (
    STATUS_AWAITING_EXECUTION_APPROVAL,
    STATUS_AWAITING_RELEASE_APPROVAL,
    STATUS_COMPLETED,
    STATUS_VALIDATION_FAILED,
    GraphRunOutcome,
    WorkflowContext,
    WorkflowError,
    apply_clarification_answers,
    apply_requirement_change,
    build_default_graph,
    context_from_dict,
    context_to_dict,
    request_execution_approval,
    request_release_approval,
    run_graph,
)
from .workspace import compute_hash

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 2
EXIT_STATE_ERROR = 3
EXIT_BACKEND_ERROR = 4
EXIT_VALIDATION_FAILED = 5

RUNS_DIR_NAME = "runs"

GenerateFn = Callable[[dict, dict], dict]

_STATUS_MESSAGES = {
    STATUS_AWAITING_EXECUTION_APPROVAL: (
        "status: awaiting execution approval — review the staged candidate and diff, "
        "then resume with --approve execute_candidate"
    ),
    STATUS_AWAITING_RELEASE_APPROVAL: (
        "status: validated; awaiting release-readiness approval — "
        "resume with --approve release_ready"
    ),
    STATUS_VALIDATION_FAILED: (
        "status: validation failed — inspect state.json and the candidate for the failing command"
    ),
    STATUS_COMPLETED: "status: completed — release-readiness approved (never deployed or published)",
}


def _build_backend_selector() -> BackendSelector:
    return BackendSelector([CopilotCliAdapter(), ClaudeCodeCliAdapter(), CodexCliAdapter()])


def _api_credentials_available() -> bool:
    import os

    try:
        LlmConfig.from_env(dict(os.environ))
    except LlmConfigError:
        return False
    return True


def _build_generate_fn(backend_name: str) -> Optional[GenerateFn]:
    """Build the ``generate(task_payload, schema)`` callable for an already-
    selected backend, so ``workflow.run_graph`` never knows which adapter it
    is talking to. Returns ``None`` if the backend can no longer be
    initialized (for example, API credentials were removed between ``run``
    and ``resume``)."""
    if backend_name == "api":
        import os

        try:
            config = LlmConfig.from_env(dict(os.environ))
        except LlmConfigError:
            return None
        return ApiBackend(config).generate

    for adapter in (CopilotCliAdapter(), ClaudeCodeCliAdapter(), CodexCliAdapter()):
        if adapter.name == backend_name:
            return lambda task_payload, schema, _adapter=adapter: _adapter.invoke(task_payload, schema=schema)
    return None


def _default_validation_commands() -> List[Sequence[str]]:
    """Fixed, tool-owned local validation commands run against a staged
    candidate — never a model-supplied command. A generated candidate is
    expected to include its own ``tests`` directory; a missing one is a real
    validation failure, not silently skipped."""
    return [
        (sys.executable, "-m", "compileall", "-q", "."),
        (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"),
    ]


def _default_acceptance_checks(context: WorkflowContext) -> List[AcceptanceCheck]:
    """Tool-owned, independent acceptance check: the files on disk still match
    the exact hashes staged for this candidate. Reads ``context.candidate_hashes``
    at call time (after implementation has staged the candidate), not when this
    list is built, so it is accurate however the candidate was resumed."""

    def _candidate_matches_staged_hashes(candidate_dir: Path) -> bool:
        for relative_path, expected_hash in context.candidate_hashes.items():
            file_path = candidate_dir / relative_path
            if not file_path.is_file():
                return False
            if compute_hash(file_path.read_text(encoding="utf-8")) != expected_hash:
                return False
        return True

    return [AcceptanceCheck(name="candidate_matches_staged_hashes", check=_candidate_matches_staged_hashes)]


def _continue_graph(context: WorkflowContext, generate_fn: GenerateFn, run_dir: Path,
                     state: RunState, state_path: Path) -> GraphRunOutcome:
    """Walk the canonical governed pipeline from wherever ``context`` currently
    stands, persisting the context (and the final outcome status) to
    ``state.json`` after every stage and at the end of the walk."""
    candidate_root = run_dir / "candidate"

    def _persist(ctx: WorkflowContext) -> None:
        state.stages = context_to_dict(ctx)
        save_state(state_path, state)

    outcome = run_graph(
        build_default_graph(), context, generate_fn, candidate_root, persist=_persist,
        validation_commands=_default_validation_commands(),
        acceptance_checks=_default_acceptance_checks(context),
    )
    state.status = outcome.status
    state.stages = context_to_dict(outcome.context)
    save_state(state_path, state)
    return outcome


def _print_outcome(status: str) -> None:
    print(_STATUS_MESSAGES.get(status, f"status: {status}"))


def _exit_code_for(status: str) -> int:
    if status == STATUS_VALIDATION_FAILED:
        return EXIT_VALIDATION_FAILED
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        request = load_request(Path(args.request))
    except RequestValidationError as exc:
        print(f"request rejected: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    selector = _build_backend_selector()
    try:
        backend_name = selector.select(
            args.backend, preference=args.backend_preference,
            api_available=_api_credentials_available(),
        )
    except (BackendSelectionAmbiguousError, NoCompatibleBackendError) as exc:
        print(f"backend selection failed: {exc}", file=sys.stderr)
        return EXIT_BACKEND_ERROR

    def _approve(name: str) -> bool:
        if args.yes:
            return True
        response = input(f"Send requirement/source content to backend {name!r}? [y/N] ")
        return response.strip().lower() == "y"

    try:
        require_destination_approval(backend_name, _approve)
    except DestinationApprovalRequiredError as exc:
        print(f"destination not approved: {exc}", file=sys.stderr)
        return EXIT_BACKEND_ERROR

    generate_fn = _build_generate_fn(backend_name)
    if generate_fn is None:
        print(f"backend {backend_name!r} could not be initialized for this run", file=sys.stderr)
        return EXIT_BACKEND_ERROR

    try:
        run_dir = create_run_directory(Path(RUNS_DIR_NAME))
        state = RunState(
            run_id=run_dir.name, request_name=request.name, mode=request.mode,
            backend=backend_name, status="created",
        )
        state_path = run_dir / "state.json"
        save_state(state_path, state)
    except StateError as exc:
        print(f"could not create run: {exc}", file=sys.stderr)
        return EXIT_STATE_ERROR

    context = WorkflowContext(
        requirement=request.requirement, acceptance_criteria=request.acceptance_criteria, mode=request.mode,
    )
    if request.answers:
        context.answers = dict(request.answers)

    print(f"run created: {run_dir.name}")
    print(f"backend selected: {backend_name}")

    outcome = _continue_graph(context, generate_fn, run_dir, state, state_path)
    _print_outcome(outcome.status)
    return _exit_code_for(outcome.status)


def _cmd_show(args: argparse.Namespace) -> int:
    run_dir = Path(RUNS_DIR_NAME) / args.run
    try:
        state = load_state(run_dir / "state.json")
    except StateError as exc:
        print(f"could not read run: {exc}", file=sys.stderr)
        return EXIT_STATE_ERROR

    print(f"run: {state.run_id}")
    print(f"request: {state.request_name} ({state.mode})")
    print(f"backend: {state.backend}")
    print(f"status: {state.status}")
    return EXIT_OK


def _cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(RUNS_DIR_NAME) / args.run
    state_path = run_dir / "state.json"
    try:
        state = load_state(state_path)
    except StateError as exc:
        print(f"could not read run: {exc}", file=sys.stderr)
        return EXIT_STATE_ERROR

    if not state.stages:
        print(f"run {state.run_id} has not started any stage yet; nothing to resume", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        context = context_from_dict(state.stages)
    except WorkflowError as exc:
        print(f"could not reconstruct run state: {exc}", file=sys.stderr)
        return EXIT_STATE_ERROR

    actions_requested = sum(bool(value) for value in (args.approve, args.answers, args.request))
    if actions_requested == 0:
        print("resume requires one of --approve, --answers or --request", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    if actions_requested > 1:
        print("resume accepts only one of --approve, --answers or --request per call", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    if args.request:
        try:
            new_request = load_request(Path(args.request))
        except RequestValidationError as exc:
            print(f"request rejected: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        invalidated = apply_requirement_change(context, new_request.requirement)
        print(f"requirement changed (plan revision {context.plan_revision}); invalidated stages: {invalidated}")

    elif args.answers:
        try:
            raw_document = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"could not read answers file: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        except json.JSONDecodeError as exc:
            print(f"answers file is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        if not isinstance(raw_document, dict):
            print("answers file must contain a JSON object", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        # Accept either a bare {"question": "answer", ...} mapping, or a
        # request-shaped document (matching examples/requests/ambiguous-answers.json:
        # name/mode/requirement/acceptance_criteria plus a nested "answers" object)
        # and unwrap its "answers" field — never fold request metadata like "mode"
        # or "requirement" into the clarification-answers mapping.
        nested_answers = raw_document.get("answers")
        raw_answers = nested_answers if isinstance(nested_answers, dict) else raw_document
        invalidated = apply_clarification_answers(context, raw_answers)
        print(f"clarification answers applied; invalidated stages: {invalidated}")

    elif args.approve == "execute_candidate":
        try:
            request_execution_approval(context)
        except WorkflowError as exc:
            print(f"cannot approve execution: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        print("execution approval recorded, bound to the exact staged candidate hashes")

    else:
        try:
            request_release_approval(context)
        except WorkflowError as exc:
            print(f"cannot approve release: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        print("release-readiness approval recorded (not deployed or published)")

    generate_fn = _build_generate_fn(state.backend)
    if generate_fn is None:
        print(f"backend {state.backend!r} could not be initialized to continue this run", file=sys.stderr)
        return EXIT_BACKEND_ERROR

    outcome = _continue_graph(context, generate_fn, run_dir, state, state_path)
    _print_outcome(outcome.status)
    return _exit_code_for(outcome.status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlc_agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start a new run from a request file")
    run_parser.add_argument("--request", required=True)
    run_parser.add_argument("--backend", default="auto",
                             choices=["auto", "copilot", "claude-code", "codex", "api"])
    run_parser.add_argument("--backend-preference", default=None,
                             choices=["copilot", "claude-code", "codex", "api"])
    run_parser.add_argument("--project", default=None, help="brownfield source project directory")
    run_parser.add_argument("--yes", action="store_true", help="approve destination without prompting")
    run_parser.set_defaults(func=_cmd_run)

    show_parser = subparsers.add_parser("show", help="inspect an existing run")
    show_parser.add_argument("--run", required=True)
    show_parser.set_defaults(func=_cmd_show)

    resume_parser = subparsers.add_parser("resume", help="resume a paused run")
    resume_parser.add_argument("--run", required=True)
    resume_parser.add_argument("--approve", default=None,
                                choices=["execute_candidate", "release_ready"])
    resume_parser.add_argument("--answers", default=None, help="path to a clarification-answers JSON file")
    resume_parser.add_argument("--request", default=None, help="path to an updated request JSON file")
    resume_parser.set_defaults(func=_cmd_resume)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
