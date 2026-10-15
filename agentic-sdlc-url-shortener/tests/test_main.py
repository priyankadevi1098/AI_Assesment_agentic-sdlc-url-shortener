"""Tests for the CLI entry point (``sdlc_agent.__main__``).

These exercise the actual ``run``/``resume`` subcommand dispatch — building
the real argument parser and calling ``main()`` — against a scripted fake
backend transport (no real CLI/API/network call), proving the ``resume``
subcommand is wired for real against ``workflow.run_graph`` and persisted
``state.json``, not merely that the underlying workflow functions work in
isolation.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdlc_agent import __main__ as cli
from sdlc_agent.state import load_state


def _fake_generate(task_payload: dict, schema: dict) -> dict:
    """Deterministic scripted transport, keyed by task type."""
    task = task_payload["task"]
    if task == "analysis":
        return {"summary": "URL shortener", "key_requirements": ["shorten", "redirect"]}
    if task == "decomposition":
        return {"tasks": []}
    if task == "design":
        return {"design": "stdlib sqlite3-backed URL shortener"}
    if task == "risk_assessment":
        return {"risks": ["sqlite write contention under concurrent load"]}
    if task == "implementation":
        return {
            "files": [
                {"relative_path": "app.py", "content": "print('ok')\n"},
                {
                    "relative_path": "tests/test_app.py",
                    "content": (
                        "import unittest\n\n\n"
                        "class AppTests(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        self.assertTrue(True)\n"
                    ),
                },
            ]
        }
    raise AssertionError(f"unscripted task: {task!r}")


def _fake_generate_broken_app(task_payload: dict, schema: dict) -> dict:
    """Same transport, but the implementation stage stages a file that fails
    to compile — used to prove a real validation failure is reported honestly."""
    task = task_payload["task"]
    if task == "implementation":
        return {"files": [{"relative_path": "app.py", "content": "def broken(:\n"}]}
    return _fake_generate(task_payload, schema)


class CliResumeTests(unittest.TestCase):
    """``run`` then ``resume`` driven as real CLI subcommand dispatch."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_path = Path(tmp.name)

        previous_cwd = os.getcwd()
        os.chdir(self._tmp_path)
        self.addCleanup(os.chdir, previous_cwd)

        self.request_path = self._tmp_path / "request.json"
        self.request_path.write_text(json.dumps({
            "name": "shortener",
            "mode": "greenfield",
            "requirement": "Build a URL shortener.",
            "acceptance_criteria": ["Creates short links."],
        }), encoding="utf-8")

        env_patch = patch.dict(os.environ, {"LLM_MODEL": "test-model", "LLM_API_KEY": "test-key"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run_new(self, generate=_fake_generate) -> str:
        with patch.object(cli, "_build_generate_fn", return_value=generate):
            exit_code = cli.main(["run", "--request", str(self.request_path), "--backend", "api", "--yes"])
        run_ids = [p.name for p in (Path("runs")).iterdir()]
        self.assertEqual(len(run_ids), 1, "expected exactly one run directory to be created")
        return run_ids[0], exit_code

    def _state(self, run_id: str):
        return load_state(Path("runs") / run_id / "state.json")

    def test_run_pauses_for_execution_approval_and_never_claims_completed(self):
        run_id, exit_code = self._run_new()
        self.assertEqual(exit_code, cli.EXIT_OK)

        state = self._state(run_id)
        self.assertEqual(state.status, "awaiting_execution_approval")
        self.assertNotIn("completed", state.status)

    def test_resume_approve_execute_candidate_runs_real_validation(self):
        run_id, _ = self._run_new()

        with patch.object(cli, "_build_generate_fn", return_value=_fake_generate):
            exit_code = cli.main(["resume", "--run", run_id, "--approve", "execute_candidate"])

        self.assertEqual(exit_code, cli.EXIT_OK)
        state = self._state(run_id)
        # Real validation ran (compile + the generated unit test) and passed,
        # advancing past execution approval to the separate release gate.
        self.assertEqual(state.status, "awaiting_release_approval")
        self.assertTrue(state.stages["validation_passed"])
        self.assertIsNotNone(state.stages["execution_approval"])
        self.assertFalse(state.stages["release_approved"])

    def test_resume_approve_execute_candidate_reports_real_validation_failure(self):
        run_id, _ = self._run_new(generate=_fake_generate_broken_app)

        with patch.object(cli, "_build_generate_fn", return_value=_fake_generate_broken_app):
            exit_code = cli.main(["resume", "--run", run_id, "--approve", "execute_candidate"])

        self.assertEqual(exit_code, cli.EXIT_VALIDATION_FAILED)
        state = self._state(run_id)
        self.assertEqual(state.status, "validation_failed")
        self.assertFalse(state.stages["validation_passed"])

    def test_resume_approve_release_ready_records_a_separate_approval(self):
        run_id, _ = self._run_new()
        with patch.object(cli, "_build_generate_fn", return_value=_fake_generate):
            cli.main(["resume", "--run", run_id, "--approve", "execute_candidate"])

            exit_code = cli.main(["resume", "--run", run_id, "--approve", "release_ready"])

        self.assertEqual(exit_code, cli.EXIT_OK)
        state = self._state(run_id)
        self.assertEqual(state.status, "completed")
        self.assertTrue(state.stages["release_approved"])
        # Release approval is recorded separately from, and does not retract,
        # execution approval.
        self.assertIsNotNone(state.stages["execution_approval"])

    def test_resume_release_ready_before_validation_is_rejected_not_silently_granted(self):
        run_id, _ = self._run_new()

        with patch.object(cli, "_build_generate_fn", return_value=_fake_generate):
            exit_code = cli.main(["resume", "--run", run_id, "--approve", "release_ready"])

        self.assertEqual(exit_code, cli.EXIT_VALIDATION_ERROR)
        state = self._state(run_id)
        self.assertFalse(state.stages["release_approved"])

    def test_resume_answers_invalidates_and_continues_past_the_pause(self):
        run_id, _ = self._run_new()
        first_hashes = dict(self._state(run_id).stages["candidate_hashes"])

        answers_path = self._tmp_path / "answers.json"
        answers_path.write_text(json.dumps({"domain": "short links only, no custom aliases"}),
                                 encoding="utf-8")

        with patch.object(cli, "_build_generate_fn", return_value=_fake_generate):
            exit_code = cli.main(["resume", "--run", run_id, "--answers", str(answers_path)])

        self.assertEqual(exit_code, cli.EXIT_OK)
        state = self._state(run_id)
        # Applying answers invalidated analysis onward and re-derived a fresh
        # candidate, then paused again at the same execution-approval gate —
        # proving the pause was unblocked and the walk actually continued.
        self.assertEqual(state.stages["answers"], {"domain": "short links only, no custom aliases"})
        self.assertEqual(state.status, "awaiting_execution_approval")
        self.assertIn("analysis", state.stages["invalidated_stages"])
        self.assertEqual(state.stages["candidate_hashes"], first_hashes)  # same scripted content, re-derived

    def test_resume_request_reports_invalidated_stages_and_bumps_plan_revision(self):
        run_id, _ = self._run_new()

        new_request_path = self._tmp_path / "updated_request.json"
        new_request_path.write_text(json.dumps({
            "name": "shortener",
            "mode": "greenfield",
            "requirement": "Build a URL shortener with click analytics.",
            "acceptance_criteria": ["Creates short links.", "Tracks click counts."],
        }), encoding="utf-8")

        with patch.object(cli, "_build_generate_fn", return_value=_fake_generate):
            exit_code = cli.main(["resume", "--run", run_id, "--request", str(new_request_path)])

        self.assertEqual(exit_code, cli.EXIT_OK)
        state = self._state(run_id)
        self.assertEqual(state.stages["requirement"], "Build a URL shortener with click analytics.")
        self.assertEqual(state.stages["plan_revision"], 1)
        self.assertIn("analysis", state.stages["invalidated_stages"])
        self.assertIn("validation", state.stages["invalidated_stages"])

    def test_resume_rejects_conflicting_actions_in_one_call(self):
        run_id, _ = self._run_new()
        answers_path = self._tmp_path / "answers.json"
        answers_path.write_text("{}", encoding="utf-8")

        exit_code = cli.main([
            "resume", "--run", run_id, "--approve", "execute_candidate", "--answers", str(answers_path),
        ])

        self.assertEqual(exit_code, cli.EXIT_VALIDATION_ERROR)


if __name__ == "__main__":
    unittest.main()
