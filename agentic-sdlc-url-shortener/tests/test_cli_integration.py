"""Public CLI integration tests: drive ``py -3 -m sdlc_agent`` as a real
subprocess, exactly as a user would type it, against a local loopback HTTP
fixture standing in for the ``api`` backend.

This is deliberately *not* a call into the library's internal functions (that
is what ``tests/test_main.py`` already covers via ``cli.main([...])`` in the
same process with a patched generate function): every assertion here reads
real subprocess ``stdout``/exit codes and real ``state.json`` content written
by a separate ``py -3 -m sdlc_agent`` process, proving the command-line
surface itself — argument parsing, process exit codes, and the ``--backend
api``/``LLM_BASE_URL`` wiring through to ``llm.ApiBackend`` — works for a real
user invocation, not just the orchestration functions underneath it.

The fixture HTTP server is a standard-library ``ThreadingHTTPServer`` bound to
``127.0.0.1`` on an OS-assigned free port. ``LLM_BASE_URL`` points at it as
plain loopback HTTP, which ``llm.LlmConfig.from_env`` already documents and
allows as the one exception to its HTTPS-only rule (see
``docs/backend-setup.md``) — this is never a real network or LLM call.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from sdlc_agent.state import load_state

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBPROCESS_TIMEOUT_SECONDS = 120

# Canned, deterministic implementation file bundle the fixture server returns
# for the "implementation" task — a minimal application plus its own `tests`
# directory, since the CLI's real validation commands run
# `py -3 -m compileall -q .` and `py -3 -m unittest discover -s tests -q`
# against whatever is staged in the candidate workspace.
_IMPLEMENTATION_FILES = [
    {
        "relative_path": "app.py",
        "content": (
            "\"\"\"Fixture candidate application used only by the CLI "
            "integration tests; never executed as a real service here.\"\"\"\n"
        ),
    },
    {
        "relative_path": "tests/test_app.py",
        "content": (
            "import unittest\n\n\n"
            "class AppTests(unittest.TestCase):\n"
            "    def test_fixture_app_compiles_and_passes(self):\n"
            "        self.assertTrue(True)\n"
        ),
    },
]


def _fixture_response_for(task: str) -> dict:
    """Canned structured JSON response per task type, matching the schemas in
    ``sdlc_agent/prompts.py``."""
    if task == "analysis":
        return {"summary": "URL shortener service", "key_requirements": ["shorten", "redirect"]}
    if task == "decomposition":
        return {"tasks": [{"task_id": "impl", "stage_type": "implementation"}]}
    if task == "design":
        return {"design": "stdlib sqlite3-backed URL shortener"}
    if task == "risk_assessment":
        return {"risks": ["sqlite write contention under concurrent load"]}
    if task == "implementation":
        return {"files": _IMPLEMENTATION_FILES}
    raise AssertionError(f"fixture server received an unscripted task: {task!r}")


class _FixtureRequestHandler(http.server.BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible-shaped responder for ``POST {base_url}/responses``."""

    def log_message(self, format, *args):  # noqa: A002 - silence per-request access logs
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body)
            task = body["input"]["task"]
            response_body = json.dumps(_fixture_response_for(task)).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 - surface the fixture's own failure as a 500
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(exc).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


class CliIntegrationTests(unittest.TestCase):
    """Drives the real ``py -3 -m sdlc_agent`` CLI as a subprocess against the
    loopback fixture server, asserting on subprocess stdout/exit codes and on
    ``state.json`` contents."""

    @classmethod
    def setUpClass(cls):
        cls._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureRequestHandler)
        cls._server_thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._server_thread.start()
        cls._port = cls._server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()
        cls._server_thread.join(timeout=5)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._run_cwd = Path(tmp.name)

        self._env = dict(os.environ)
        self._env.pop("LLM_API_KEY", None)  # loopback HTTP needs none; prove that exception holds
        self._env["LLM_MODEL"] = "fixture-model"
        self._env["LLM_BASE_URL"] = f"http://127.0.0.1:{self._port}"
        existing_pythonpath = self._env.get("PYTHONPATH", "")
        self._env["PYTHONPATH"] = (
            str(REPO_ROOT) if not existing_pythonpath else f"{REPO_ROOT}{os.pathsep}{existing_pythonpath}"
        )

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "sdlc_agent", *args],
            cwd=str(self._run_cwd), env=self._env,
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

    def _state(self, run_id: str):
        return load_state(self._run_cwd / "runs" / run_id / "state.json")

    def _run_id_from_run_output(self, completed: subprocess.CompletedProcess) -> str:
        for line in completed.stdout.splitlines():
            if line.startswith("run created: "):
                return line[len("run created: "):].strip()
        self.fail(f"no 'run created: <id>' line in CLI output:\n{completed.stdout}\n{completed.stderr}")

    def test_greenfield_run_resume_release_and_show_as_real_subprocesses(self):
        request_path = REPO_ROOT / "examples" / "requests" / "greenfield.json"

        run_completed = self._run_cli(
            "run", "--request", str(request_path), "--backend", "api", "--yes",
        )
        self.assertEqual(run_completed.returncode, 0, run_completed.stderr)
        self.assertIn("backend selected: api", run_completed.stdout)
        self.assertIn("awaiting execution approval", run_completed.stdout)
        run_id = self._run_id_from_run_output(run_completed)

        state = self._state(run_id)
        self.assertEqual(state.status, "awaiting_execution_approval")
        self.assertEqual(state.backend, "api")

        execute_completed = self._run_cli("resume", "--run", run_id, "--approve", "execute_candidate")
        self.assertEqual(execute_completed.returncode, 0, execute_completed.stderr)
        self.assertIn("awaiting release-readiness approval", execute_completed.stdout)

        state = self._state(run_id)
        self.assertEqual(state.status, "awaiting_release_approval")
        self.assertTrue(state.stages["validation_passed"])
        self.assertIsNotNone(state.stages["execution_approval"])
        self.assertFalse(state.stages["release_approved"])

        release_completed = self._run_cli("resume", "--run", run_id, "--approve", "release_ready")
        self.assertEqual(release_completed.returncode, 0, release_completed.stderr)
        self.assertIn("status: completed", release_completed.stdout)

        state = self._state(run_id)
        self.assertEqual(state.status, "completed")
        self.assertTrue(state.stages["release_approved"])
        # Release approval is recorded separately from, and never retracts, execution approval.
        self.assertIsNotNone(state.stages["execution_approval"])

        show_completed = self._run_cli("show", "--run", run_id)
        self.assertEqual(show_completed.returncode, 0, show_completed.stderr)
        self.assertIn(f"run: {run_id}", show_completed.stdout)
        self.assertIn("backend: api", show_completed.stdout)
        self.assertIn("status: completed", show_completed.stdout)

    def test_ambiguous_request_pauses_then_resumes_with_answers(self):
        request_path = REPO_ROOT / "examples" / "requests" / "ambiguous.json"
        answers_path = REPO_ROOT / "examples" / "requests" / "ambiguous-answers.json"

        run_completed = self._run_cli(
            "run", "--request", str(request_path), "--backend", "api", "--yes",
        )
        self.assertEqual(run_completed.returncode, 0, run_completed.stderr)
        run_id = self._run_id_from_run_output(run_completed)

        state = self._state(run_id)
        self.assertEqual(state.status, "awaiting_execution_approval")
        first_candidate_hashes = dict(state.stages["candidate_hashes"])

        answers_completed = self._run_cli("resume", "--run", run_id, "--answers", str(answers_path))
        self.assertEqual(answers_completed.returncode, 0, answers_completed.stderr)
        self.assertIn("clarification answers applied", answers_completed.stdout)
        self.assertIn("awaiting execution approval", answers_completed.stdout)

        state = self._state(run_id)
        # The shipped ambiguous-answers.json is request-shaped (name/mode/
        # requirement/acceptance_criteria plus a nested "answers" object); the
        # CLI must unwrap that nested object rather than folding the whole
        # document's unrelated top-level fields into context.answers.
        expected_answers = json.loads(answers_path.read_text(encoding="utf-8"))["answers"]
        self.assertEqual(state.stages["answers"], expected_answers)
        self.assertNotIn("requirement", state.stages["answers"])
        self.assertNotIn("mode", state.stages["answers"])
        self.assertEqual(state.status, "awaiting_execution_approval")
        self.assertIn("analysis", state.stages["invalidated_stages"])
        # Same scripted fixture content, re-derived after invalidation.
        self.assertEqual(state.stages["candidate_hashes"], first_candidate_hashes)


if __name__ == "__main__":
    unittest.main()
