"""Prompt templates and task-specific JSON schemas for each workflow stage.

Centralized so CLI and API adapters send the same structured task contract,
and so a schema change happens in one place.
"""
from __future__ import annotations

from typing import Iterable

# Each schema below validates the outgoing structured task payload at the top
# level (``required``/``properties``), and the model's JSON response under the
# nested ``response`` key — the same two-sided contract
# ``llm.ApiBackend.generate``/``cli_backends.CliAdapter.invoke`` already
# enforce (request validated against the schema itself, response validated
# against ``schema.get("response", schema)``). Earlier versions of these
# constants described only the response shape and were also handed to
# ``validate_schema`` as the *request* schema, which made every real backend
# call (API or CLI) reject its own outgoing payload before it was ever sent —
# caught by the subprocess-level CLI integration tests, since every unit test
# up to that point stubbed ``generate``/exercised the two sides separately and
# never drove a real adapter against these constants end-to-end.
ANALYSIS_SCHEMA = {
    "required": ["task", "requirement", "acceptance_criteria"],
    "properties": {"task": str, "requirement": str, "acceptance_criteria": list},
    "response": {
        "required": ["summary", "key_requirements"],
        "properties": {"summary": str, "key_requirements": list},
    },
}

DECOMPOSITION_SCHEMA = {
    "required": ["task", "analysis"],
    "properties": {"task": str, "analysis": dict},
    "response": {"required": ["tasks"], "properties": {"tasks": list}},
}

DESIGN_SCHEMA = {
    "required": ["task", "decomposition"],
    "properties": {"task": str, "decomposition": dict},
    "response": {"required": ["design"], "properties": {"design": str}},
}

RISK_SCHEMA = {
    "required": ["task", "decomposition"],
    "properties": {"task": str, "decomposition": dict},
    "response": {"required": ["risks"], "properties": {"risks": list}},
}

FILE_BUNDLE_SCHEMA = {
    "required": ["task", "decomposition", "design"],
    "properties": {"task": str, "decomposition": dict, "design": dict},
    "response": {"required": ["files"], "properties": {"files": list}},
}


def build_analysis_prompt(requirement: str, acceptance_criteria: Iterable[str]) -> dict:
    """Build the structured task payload for the analysis stage."""
    return {
        "task": "analysis",
        "requirement": requirement,
        "acceptance_criteria": list(acceptance_criteria),
    }


def build_decomposition_prompt(analysis: dict) -> dict:
    """Build the structured task payload for the decomposition stage."""
    return {"task": "decomposition", "analysis": analysis}


def build_design_prompt(decomposition: dict) -> dict:
    """Build the structured task payload for the design stage."""
    return {"task": "design", "decomposition": decomposition}


def build_risk_prompt(decomposition: dict) -> dict:
    """Build the structured task payload for the risk-assessment stage."""
    return {"task": "risk_assessment", "decomposition": decomposition}


def build_implementation_prompt(decomposition: dict, design: dict) -> dict:
    """Build the structured task payload for the implementation (file-bundle) stage."""
    return {"task": "implementation", "decomposition": decomposition, "design": design}
