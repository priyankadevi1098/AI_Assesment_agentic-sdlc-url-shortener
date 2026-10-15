"""Tests for run state persistence (state.py)."""
import tempfile
import unittest
from pathlib import Path

from sdlc_agent.state import RunState, StateError, create_run_directory, load_state, save_state


class StateTests(unittest.TestCase):
    def test_create_run_directory_is_unique(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir) / "runs"
            first = create_run_directory(base)
            second = create_run_directory(base)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())
            self.assertNotEqual(first, second)

    def test_save_and_load_state_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir)
            state = RunState(run_id="abc123", request_name="sample", mode="greenfield", backend="api")
            state_path = run_dir / "state.json"
            save_state(state_path, state)

            loaded = load_state(state_path)
            self.assertEqual(loaded.run_id, "abc123")
            self.assertEqual(loaded.backend, "api")
            self.assertEqual(loaded.status, "created")

    def test_save_state_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir)
            state = RunState(run_id="abc123", request_name="sample", mode="greenfield", backend="api")
            save_state(run_dir / "state.json", state)
            leftover_temp_files = list(run_dir.glob(".state-*.tmp"))
            self.assertEqual(leftover_temp_files, [])

    def test_load_state_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(StateError):
                load_state(Path(tmp_dir) / "missing-state.json")


if __name__ == "__main__":
    unittest.main()
