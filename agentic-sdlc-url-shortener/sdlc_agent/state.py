"""Run state: unique run directories and atomically persisted JSON state.

Every run gets its own directory under ``runs/`` and a ``state.json`` that is
always written whole, via a temp-file-then-replace sequence, so a crash mid-write
can never leave a half-written state file behind.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict


class StateError(RuntimeError):
    """Raised when run state cannot be created, read or persisted."""


@dataclass
class RunState:
    """Persisted state for one run: graph, stage outputs, approvals."""

    run_id: str
    request_name: str
    mode: str
    backend: str
    status: str = "created"
    stages: Dict[str, Any] = field(default_factory=dict)
    approvals: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "RunState":
        return cls(**data)


def create_run_directory(base_dir: Path) -> Path:
    """Create a new, unique run directory under ``base_dir`` and return its path.

    Raises:
        StateError: a unique directory could not be allocated.
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    attempts = 5
    for _ in range(attempts):
        run_id = uuid.uuid4().hex[:12]
        run_dir = base_dir / run_id
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    raise StateError(f"could not allocate a unique run directory after {attempts} attempts")


def save_state(state_path: Path, state: RunState) -> None:
    """Persist state atomically: write a temp file beside the target, then replace it.

    Raises:
        StateError: the state directory does not exist or is not writable.
    """
    state_path = Path(state_path)
    payload = json.dumps(state.to_json(), indent=2, sort_keys=True)
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(state_path.parent), prefix=".state-", suffix=".tmp")
    except OSError as exc:
        raise StateError(f"could not create temp file in {state_path.parent}: {exc}") from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
        os.replace(tmp_name, state_path)
    except OSError as exc:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise StateError(f"could not persist state to {state_path}: {exc}") from exc


def load_state(state_path: Path) -> RunState:
    """Load a previously persisted state file.

    Raises:
        StateError: the file is missing, unreadable or not valid JSON.
    """
    state_path = Path(state_path)
    try:
        raw_text = state_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"could not read state file {state_path}: {exc}") from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StateError(f"state file {state_path} is not valid JSON: {exc}") from exc
    return RunState.from_json(data)
