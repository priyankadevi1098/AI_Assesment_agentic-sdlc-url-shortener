"""Tests for brownfield reading and file-bundle validation (workspace.py)."""
import os
import tempfile
import unittest
from pathlib import Path

from sdlc_agent.workspace import (
    FileChange,
    WorkspaceValidationError,
    build_unified_diff,
    compute_hash,
    read_brownfield_source,
    restore_snapshot,
    snapshot_candidate,
    stage_changes,
    validate_file_bundle,
)


class WorkspaceTests(unittest.TestCase):
    def test_read_brownfield_source_skips_disallowed_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "app.py").write_text("ok", encoding="utf-8")
            (root / "data.bin").write_bytes(b"\x00\x01\x02")
            oversized = root / "big.py"
            oversized.write_text("x" * 50, encoding="utf-8")

            contents = read_brownfield_source(root, allowed_suffixes=(".py",), max_file_bytes=10)

            self.assertIn("app.py", contents)
            self.assertNotIn("data.bin", contents)
            self.assertNotIn("big.py", contents)  # exceeds max_file_bytes

    def test_read_brownfield_source_missing_directory_rejected(self):
        with self.assertRaises(WorkspaceValidationError):
            read_brownfield_source(Path("does-not-exist-anywhere"))

    def test_traversal_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir) / "candidate"
            candidate_root.mkdir()

            # Path traversal must be rejected before anything touches disk.
            with self.assertRaises(WorkspaceValidationError) as traversal_ctx:
                validate_file_bundle(
                    [FileChange(relative_path="../escape.py", content="print('x')")],
                    candidate_root,
                )
            self.assertIn("traversal", str(traversal_ctx.exception))

            # A symlinked target inside the candidate root must also be rejected.
            outside_target = Path(tmp_dir) / "outside.py"
            outside_target.write_text("print('outside')", encoding="utf-8")
            symlink_path = candidate_root / "linked.py"
            try:
                os.symlink(outside_target, symlink_path)
            except OSError:
                self.skipTest("creating symlinks is not permitted on this platform/account")

            with self.assertRaises(WorkspaceValidationError) as symlink_ctx:
                validate_file_bundle(
                    [FileChange(relative_path="linked.py", content="print('replaced')")],
                    candidate_root,
                )
            self.assertIn("symlink", str(symlink_ctx.exception))

    def test_absolute_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir)
            with self.assertRaises(WorkspaceValidationError):
                validate_file_bundle(
                    [FileChange(relative_path=str(Path(tmp_dir) / "abs.py"), content="x")],
                    candidate_root,
                )

    def test_binary_content_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(WorkspaceValidationError):
                validate_file_bundle(
                    [FileChange(relative_path="app.py", content="print('x')\x00")],
                    Path(tmp_dir),
                )

    def test_oversized_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(WorkspaceValidationError):
                validate_file_bundle(
                    [FileChange(relative_path="app.py", content="x" * 100)],
                    Path(tmp_dir),
                    max_file_bytes=10,
                )

    def test_stage_changes_writes_files_and_returns_hashes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir)
            change = FileChange(relative_path="app.py", content="print('hello')")
            hashes = stage_changes([change], candidate_root)

            self.assertEqual((candidate_root / "app.py").read_text(encoding="utf-8"), "print('hello')")
            self.assertEqual(hashes["app.py"], compute_hash("print('hello')"))

    def test_build_unified_diff_shows_change(self):
        diff = build_unified_diff("old line\n", "new line\n", "app.py")
        self.assertIn("-old line", diff)
        self.assertIn("+new line", diff)

    def test_failed_repair_restores_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir)
            (candidate_root / "app.py").write_text("good = 1\n", encoding="utf-8")
            snapshot = snapshot_candidate(candidate_root)

            # Simulate a repair that both breaks an existing file and adds a new one.
            (candidate_root / "app.py").write_text("broken = (\n", encoding="utf-8")
            (candidate_root / "extra.py").write_text("leftover from failed repair\n", encoding="utf-8")

            restore_snapshot(candidate_root, snapshot)

            self.assertEqual((candidate_root / "app.py").read_text(encoding="utf-8"), "good = 1\n")
            self.assertFalse((candidate_root / "extra.py").exists())


if __name__ == "__main__":
    unittest.main()
