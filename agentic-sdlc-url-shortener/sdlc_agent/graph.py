"""Task graph: a validated dependency graph of workflow stages.

A model-produced decomposition must pass through this validation before the
orchestrator will touch it: unique task ids, only recognised stage types, no
cycles, and no gated stage (implementation, release) that bypasses the required
``governance_gate`` dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence


class GraphValidationError(ValueError):
    """Raised when a task graph violates a structural or governance rule."""


STAGE_TYPES = frozenset({
    "analysis", "decomposition", "design", "risk_assessment",
    "governance_gate", "implementation", "validation", "release",
})

# Stage types that must have a governance_gate task somewhere in their transitive
# dependency chain before they may run.
GATED_STAGE_TYPES = frozenset({"implementation", "release"})


@dataclass(frozen=True)
class Task:
    """One node in the task graph."""

    task_id: str
    stage_type: str
    depends_on: tuple = ()


class TaskGraph:
    """A validated, immutable task graph built from a model-produced decomposition."""

    def __init__(self, tasks: Sequence[Task]):
        """Build and validate the graph.

        Raises:
            GraphValidationError: a duplicate id, unknown stage type, unknown
                dependency, cycle, or governance-gate bypass was found.
        """
        self._tasks: Dict[str, Task] = {}
        self._validate_and_store(tasks)

    def _validate_and_store(self, tasks: Sequence[Task]) -> None:
        for task in tasks:
            if task.task_id in self._tasks:
                raise GraphValidationError(f"duplicate task id: {task.task_id!r}")
            if task.stage_type not in STAGE_TYPES:
                raise GraphValidationError(
                    f"task {task.task_id!r} has an unknown stage type: {task.stage_type!r}"
                )
            self._tasks[task.task_id] = task

        for task in tasks:
            for dependency_id in task.depends_on:
                if dependency_id not in self._tasks:
                    raise GraphValidationError(
                        f"task {task.task_id!r} depends on unknown task {dependency_id!r}"
                    )

        self._check_acyclic()
        self._check_required_gates()

    def _check_acyclic(self) -> None:
        white, gray, black = 0, 1, 2
        color = {task_id: white for task_id in self._tasks}

        def visit(task_id: str, path: List[str]) -> None:
            color[task_id] = gray
            for dependency_id in self._tasks[task_id].depends_on:
                if color[dependency_id] == gray:
                    cycle = "->".join(path + [dependency_id])
                    raise GraphValidationError(f"task graph has a cycle: {cycle}")
                if color[dependency_id] == white:
                    visit(dependency_id, path + [dependency_id])
            color[task_id] = black

        for task_id in self._tasks:
            if color[task_id] == white:
                visit(task_id, [task_id])

    def _upstream_stage_types(self, task_id: str, visited: set) -> set:
        if task_id in visited:
            return set()
        visited.add(task_id)
        task = self._tasks[task_id]
        upstream = {task.stage_type}
        for dependency_id in task.depends_on:
            upstream |= self._upstream_stage_types(dependency_id, visited)
        return upstream

    def _check_required_gates(self) -> None:
        for task in self._tasks.values():
            if task.stage_type not in GATED_STAGE_TYPES:
                continue
            upstream_types: set = set()
            for dependency_id in task.depends_on:
                upstream_types |= self._upstream_stage_types(dependency_id, set())
            if "governance_gate" not in upstream_types:
                raise GraphValidationError(
                    f"task {task.task_id!r} ({task.stage_type}) bypasses the required "
                    "governance_gate dependency"
                )

    def tasks(self) -> Dict[str, Task]:
        """Return a copy of the task map, keyed by task id."""
        return dict(self._tasks)

    def topological_order(self) -> List[str]:
        """Return task ids in an order that honours every dependency."""
        order: List[str] = []
        visited: set = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            visited.add(task_id)
            for dependency_id in self._tasks[task_id].depends_on:
                visit(dependency_id)
            order.append(task_id)

        for task_id in self._tasks:
            visit(task_id)
        return order
