"""End-to-end tests exercising the full generated-service flow through a fake
model transport: analysis -> decomposition -> design/risk join -> implementation
staging -> execution approval -> real local compile/test -> independent
acceptance checks -> release approval, plus brownfield reasoning.

The fake transport is deterministic and keyed by task type so these tests are
repeatable without a real LLM or CLI. Where the plan calls for *real* local
validation, these tests run the real ``py -3`` interpreter against generated
files on disk — nothing here fakes compile/test/acceptance results.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sdlc_agent import workflow
from sdlc_agent.validation import (
    AcceptanceCheck,
    run_acceptance_checks,
    run_fixed_command,
)
from sdlc_agent.workspace import compute_hash, read_brownfield_source, stage_changes
from sdlc_agent.workspace import FileChange

APP_SOURCE_V1 = (
    "import sqlite3\n"
    "import re\n\n"
    "VALID_URL_RE = re.compile(r'^https?://')\n\n\n"
    "def init_schema(conn):\n"
    "    conn.execute(\n"
    "        'CREATE TABLE IF NOT EXISTS links ('\n"
    "        'code TEXT PRIMARY KEY, url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0)'\n"
    "    )\n"
    "    conn.commit()\n\n\n"
    "def create_link(conn, code, url):\n"
    "    if not VALID_URL_RE.match(url):\n"
    "        raise ValueError('url must be http(s)')\n"
    "    conn.execute('INSERT INTO links (code, url, clicks) VALUES (?, ?, 0)', (code, url))\n"
    "    conn.commit()\n\n\n"
    "def resolve(conn, code):\n"
    "    row = conn.execute('SELECT url FROM links WHERE code = ?', (code,)).fetchone()\n"
    "    if row is None:\n"
    "        return None\n"
    "    conn.execute('UPDATE links SET clicks = clicks + 1 WHERE code = ?', (code,))\n"
    "    conn.commit()\n"
    "    return row[0]\n\n\n"
    "def click_count(conn, code):\n"
    "    row = conn.execute('SELECT clicks FROM links WHERE code = ?', (code,)).fetchone()\n"
    "    return row[0] if row else None\n"
)

APP_TEST_SOURCE = (
    "import sqlite3\n"
    "import unittest\n\n"
    "import app\n\n\n"
    "class AppTests(unittest.TestCase):\n"
    "    def test_create_and_resolve_redirect(self):\n"
    "        conn = sqlite3.connect(':memory:')\n"
    "        app.init_schema(conn)\n"
    "        app.create_link(conn, 'abc123', 'https://example.com')\n"
    "        self.assertEqual(app.resolve(conn, 'abc123'), 'https://example.com')\n\n"
    "    def test_unknown_code_returns_none(self):\n"
    "        conn = sqlite3.connect(':memory:')\n"
    "        app.init_schema(conn)\n"
    "        self.assertIsNone(app.resolve(conn, 'missing'))\n\n\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)

# Brownfield change: adds optional expiration while preserving existing behavior.
APP_SOURCE_V2_WITH_EXPIRATION = APP_SOURCE_V1.replace(
    "def init_schema(conn):\n"
    "    conn.execute(\n"
    "        'CREATE TABLE IF NOT EXISTS links ('\n"
    "        'code TEXT PRIMARY KEY, url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0)'\n"
    "    )\n"
    "    conn.commit()\n",
    "def init_schema(conn):\n"
    "    conn.execute(\n"
    "        'CREATE TABLE IF NOT EXISTS links ('\n"
    "        'code TEXT PRIMARY KEY, url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0, '\n"
    "        'expires_at TEXT)'\n"
    "    )\n"
    "    conn.commit()\n"
    "    columns = [row[1] for row in conn.execute('PRAGMA table_info(links)').fetchall()]\n"
    "    if 'expires_at' not in columns:\n"
    "        conn.execute('ALTER TABLE links ADD COLUMN expires_at TEXT')\n"
    "        conn.commit()\n",
).replace(
    "def resolve(conn, code):\n"
    "    row = conn.execute('SELECT url FROM links WHERE code = ?', (code,)).fetchone()\n"
    "    if row is None:\n"
    "        return None\n"
    "    conn.execute('UPDATE links SET clicks = clicks + 1 WHERE code = ?', (code,))\n"
    "    conn.commit()\n"
    "    return row[0]\n",
    "def resolve(conn, code, now=None):\n"
    "    row = conn.execute('SELECT url, expires_at FROM links WHERE code = ?', (code,)).fetchone()\n"
    "    if row is None:\n"
    "        return None\n"
    "    url, expires_at = row\n"
    "    if expires_at is not None and now is not None and now > expires_at:\n"
    "        return None\n"
    "    conn.execute('UPDATE links SET clicks = clicks + 1 WHERE code = ?', (code,))\n"
    "    conn.commit()\n"
    "    return url\n",
)


class FakeTransport:
    """Deterministic, scripted ``generate(task_payload, schema)`` keyed by task type."""

    def __init__(self, implementation_files):
        self._implementation_files = implementation_files

    def __call__(self, task_payload: dict, schema: dict) -> dict:
        task = task_payload["task"]
        if task == "analysis":
            return {"summary": "URL shortener service", "key_requirements": ["shorten", "redirect"]}
        if task == "decomposition":
            return {"tasks": [{"task_id": "impl", "stage_type": "implementation"}]}
        if task == "design":
            return {"design": "stdlib sqlite3-backed URL shortener"}
        if task == "risk_assessment":
            return {"risks": ["sqlite write contention under concurrent load"]}
        if task == "implementation":
            return {"files": self._implementation_files}
        raise AssertionError(f"fake transport received an unscripted task: {task!r}")


def _run_generation_pipeline(requirement: str, acceptance_criteria: tuple, candidate_root: Path,
                              implementation_files) -> workflow.WorkflowContext:
    context = workflow.WorkflowContext(
        requirement=requirement, acceptance_criteria=acceptance_criteria, mode="greenfield",
    )
    transport = FakeTransport(implementation_files)
    persisted: list = []

    workflow.run_analysis_stage(context, transport, persist=persisted.append)
    workflow.run_decomposition_stage(context, transport, persist=persisted.append)
    workflow.run_design_and_risk_join(context, transport, persist=persisted.append)
    workflow.run_implementation_stage(context, transport, candidate_root, persist=persisted.append)
    return context


def _redirect_resolves_check(candidate_dir: Path) -> bool:
    script = (
        "import sqlite3, sys; sys.path.insert(0, sys.argv[1]); import app\n"
        "conn = sqlite3.connect(':memory:')\n"
        "app.init_schema(conn)\n"
        "app.create_link(conn, 'abc123', 'https://example.com')\n"
        "print(app.resolve(conn, 'abc123') == 'https://example.com' and app.click_count(conn, 'abc123') == 1)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(candidate_dir)],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return completed.stdout.strip() == "True"


class EndToEndTests(unittest.TestCase):
    def test_greenfield_generates_and_validates_application(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate_root = Path(tmp_dir)
            context = _run_generation_pipeline(
                requirement="Build a URL shortener with SQLite persistence and click counts.",
                acceptance_criteria=("Redirects a known short code to its destination.",),
                candidate_root=candidate_root,
                implementation_files=[
                    {"relative_path": "app.py", "content": APP_SOURCE_V1},
                    {"relative_path": "app_test.py", "content": APP_TEST_SOURCE},
                ],
            )

            self.assertIn("app.py", context.candidate_hashes)
            self.assertEqual(context.candidate_hashes["app.py"], compute_hash(APP_SOURCE_V1))

            # Manual review and execution approval, bound to these exact hashes.
            workflow.request_execution_approval(context)
            workflow.ensure_execution_approved(context)  # does not raise

            # Real local compile and test commands — never model-supplied.
            compile_result = run_fixed_command(
                [sys.executable, "-m", "py_compile", "app.py", "app_test.py"], candidate_root,
            )
            self.assertEqual(compile_result.exit_code, 0, compile_result.stderr)

            test_result = run_fixed_command(
                [sys.executable, "-m", "unittest", "app_test", "-v"], candidate_root,
            )
            self.assertEqual(test_result.exit_code, 0, test_result.stderr)

            # Independent, tool-owned acceptance check — not the model-generated test.
            acceptance_results = run_acceptance_checks(
                candidate_root, [AcceptanceCheck(name="redirect_and_click_count", check=_redirect_resolves_check)],
            )
            self.assertTrue(all(result.passed for result in acceptance_results), acceptance_results)

            context.validation_passed = True
            workflow.request_release_approval(context)
            workflow.ensure_release_approved(context)  # does not raise

    def test_expiration_change_preserves_existing_links(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as candidate_dir:
            source_root = Path(source_dir)
            candidate_root = Path(candidate_dir)

            # The previously generated (approved) greenfield application.
            (source_root / "app.py").write_text(APP_SOURCE_V1, encoding="utf-8")
            (source_root / "app_test.py").write_text(APP_TEST_SOURCE, encoding="utf-8")

            # Brownfield reading: text only, never imported or executed.
            brownfield_source = read_brownfield_source(source_root)
            self.assertIn("app.py", brownfield_source)

            context = _run_generation_pipeline(
                requirement="Add optional link expiration; preserve existing links and APIs.",
                acceptance_criteria=("Existing links still redirect correctly after migration.",),
                candidate_root=candidate_root,
                implementation_files=[
                    {"relative_path": "app.py", "content": APP_SOURCE_V2_WITH_EXPIRATION},
                    {"relative_path": "app_test.py", "content": APP_TEST_SOURCE},
                ],
            )
            workflow.request_execution_approval(context)
            workflow.ensure_execution_approved(context)

            compile_result = run_fixed_command(
                [sys.executable, "-m", "py_compile", "app.py", "app_test.py"], candidate_root,
            )
            self.assertEqual(compile_result.exit_code, 0, compile_result.stderr)

            test_result = run_fixed_command(
                [sys.executable, "-m", "unittest", "app_test", "-v"], candidate_root,
            )
            self.assertEqual(test_result.exit_code, 0, test_result.stderr)  # old behavior still passes

            def _migration_preserves_existing_link_and_adds_expiration(candidate_dir: Path) -> bool:
                script = (
                    "import sqlite3, sys; sys.path.insert(0, sys.argv[1]); import app\n"
                    "conn = sqlite3.connect(':memory:')\n"
                    # Simulate a pre-migration database created by the old schema.
                    "conn.execute(\n"
                    "    'CREATE TABLE links (code TEXT PRIMARY KEY, url TEXT NOT NULL, '\n"
                    "    'clicks INTEGER NOT NULL DEFAULT 0)'\n"
                    ")\n"
                    "conn.execute(\"INSERT INTO links (code, url, clicks) VALUES ('old1', 'https://old.example', 0)\")\n"
                    "conn.commit()\n"
                    "app.init_schema(conn)\n"  # migrates the existing table in place
                    "existing_ok = app.resolve(conn, 'old1') == 'https://old.example'\n"
                    "app.create_link(conn, 'new1', 'https://new.example')\n"
                    "conn.execute(\"UPDATE links SET expires_at = '2000-01-01' WHERE code = 'new1'\")\n"
                    "conn.commit()\n"
                    "expired_rejected = app.resolve(conn, 'new1', now='2099-01-01') is None\n"
                    "print(existing_ok and expired_rejected)\n"
                )
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(candidate_dir)],
                    capture_output=True, text=True, timeout=10, check=True,
                )
                return completed.stdout.strip() == "True"

            acceptance_results = run_acceptance_checks(
                candidate_root,
                [AcceptanceCheck(name="migration_preserves_existing_links",
                                  check=_migration_preserves_existing_link_and_adds_expiration)],
            )
            self.assertTrue(all(result.passed for result in acceptance_results), acceptance_results)

    def test_brownfield_never_mutates_source_project(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as candidate_dir:
            source_root = Path(source_dir)
            candidate_root = Path(candidate_dir)
            (source_root / "app.py").write_text(APP_SOURCE_V1, encoding="utf-8")
            (source_root / "app_test.py").write_text(APP_TEST_SOURCE, encoding="utf-8")

            before = {
                path.relative_to(source_root).as_posix(): path.read_text(encoding="utf-8")
                for path in sorted(source_root.rglob("*")) if path.is_file()
            }

            read_brownfield_source(source_root)
            stage_changes(
                [FileChange(relative_path="app.py", content=APP_SOURCE_V2_WITH_EXPIRATION)], candidate_root,
            )

            after = {
                path.relative_to(source_root).as_posix(): path.read_text(encoding="utf-8")
                for path in sorted(source_root.rglob("*")) if path.is_file()
            }

            self.assertEqual(before, after)  # the approved source project is never changed in place
            # The candidate (a separate workspace) received the new content.
            self.assertEqual((candidate_root / "app.py").read_text(encoding="utf-8"), APP_SOURCE_V2_WITH_EXPIRATION)


if __name__ == "__main__":
    unittest.main()
