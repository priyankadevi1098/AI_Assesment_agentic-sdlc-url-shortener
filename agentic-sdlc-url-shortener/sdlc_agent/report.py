"""Release evidence and honest metrics: an append-only JSONL event log plus
JSON/Markdown run summaries.

Metrics report only what the event log actually shows. Mean time to recovery
is reported as unavailable — never estimated or fabricated — when no recorded
failure has a matching, later recovery event.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


class ReportError(RuntimeError):
    """Raised when event log or summary artifacts cannot be written or read."""


def append_event(events_path: Path, event_type: str, details: Optional[dict] = None, *,
                  clock: Callable[[], float] = time.time) -> dict:
    """Append one event to the append-only JSONL log and return the recorded record.

    Raises:
        ReportError: the event log file could not be appended to.
    """
    event = {
        "event_id": uuid.uuid4().hex,
        "timestamp": clock(),
        "event_type": event_type,
        "details": details or {},
    }
    events_path = Path(events_path)
    try:
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        raise ReportError(f"could not append event to {events_path}: {exc}") from exc
    return event


def read_events(events_path: Path) -> List[dict]:
    """Read every event from the JSONL log, in append order.

    Returns an empty list if the log does not exist yet (a run that has not
    recorded any events is not an error).

    Raises:
        ReportError: the log file exists but contains invalid JSON.
    """
    events_path = Path(events_path)
    if not events_path.exists():
        return []
    events: List[dict] = []
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    except json.JSONDecodeError as exc:
        raise ReportError(f"event log {events_path} contains invalid JSON: {exc}") from exc
    return events


@dataclass
class Metrics:
    """Honest, event-derived metrics for one run."""

    task_success_rate: Optional[float]
    retries: int
    rollbacks: int
    stage_durations_seconds: Dict[str, float]
    mean_time_to_recovery_seconds: Optional[float]
    mttr_available: bool


def compute_metrics(events: Sequence[dict]) -> Metrics:
    """Compute metrics strictly from recorded events.

    ``mean_time_to_recovery_seconds`` is ``None`` and ``mttr_available`` is
    ``False`` unless at least one recorded ``stage_failed`` event has a later
    matching ``recovery`` event for the same stage.
    """
    stage_starts: Dict[str, float] = {}
    stage_durations: Dict[str, float] = {}
    pending_failures: Dict[str, float] = {}
    recovery_durations: List[float] = []
    retries = 0
    rollbacks = 0
    tasks_total = 0
    tasks_succeeded = 0

    for event in events:
        event_type = event["event_type"]
        timestamp = event["timestamp"]
        details = event.get("details", {})

        if event_type == "stage_started":
            stage_starts[details["stage"]] = timestamp
        elif event_type == "stage_completed":
            stage = details["stage"]
            if stage in stage_starts:
                stage_durations[stage] = timestamp - stage_starts[stage]
            tasks_total += 1
            tasks_succeeded += 1
        elif event_type == "stage_failed":
            tasks_total += 1
            pending_failures[details["stage"]] = timestamp
        elif event_type == "retry":
            retries += 1
        elif event_type == "rollback":
            rollbacks += 1
        elif event_type == "recovery":
            stage = details["stage"]
            if stage in pending_failures:
                recovery_durations.append(timestamp - pending_failures.pop(stage))

    success_rate = (tasks_succeeded / tasks_total) if tasks_total else None
    mttr_available = bool(recovery_durations)
    mttr = (sum(recovery_durations) / len(recovery_durations)) if mttr_available else None

    return Metrics(
        task_success_rate=success_rate, retries=retries, rollbacks=rollbacks,
        stage_durations_seconds=stage_durations,
        mean_time_to_recovery_seconds=mttr, mttr_available=mttr_available,
    )


def build_summary(run_id: str, status: str, metrics: Metrics, artifact_hashes: Dict[str, str]) -> dict:
    """Build the JSON-serializable summary document for a run."""
    return {
        "run_id": run_id,
        "status": status,
        "metrics": {
            "task_success_rate": metrics.task_success_rate,
            "retries": metrics.retries,
            "rollbacks": metrics.rollbacks,
            "stage_durations_seconds": metrics.stage_durations_seconds,
            "mean_time_to_recovery_seconds": metrics.mean_time_to_recovery_seconds,
            "mttr_available": metrics.mttr_available,
        },
        "artifact_hashes": dict(artifact_hashes),
    }


def render_markdown(summary: dict) -> str:
    """Render a run summary document as Markdown."""
    metrics = summary["metrics"]
    mttr = (
        f"{metrics['mean_time_to_recovery_seconds']:.2f}s"
        if metrics["mttr_available"] else "unavailable (no failure recovered)"
    )
    lines = [
        f"# Run {summary['run_id']} summary",
        "",
        f"Status: **{summary['status']}**",
        "",
        "## Metrics",
        "",
        f"- Task success rate: {metrics['task_success_rate']}",
        f"- Retries: {metrics['retries']}",
        f"- Rollbacks: {metrics['rollbacks']}",
        f"- Mean time to recovery: {mttr}",
        "",
        "## Artifact hashes",
        "",
    ]
    for path, digest in sorted(summary["artifact_hashes"].items()):
        lines.append(f"- `{path}`: `{digest}`")
    lines.append("")
    return "\n".join(lines)


def write_summary(run_dir: Path, summary: dict) -> None:
    """Write ``summary.json`` and ``summary.md`` into the run directory.

    Raises:
        ReportError: the summary files could not be written.
    """
    run_dir = Path(run_dir)
    try:
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        (run_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"could not write summary artifacts to {run_dir}: {exc}") from exc
