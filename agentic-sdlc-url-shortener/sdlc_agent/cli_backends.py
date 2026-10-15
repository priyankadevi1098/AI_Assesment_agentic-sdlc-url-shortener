"""CLI coding-assistant adapters: Copilot, Claude Code and Codex.

Each adapter's compatibility is the result of a real, evidence-backed
investigation recorded in ``docs/backend-setup.md`` — the exact ``--help``
excerpt examined (for a CLI installed in the implementation environment) or the
exact public documentation cited (for one that was not), plus the specific
reason it is supported or unsupported. ``VERIFIED_CLI_CAPABILITIES`` only ever
gains an entry once that evidence shows a genuine non-interactive, inference-only
flag combination with shell, file-write and network tool access disabled; no
flag here is invented, and no entry here uses a permission-bypass flag
(``--yolo``, ``--dangerously-skip-permissions``, ``--allow-all-tools``,
``--dangerously-bypass-approvals-and-sandbox``, or any equivalent). A CLI whose
own documentation does not expose a way to enforce that bar stays unsupported
for this workflow — see ``docs/backend-setup.md`` for the Copilot CLI finding.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence

from .backends import CliCapability, ensure_tools_disabled

DEFAULT_TIMEOUT_SECONDS = 30


class CliUnsupportedError(RuntimeError):
    """Raised when invocation is attempted against a CLI that preflight marked unsupported."""


@dataclass(frozen=True)
class VerifiedCliProfile:
    """A CLI version whose non-interactive, inference-only invocation has been
    confirmed against real documentation/testing, and the exact flags that enforce it."""

    min_version: str
    inference_only_args: Sequence[str]


# Populated only after confirming the real flags against the CLI's own
# --help/documentation (see docs/backend-setup.md for the evidence behind each
# entry); never invent one here. Copilot CLI has no entry: its own --help marks
# --allow-all-tools "required for non-interactive mode", a broad permission
# grant this workflow refuses to use, so it stays unsupported.
VERIFIED_CLI_CAPABILITIES: Dict[str, VerifiedCliProfile] = {
    "claude-code": VerifiedCliProfile(
        min_version="2.1.296",
        inference_only_args=(
            "-p",
            "Process the JSON task provided on stdin and respond using the requested JSON schema.",
            "--disallowedTools", "*",
            "--permission-prompts", "none",
            "--output-format", "json",
        ),
    ),
    "codex": VerifiedCliProfile(
        min_version="0.162.1",
        inference_only_args=(
            "exec", "--sandbox", "read-only", "--ask-for-approval", "never", "--json", "-",
        ),
    ),
}


def _version_tuple(version: str) -> tuple:
    parts = []
    for part in version.split("."):
        parts.append(int(part) if part.isdigit() else 0)
    return tuple(parts)


class CliAdapter:
    """Shared preflight/invocation behaviour for a coding-assistant CLI.

    Required disable markers and the verified profile are both injectable so
    tests can exercise the enforcement logic without a real CLI installed.
    """

    name: str = "cli"
    executable_names: Sequence[str] = ()
    version_args: Sequence[str] = ("--version",)
    REQUIRED_DISABLE_MARKERS: Sequence[str] = ()
    # Set on a subclass to record a specific, investigated reason a CLI with no
    # verified profile is unsupported (quoting its own --help/documentation),
    # instead of the generic fail-closed message below.
    UNVERIFIABLE_REASON: Optional[str] = None

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 verified_profile: Optional[VerifiedCliProfile] = None):
        self._runner = runner
        self._which = which
        self._verified_profile = verified_profile if verified_profile is not None else \
            VERIFIED_CLI_CAPABILITIES.get(self.name)

    def _find_executable(self) -> Optional[str]:
        for candidate in self.executable_names:
            found = self._which(candidate)
            if found:
                return found
        return None

    def parse_version(self, raw_output: str) -> Optional[str]:
        """Extract a dotted version number from a CLI's ``--version`` output."""
        for token in raw_output.split():
            stripped = token.strip("v,()")
            parts = stripped.split(".")
            if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
                return stripped
        return None

    def preflight(self) -> CliCapability:
        """Run a bounded, non-mutating capability check: find the executable,
        read its version, and compare against any verified profile."""
        executable = self._find_executable()
        if executable is None:
            return CliCapability(
                name=self.name, executable=None, version=None,
                supports_inference_only=False, supported=False,
                reason=f"{self.name} executable not found on PATH",
            )

        try:
            result = self._runner(
                [executable, *self.version_args],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CliCapability(
                name=self.name, executable=executable, version=None,
                supports_inference_only=False, supported=False,
                reason=f"{self.name} version check failed: {exc}",
            )

        version = self.parse_version((result.stdout or "") + (result.stderr or ""))

        if self._verified_profile is None:
            reason = self.UNVERIFIABLE_REASON or (
                f"{self.name} non-interactive inference-only flags are not yet "
                "verified against a real installation; marking unsupported rather "
                "than granting broad permissions"
            )
            return CliCapability(
                name=self.name, executable=executable, version=version,
                supports_inference_only=False, supported=False, reason=reason,
            )

        if version is None or _version_tuple(version) < _version_tuple(self._verified_profile.min_version):
            return CliCapability(
                name=self.name, executable=executable, version=version,
                supports_inference_only=False, supported=False,
                reason=f"{self.name} version {version} is older than verified minimum "
                       f"{self._verified_profile.min_version}",
            )

        return CliCapability(
            name=self.name, executable=executable, version=version,
            supports_inference_only=True, supported=True, reason=None,
        )

    def build_inference_only_args(self) -> Sequence[str]:
        """Return the verified permission-restricting flags for this CLI.

        Raises:
            CliUnsupportedError: no verified profile exists for this CLI.
        """
        if self._verified_profile is None:
            raise CliUnsupportedError(
                f"{self.name} has no verified inference-only flag set; refusing to invoke"
            )
        return self._verified_profile.inference_only_args

    def invoke(self, task_payload: dict, *, schema: dict) -> dict:
        """Send a structured task to the CLI and return its validated JSON response.

        Enforces, in order: the shared task schema, CLI compatibility (preflight),
        and inference-only permission flags — the subprocess is never started if
        any of those checks fail.

        Raises:
            CliUnsupportedError: preflight marked this CLI unsupported, or no
                verified inference-only flag set exists.
            PermissionError: the verified flag set is missing a required
                permission-restricting marker.
        """
        from .llm import validate_schema  # shared structured-task contract

        validate_schema(task_payload, schema)

        capability = self.preflight()
        if not capability.supported:
            raise CliUnsupportedError(capability.reason or f"{self.name} is unsupported for this workflow")

        args = self.build_inference_only_args()
        ensure_tools_disabled(args, self.REQUIRED_DISABLE_MARKERS)

        result = self._runner(
            [capability.executable, *args], input=json.dumps(task_payload),
            capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, check=False,
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.name} returned non-JSON output") from exc

        validate_schema(response, schema.get("response", schema))
        return response


class CopilotCliAdapter(CliAdapter):
    """GitHub Copilot CLI adapter.

    Installed and authenticated in the implementation environment (version
    1.0.95-3). Its own ``--help`` documents ``--allow-all-tools`` as "required
    for non-interactive mode" (``-p``/``--prompt``), and the permissions help
    topic confirms ``--available-tools``/``--excluded-tools`` only filter which
    tools the model can *see* — approval-prompt behaviour in non-interactive
    mode is governed separately by ``--allow-tool``/``--deny-tool``/
    ``--allow-all-tools``, with no documented flag that makes ``-p`` run
    without that broad grant. ``--no-ask-user`` separately disables the one
    tool that would otherwise let the agent pause and ask instead of acting
    autonomously. Since this workflow refuses to use a broad/autonomous
    permission grant, Copilot CLI is marked unsupported here rather than
    invoked with ``--allow-all-tools``. See docs/backend-setup.md for the full
    ``--help`` excerpt."""

    name = "copilot"
    executable_names = ("copilot", "gh-copilot", "github-copilot-cli")
    REQUIRED_DISABLE_MARKERS = ()
    UNVERIFIABLE_REASON = (
        "copilot's own --help documents --allow-all-tools as \"required for "
        "non-interactive mode\" (-p/--prompt); no flag combination was found "
        "that runs -p without that broad, automatic-approval permission grant, "
        "so this CLI is marked unsupported for this workflow rather than using "
        "it (see docs/backend-setup.md)"
    )


class ClaudeCodeCliAdapter(CliAdapter):
    """Claude Code CLI adapter.

    Not installed in this environment; the profile below is built from the
    official CLI reference (docs.anthropic.com/en/docs/claude-code/cli-reference,
    version 2.1.296) rather than a local ``--help`` run. ``--disallowedTools "*"``
    is documented to remove every tool from Claude's context entirely (not just
    deny it with a prompt), so no shell, file-write or network-capable tool is
    ever offered to the model. ``--permission-prompts none`` (headless doc,
    "Turn off permission prompts in unattended runs") denies — rather than
    bypasses — anything that would otherwise need a prompt, so a headless run
    never hangs waiting for approval it cannot grant. ``-p`` is print
    (non-interactive) mode and ``--output-format json`` requests structured
    output. This is implemented and fixture-tested only, per the plan."""

    name = "claude-code"
    executable_names = ("claude",)
    REQUIRED_DISABLE_MARKERS = ("--disallowedTools", "*", "--permission-prompts", "none")


class CodexCliAdapter(CliAdapter):
    """Codex CLI adapter.

    Not installed in this environment; the profile below is built from the
    official CLI reference (developers.openai.com/codex/reference/commands and
    /codex/agent-approvals-security, version 0.162.1) rather than a local
    ``--help`` run. ``codex exec`` is the documented non-interactive/scripted
    entry point; ``--sandbox read-only`` is an OS-enforced sandbox policy that
    blocks model-generated writes and shell mutation regardless of approval
    settings, network access is off by default, and ``--ask-for-approval
    never`` ("works with all --sandbox modes") only suppresses prompts — it
    does not relax the sandbox, unlike ``--dangerously-bypass-approvals-and-
    sandbox``/``--yolo``, which this adapter never uses. ``--json`` requests
    structured, newline-delimited output. This is implemented and
    fixture-tested only, per the plan."""

    name = "codex"
    executable_names = ("codex",)
    REQUIRED_DISABLE_MARKERS = ("--sandbox", "read-only", "--ask-for-approval", "never")
