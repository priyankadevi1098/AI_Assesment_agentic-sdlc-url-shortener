"""Tests for backend selection, CLI adapter preflight and permission enforcement."""
import unittest
from typing import Optional

from sdlc_agent.backends import (
    BackendLock,
    BackendSelectionAmbiguousError,
    BackendSelector,
    CliCapability,
    DestinationApprovalRequiredError,
    MidRunFallbackProhibitedError,
    NoCompatibleBackendError,
    ensure_tools_disabled,
    require_destination_approval,
)
from sdlc_agent.cli_backends import (
    ClaudeCodeCliAdapter,
    CliUnsupportedError,
    CodexCliAdapter,
    CopilotCliAdapter,
    VerifiedCliProfile,
)


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(version_output: str, *, called: Optional[list] = None):
    def runner(args, **kwargs):
        if called is not None:
            called.append(args)
        return _FakeCompletedProcess(stdout=version_output)
    return runner


def _sequenced_fake_runner(version_output: str, invocation_output: str, *, called: list):
    """A fake runner returning the version check output first, then the
    invocation's own (structured-JSON) stdout on the second call."""
    responses = iter([version_output, invocation_output])

    def runner(args, **kwargs):
        called.append(args)
        return _FakeCompletedProcess(stdout=next(responses))
    return runner


def _fake_which(available_name: str):
    def which(name):
        return f"/usr/bin/{name}" if name == available_name else None
    return which


class _FakeCapabilityAdapter:
    """A minimal fake standing in for a CliAdapter in selector tests."""

    def __init__(self, name: str, supported: bool, reason: Optional[str] = None):
        self._capability = CliCapability(
            name=name, executable=f"/usr/bin/{name}" if supported else None,
            version="1.2.3" if supported else None,
            supports_inference_only=supported, supported=supported, reason=reason,
        )

    def preflight(self) -> CliCapability:
        return self._capability


class BackendTests(unittest.TestCase):
    def test_copilot_has_no_verified_profile_and_reports_investigated_reason(self):
        # Copilot CLI is installed in this environment; its own --help documents
        # --allow-all-tools as required for non-interactive mode, a broad
        # permission grant this workflow refuses to use, so it stays unsupported
        # with a specific, investigated reason rather than the generic default.
        adapter = CopilotCliAdapter(
            runner=_fake_runner("copilot version 1.4.2\n"),
            which=_fake_which("copilot"),
        )
        capability = adapter.preflight()
        self.assertIsNotNone(capability.version)
        self.assertFalse(capability.supported)
        self.assertIn("required for non-interactive mode", capability.reason)

    def test_claude_code_below_verified_minimum_is_unsupported(self):
        adapter = ClaudeCodeCliAdapter(
            runner=_fake_runner("claude-code v2.0.0 (build 991)\n"),
            which=_fake_which("claude"),
        )
        capability = adapter.preflight()
        self.assertIsNotNone(capability.version)
        self.assertFalse(capability.supported)
        self.assertIn("older than verified minimum", capability.reason)

    def test_claude_code_at_verified_minimum_is_supported(self):
        adapter = ClaudeCodeCliAdapter(
            runner=_fake_runner("claude-code v2.1.296\n"),
            which=_fake_which("claude"),
        )
        capability = adapter.preflight()
        self.assertTrue(capability.supported)
        self.assertTrue(capability.supports_inference_only)

    def test_codex_below_verified_minimum_is_unsupported(self):
        adapter = CodexCliAdapter(
            runner=_fake_runner("codex-cli 0.9.1\n"),
            which=_fake_which("codex"),
        )
        capability = adapter.preflight()
        self.assertIsNotNone(capability.version)
        self.assertFalse(capability.supported)
        self.assertIn("older than verified minimum", capability.reason)

    def test_codex_at_verified_minimum_is_supported(self):
        adapter = CodexCliAdapter(
            runner=_fake_runner("codex-cli 0.162.1\n"),
            which=_fake_which("codex"),
        )
        capability = adapter.preflight()
        self.assertTrue(capability.supported)
        self.assertTrue(capability.supports_inference_only)

    def test_claude_code_invocation_builds_restricted_argv_and_parses_response(self):
        # The verified profile must disable every tool (--disallowedTools "*"),
        # deny rather than bypass permission prompts (--permission-prompts none),
        # and request structured output -- never a permission-bypass flag.
        called: list = []
        adapter = ClaudeCodeCliAdapter(
            runner=_sequenced_fake_runner(
                "claude-code v2.1.296\n", '{"result": "ok"}', called=called,
            ),
            which=_fake_which("claude"),
        )
        response = adapter.invoke(
            {"task": "analysis"},
            schema={"required": ["task"], "properties": {}, "response": {"required": ["result"], "properties": {}}},
        )
        self.assertEqual(response, {"result": "ok"})
        invocation_args = called[1]
        self.assertIn("--disallowedTools", invocation_args)
        self.assertIn("*", invocation_args)
        self.assertIn("--permission-prompts", invocation_args)
        self.assertIn("none", invocation_args)
        self.assertIn("--output-format", invocation_args)
        self.assertIn("json", invocation_args)
        self.assertNotIn("--dangerously-skip-permissions", invocation_args)

    def test_codex_invocation_builds_restricted_argv_and_parses_response(self):
        # The verified profile must run the non-interactive exec subcommand with
        # an OS-enforced read-only sandbox and suppressed (not bypassed) prompts.
        called: list = []
        adapter = CodexCliAdapter(
            runner=_sequenced_fake_runner(
                "codex-cli 0.162.1\n", '{"result": "ok"}', called=called,
            ),
            which=_fake_which("codex"),
        )
        response = adapter.invoke(
            {"task": "analysis"},
            schema={"required": ["task"], "properties": {}, "response": {"required": ["result"], "properties": {}}},
        )
        self.assertEqual(response, {"result": "ok"})
        invocation_args = called[1]
        self.assertIn("exec", invocation_args)
        self.assertIn("--sandbox", invocation_args)
        self.assertIn("read-only", invocation_args)
        self.assertIn("--ask-for-approval", invocation_args)
        self.assertIn("never", invocation_args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", invocation_args)
        self.assertNotIn("--yolo", invocation_args)

    def test_installed_but_incompatible_cli_rejected(self):
        profile = VerifiedCliProfile(min_version="2.0.0", inference_only_args=("--no-shell-tools",))
        adapter = CopilotCliAdapter(
            runner=_fake_runner("copilot version 1.0.0\n"),
            which=_fake_which("copilot"),
            verified_profile=profile,
        )
        capability = adapter.preflight()
        self.assertFalse(capability.supported)
        self.assertIn("older than verified minimum", capability.reason)

    def test_executable_not_found_marks_unsupported(self):
        adapter = CopilotCliAdapter(runner=_fake_runner(""), which=_fake_which("something-else"))
        capability = adapter.preflight()
        self.assertFalse(capability.supported)
        self.assertIn("not found on PATH", capability.reason)

    def test_multiple_backends_require_preference(self):
        selector = BackendSelector([
            _FakeCapabilityAdapter("copilot", supported=True),
            _FakeCapabilityAdapter("codex", supported=True),
        ])
        with self.assertRaises(BackendSelectionAmbiguousError) as ctx:
            selector.select("auto")
        self.assertIn("copilot", ctx.exception.available)
        self.assertIn("codex", ctx.exception.available)

    def test_multiple_backends_with_preference_selects_preferred(self):
        selector = BackendSelector([
            _FakeCapabilityAdapter("copilot", supported=True),
            _FakeCapabilityAdapter("codex", supported=True),
        ])
        chosen = selector.select("auto", preference="codex")
        self.assertEqual(chosen, "codex")

    def test_single_usable_backend_auto_selected(self):
        selector = BackendSelector([
            _FakeCapabilityAdapter("copilot", supported=True),
            _FakeCapabilityAdapter("codex", supported=False, reason="not installed"),
        ])
        chosen = selector.select("auto")
        self.assertEqual(chosen, "copilot")

    def test_no_compatible_backend_raises(self):
        selector = BackendSelector([
            _FakeCapabilityAdapter("copilot", supported=False, reason="not installed"),
        ])
        with self.assertRaises(NoCompatibleBackendError):
            selector.select("auto", api_available=False)

    def test_preflight_fallback_requires_destination_approval(self):
        with self.assertRaises(DestinationApprovalRequiredError) as ctx:
            require_destination_approval("api", approver=lambda name: False)
        self.assertEqual(ctx.exception.backend_name, "api")

        # Approval granted does not raise.
        require_destination_approval("api", approver=lambda name: True)

    def test_no_silent_midrun_fallback(self):
        lock = BackendLock(locked_backend="copilot")
        with self.assertRaises(MidRunFallbackProhibitedError) as ctx:
            lock.ensure("api")
        self.assertEqual(ctx.exception.locked_backend, "copilot")
        self.assertEqual(ctx.exception.requested_backend, "api")

        # Re-approved switch does not raise.
        lock.ensure("api", reapproved=True)

    def test_tools_disabled_before_cli_invocation(self):
        # A profile that omits a required disable marker (here, the
        # --permission-prompts none pair Claude Code's real
        # REQUIRED_DISABLE_MARKERS requires) must block invocation before the
        # subprocess runner is ever called.
        profile = VerifiedCliProfile(min_version="1.0.0", inference_only_args=("-p", "--disallowedTools", "*"))
        called: list = []
        adapter = ClaudeCodeCliAdapter(
            runner=_fake_runner("claude-code v2.1.296\n", called=called),
            which=_fake_which("claude"),
            verified_profile=profile,
        )
        with self.assertRaises(PermissionError):
            adapter.invoke({"task": "analysis"}, schema={"required": ["task"], "properties": {}})
        # Only the non-mutating preflight version check ran; the actual task
        # invocation (which would carry the task payload as stdin) never fired.
        self.assertEqual(called, [["/usr/bin/claude", "--version"]])

    def test_tools_disabled_check_passes_with_full_marker_set(self):
        ensure_tools_disabled(
            ["--disallowedTools", "*", "--permission-prompts", "none"],
            ["--disallowedTools", "*", "--permission-prompts", "none"],
        )

    def test_invoke_raises_for_unsupported_cli(self):
        adapter = CopilotCliAdapter(runner=_fake_runner(""), which=_fake_which("something-else"))
        with self.assertRaises(CliUnsupportedError):
            adapter.invoke({"task": "analysis"}, schema={"required": ["task"], "properties": {}})


if __name__ == "__main__":
    unittest.main()
