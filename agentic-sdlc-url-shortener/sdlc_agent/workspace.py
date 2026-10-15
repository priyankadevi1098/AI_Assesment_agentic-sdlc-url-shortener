"""Workspace: bounded allowlisted brownfield reading and validated file-bundle staging.

Brownfield source is read as text only — never imported, never executed. Model-
generated file bundles are validated before anything touches disk: absolute or
traversal paths, symlinked targets, binary content, oversized changes and edits
outside the candidate workspace are all rejected.
"""
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Sequence

DEFAULT_MAX_FILE_BYTES = 200_000
DEFAULT_ALLOWED_SUFFIXES = (".py", ".md", ".json", ".txt", ".cfg", ".toml", ".ini")


class WorkspaceValidationError(ValueError):
    """Raised when a model-generated file bundle violates a workspace safety rule."""


@dataclass(frozen=True)
class FileChange:
    """One file in a model-generated change bundle."""

    relative_path: str
    content: str


def read_brownfield_source(project_dir: Path, *, allowed_suffixes: Sequence[str] = DEFAULT_ALLOWED_SUFFIXES,
                            max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> Dict[str, str]:
    """Read allowlisted application source/tests as text only — never imported or executed.

    Symlinked entries, non-allowlisted suffixes and oversized files are silently
    skipped rather than read, so a brownfield project can never smuggle in content
    this tool was not asked to reason about.

    Raises:
        WorkspaceValidationError: ``project_dir`` does not exist or is not a directory.
    """
    project_dir = Path(project_dir).resolve()
    if not project_dir.is_dir():
        raise WorkspaceValidationError(f"brownfield project directory does not exist: {project_dir}")

    contents: Dict[str, str] = {}
    for path in sorted(project_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix not in allowed_suffixes:
            continue
        if path.stat().st_size > max_file_bytes:
            continue
        relative = path.relative_to(project_dir).as_posix()
        contents[relative] = path.read_text(encoding="utf-8", errors="replace")
    return contents


def validate_file_bundle(changes: Sequence[FileChange], candidate_root: Path, *,
                          max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> None:
    """Reject a model-generated file bundle that violates a workspace safety rule.

    Raises:
        WorkspaceValidationError: an absolute path, parent-directory traversal, a
            symlinked target, binary content, an oversized change, or an edit
            resolving outside ``candidate_root`` was found.
    """
    candidate_root = Path(candidate_root).resolve()
    for change in changes:
        relative = Path(change.relative_path)

        if relative.is_absolute():
            raise WorkspaceValidationError(f"absolute paths are not allowed: {change.relative_path!r}")

        if ".." in relative.parts:
            raise WorkspaceValidationError(f"path traversal is not allowed: {change.relative_path!r}")

        target = candidate_root / relative
        if target.is_symlink():
            raise WorkspaceValidationError(f"symlinked targets are not allowed: {change.relative_path!r}")

        resolved_target = target.resolve()
        try:
            resolved_target.relative_to(candidate_root)
        except ValueError:
            raise WorkspaceValidationError(
                f"change resolves outside the candidate workspace: {change.relative_path!r}"
            ) from None

        if "\x00" in change.content:
            raise WorkspaceValidationError(f"binary content is not allowed: {change.relative_path!r}")

        encoded_size = len(change.content.encode("utf-8"))
        if encoded_size > max_file_bytes:
            raise WorkspaceValidationError(
                f"change exceeds the maximum file size of {max_file_bytes} bytes: {change.relative_path!r}"
            )


def compute_hash(content: str) -> str:
    """Return the sha256 hex digest of file content, used to bind approval to exact bytes."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_unified_diff(original: str, updated: str, relative_path: str) -> str:
    """Build a unified diff of one file's change, for human review before execution."""
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{relative_path}",
        tofile=f"b/{relative_path}",
    ))


def snapshot_candidate(candidate_root: Path) -> Dict[str, str]:
    """Return a mapping of relative path to content for every file currently in
    the candidate workspace, so a failed repair can be restored exactly.
    """
    candidate_root = Path(candidate_root)
    snapshot: Dict[str, str] = {}
    for path in sorted(candidate_root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(candidate_root).as_posix()
            snapshot[relative] = path.read_text(encoding="utf-8", errors="replace")
    return snapshot


def restore_snapshot(candidate_root: Path, snapshot: Dict[str, str]) -> None:
    """Restore the candidate workspace to exactly the given snapshot.

    Every snapshotted file is rewritten to its snapshotted content, and any file
    present now but absent from the snapshot (e.g. written by a failed repair)
    is deleted. A failed validation must never leave a repaired-but-broken
    candidate standing in place of the last approved baseline.
    """
    candidate_root = Path(candidate_root)
    current_files = {
        path.relative_to(candidate_root).as_posix()
        for path in candidate_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    for relative in current_files - snapshot.keys():
        (candidate_root / relative).unlink()
    for relative, content in snapshot.items():
        target = candidate_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def stage_changes(changes: Sequence[FileChange], candidate_root: Path) -> Dict[str, str]:
    """Validate and write a change bundle into the candidate staging directory.

    Returns:
        A mapping of relative path to the sha256 hash of its staged content.

    Raises:
        WorkspaceValidationError: see ``validate_file_bundle``.
    """
    validate_file_bundle(changes, candidate_root)
    candidate_root = Path(candidate_root)
    hashes: Dict[str, str] = {}
    for change in changes:
        target = candidate_root / change.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.content, encoding="utf-8")
        hashes[change.relative_path] = compute_hash(change.content)
    return hashes
