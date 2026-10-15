"""Tests for execution validation: approval binding, fixed commands, timeouts
and independent tool-owned acceptance checks (validation.py)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sdlc_agent.validation import (
    ApprovalMismatchError,
    AcceptanceCheck,
    create_execution_approval,
    run_acceptance_checks,
    run_fixed_command,
    verify_execution_approval,
)


class ValidationTests(unittest.TestCase):
    def test_approval_binds_exact_candidate_hashes_requirement_and_plan(self):
        approval = create_execution_approval({"app.py": "hash1"}, "req-hash", plan_revision=1)

        verify_execution_approval(approval, {"app.py": "hash1"}, "req-hash", plan_revision=1)  # does not raise

        with self.assertRaises(ApprovalMismatchError):
            verify_execution_approval(approval, {"app.py": "hash2"}, "req-hash", plan_revision=1)
        with self.assertRaises(ApprovalMismatchError):
            verify_execution_approval(approval, {"app.py": "hash1"}, "different-req-hash", plan_revision=1)
        with self.assertRaises(ApprovalMismatchError):
            verify_execution_approval(approval, {"app.py": "hash1"}, "req-hash", plan_revision=2)

    def test_no_approval_rejected(self):
        with self.assertRaises(ApprovalMismatchError):
            verify_execution_approval(None, {"app.py": "hash1"}, "req-hash", plan_revision=1)

    def test_timeout_reported(self):
        def slow_runner(command, cwd, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout, output="partial stdout", stderr="")

        result = run_fixed_command(
            [sys.executable, "-c", "import time; time.sleep(5)"], Path("."),
            timeout_seconds=1, runner=slow_runner,
        )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("partial stdout", result.stdout)

    def test_fixed_command_reports_real_exit_code(self):
        result = run_fixed_command([sys.executable, "-c", "import sys; sys.exit(3)"], Path("."))
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 3)

    def test_output_is_bounded(self):
        def noisy_runner(command, cwd, capture_output, text, timeout):
            return subprocess.CompletedProcess(command, 0, stdout="x" * 1000, stderr="")

        result = run_fixed_command(["noop"], Path("."), max_output_bytes=10, runner=noisy_runner)
        self.assertEqual(len(result.stdout), 10)

    def test_generated_tests_cannot_mask_broken_redirect(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate = Path(tmp_dir)
            # A broken implementation: redirect resolution always reports "not found".
            (candidate / "app.py").write_text(
                "def redirect_target(code):\n    return None\n", encoding="utf-8",
            )
            # A model-generated test that cheats by asserting an unconditional truth
            # instead of exercising the real behavior.
            (candidate / "app_generated_test.py").write_text(
                "def test_always_passes():\n    assert True\n", encoding="utf-8",
            )

            def fake_passing_test_runner(command, cwd, capture_output, text, timeout):
                return subprocess.CompletedProcess(command, 0, stdout="1 passed", stderr="")

            generated_test_result = run_fixed_command(
                [sys.executable, "-m", "pytest"], candidate, runner=fake_passing_test_runner,
            )
            self.assertEqual(generated_test_result.exit_code, 0)  # the cheating test "passes"

            def redirect_actually_resolves(candidate_dir: Path) -> bool:
                script = (
                    "import sys; sys.path.insert(0, sys.argv[1]); import app; "
                    "print(app.redirect_target('abc123') is not None)"
                )
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(candidate_dir)],
                    capture_output=True, text=True, timeout=10, check=True,
                )
                return completed.stdout.strip() == "True"

            results = run_acceptance_checks(
                candidate, [AcceptanceCheck(name="redirect_resolves", check=redirect_actually_resolves)],
            )

            # The independent, tool-owned acceptance check catches what the
            # model-generated test suite was written to mask.
            self.assertFalse(results[0].passed)


if __name__ == "__main__":
    unittest.main()
