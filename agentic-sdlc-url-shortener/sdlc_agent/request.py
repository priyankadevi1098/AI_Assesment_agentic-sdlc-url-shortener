"""Request contract: validated input describing a single SDLC agent run.

A request is the only untrusted document this tool reads at the boundary; every
field is validated here, once, so downstream stages can trust the shape of a
``Request`` without re-checking it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

VALID_MODES = ("greenfield", "brownfield")


class RequestValidationError(ValueError):
    """Raised when a request document fails contract validation.

    Carries the offending field name so a CLI caller can report precisely what
    was wrong, rather than a generic parse failure.
    """

    def __init__(self, message: str, *, field: str):
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True)
class Request:
    """A validated run request. Immutable once constructed."""

    name: str
    mode: str
    requirement: str
    acceptance_criteria: tuple
    answers: Optional[dict] = None


def load_request(path: Path) -> Request:
    """Read and validate a request JSON file from disk.

    Raises:
        RequestValidationError: the file could not be read, was not valid JSON,
            or failed contract validation.
    """
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RequestValidationError(f"could not read request file: {exc}", field="path") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RequestValidationError(f"not valid JSON: {exc}", field="document") from exc

    return parse_request(raw)


def parse_request(raw: object) -> Request:
    """Validate an already-parsed request document and return a ``Request``.

    Raises:
        RequestValidationError: a required field is missing, empty or the wrong type.
    """
    if not isinstance(raw, dict):
        raise RequestValidationError("request document must be a JSON object", field="document")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RequestValidationError("must be a non-empty string", field="name")

    mode = raw.get("mode")
    if mode not in VALID_MODES:
        raise RequestValidationError(f"must be one of {VALID_MODES}", field="mode")

    requirement = raw.get("requirement")
    if not isinstance(requirement, str) or not requirement.strip():
        raise RequestValidationError("must be a non-empty string", field="requirement")

    acceptance_criteria = raw.get("acceptance_criteria")
    if not isinstance(acceptance_criteria, list) or not acceptance_criteria:
        raise RequestValidationError("must be a non-empty list of strings", field="acceptance_criteria")
    for index, criterion in enumerate(acceptance_criteria):
        if not isinstance(criterion, str) or not criterion.strip():
            raise RequestValidationError(
                f"item {index} must be a non-empty string", field="acceptance_criteria"
            )

    answers = raw.get("answers")
    if answers is not None and not isinstance(answers, dict):
        raise RequestValidationError("must be a JSON object when present", field="answers")

    return Request(
        name=name.strip(),
        mode=mode,
        requirement=requirement.strip(),
        acceptance_criteria=tuple(criterion.strip() for criterion in acceptance_criteria),
        answers=answers,
    )
