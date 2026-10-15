# Backend setup guide

This tool can send a run's requirement, acceptance criteria and (for brownfield
runs) existing application source to one of four backends: the GitHub Copilot
CLI, the Claude Code CLI, the Codex CLI, or an OpenAI-compatible HTTPS API.
Read this before choosing `--backend` on a real run — submitting requirement
or source text to any of these is a data transfer to that provider.

## Current verification status

Each CLI adapter's compatibility below is the result of an actual investigation
during implementation — a local `copilot --help` run for the one CLI installed
on the build machine, and the official public CLI reference documentation for
the two that are not installed — never a guessed or invented flag, and never
the `--dangerously-*`/`--yolo` style permission-bypass flag some of these CLIs
expose to grant themselves broad autonomy.

### GitHub Copilot CLI — unsupported for this workflow

Installed and authenticated on the build machine (`GitHub Copilot CLI
1.0.95-3`). Its own `copilot --help` output states, for `--allow-all-tools`:

> Allow all tools to run automatically without confirmation; **required for
> non-interactive mode**. Run `copilot help environment` for what
> `COPILOT_ALLOW_ALL` accepts and what exact "true" grants

and `copilot help permissions` confirms that `--available-tools`/
`--excluded-tools` only control which tools the model can *see* — approval of
the tools it is allowed to see is a separate mechanism
(`--allow-tool`/`--deny-tool`/`--allow-all-tools`), and the CLI's own
documentation ties `-p`/`--prompt` (its only non-interactive mode) to the
broad `--allow-all-tools` grant. No flag combination was found in `--help` or
the permissions topic that runs `-p` non-interactively without that grant.
`--no-ask-user` ("Disable the ask_user tool (agent works autonomously without
asking questions)") would additionally remove the one tool that lets the agent
pause rather than act. Since this workflow refuses to grant broad/autonomous
tool permissions, **Copilot CLI stays unsupported** — `CopilotCliAdapter` has
no entry in `VERIFIED_CLI_CAPABILITIES` and reports this exact reason from
`preflight()`.

### Claude Code CLI — supported, verified against documentation (not installed)

Not installed on the build machine; verified against the official CLI
reference at `docs.anthropic.com/en/docs/claude-code/cli-reference`, current
published version `2.1.296` (`@anthropic-ai/claude-code` on npm). Key
documented facts behind the verified profile in `cli_backends.py`:

- `-p`/`--print` is the documented non-interactive mode; `--output-format
  json` requests a structured JSON response.
- `--disallowedTools "*"` is documented as removing **every tool from Claude's
  context entirely** ("A bare tool name removes the matching tools from
  Claude's context... `"*"` removes every tool") — stronger than denying with
  a prompt, since no shell, file-write or network-capable tool is ever offered
  to the model to begin with.
- `--permission-prompts none` (headless guide, "Turn off permission prompts in
  unattended runs") is documented to **deny** — not bypass — anything that
  would otherwise need a prompt ("Anything that would prompt is denied... the
  run continues"), so a headless run cannot hang waiting for an approval it
  has no way to grant. This is the opposite of `--dangerously-skip-
  permissions`/`--permission-mode bypassPermissions`, which this profile never
  uses.
- Minimum verified version is `2.1.296`, the version whose documentation was
  actually read (and above the `2.1.259` minimum the docs cite for
  `--permission-prompts`).

Implemented and fixture-tested only (`tests/test_backends.py`); not yet
exercised against a real authenticated installation.

### Codex CLI — supported, verified against documentation (not installed)

Not installed on the build machine; verified against the official CLI
reference at `developers.openai.com/codex/reference/commands` and
`/codex/agent-approvals-security`, current published version `0.162.1`
(`@openai/codex` on npm). Key documented facts behind the verified profile:

- `codex exec` (alias `codex e`) is documented as the entry point for
  "scripted or CI-style runs that should finish without human interaction."
- `--sandbox read-only` is an OS-enforced sandbox policy for model-generated
  commands; by default "the agent runs with network access turned off," and
  `read-only` additionally blocks filesystem writes and shell mutation.
- `--ask-for-approval never` is documented to work with **all** `--sandbox`
  modes and only suppresses approval prompts — it does not relax the sandbox.
  This is distinct from, and never combined with,
  `--dangerously-bypass-approvals-and-sandbox`/`--yolo`, which the docs
  describe as bypassing both approvals and sandboxing and which this adapter
  never uses.
- `--json` requests newline-delimited structured output; the `-` positional
  prompt argument reads the task from stdin.
- Minimum verified version is `0.162.1`, the version whose documentation was
  actually read; no earlier minimum is documented for these flags.

Implemented and fixture-tested only (`tests/test_backends.py`); not yet
exercised against a real authenticated installation.

### Re-verifying or adding a CLI

To move an unsupported CLI to supported, or to confirm an existing entry still
holds after an upgrade:

1. Install and authenticate it using its own official setup.
2. Run its `--help` (or consult its official docs) and confirm the exact flags
   for: non-interactive/headless execution, structured (JSON) output, and
   disabling shell commands, file writes and network access from the model —
   without a permission-bypass flag.
3. Add or update the `VerifiedCliProfile(min_version=..., inference_only_args=
   (...))` entry in `VERIFIED_CLI_CAPABILITIES` in `cli_backends.py`, citing
   the CLI version and documentation checked, and record the evidence here.
4. If the CLI has **no** way to disable shell/file/network tool use for a
   single invocation without a permission-bypass flag, do not add an entry —
   mark it unsupported permanently for this workflow rather than granting
   broad autonomous permissions.

## API backend (`--backend api`)

Configure via environment variables (never paste these into chat):

| Variable | Required | Notes |
|---|---|---|
| `LLM_MODEL` | yes | model identifier, provider-specific |
| `LLM_API_KEY` | yes, unless `LLM_BASE_URL` is loopback HTTP | bearer token |
| `LLM_BASE_URL` | no | defaults to `https://api.openai.com/v1`; must be HTTPS, or plain HTTP only against `localhost`/`127.0.0.1`/`::1` (e.g. a local Ollama endpoint) |

Requests use a finite timeout, a bounded output-size limit, and a small number
of transient retries (network errors and HTTP 408/429/5xx) with exponential
backoff. Exhausting retries raises an explicit error — the tool never silently
substitutes a canned "success" result for a failed request. Error messages are
sanitized: the API key and raw request/response bodies are never included.

Not every OpenAI-compatible endpoint supports every option this tool might use
in later phases (e.g. structured-output modes, certain parameters); document
provider-specific limitations here as they are discovered rather than assuming
uniform behavior.

## Backend selection and destination approval

- `--backend auto` discovers usable CLIs via a bounded, non-mutating preflight
  check, then falls back to the API only if its environment variables are
  configured. If more than one CLI is usable, `auto` requires
  `--backend-preference` — it will not silently pick which subscription or
  provider receives your content.
- Before transmitting anything, the CLI prints the selected backend and
  requires destination approval (`--yes` to approve non-interactively in a
  scripted run).
- Once a run has started, switching backend requires explicit re-approval; the
  tool never silently retries through a different backend after a partial
  failure (`BackendLock` / `MidRunFallbackProhibitedError` in `backends.py`).

## Structured task contract

Every backend (CLI or API) receives the same structured JSON task payload and
returns a JSON response validated against a task-specific schema
(`prompts.py`). This keeps CLI and API adapters interchangeable from the
orchestrator's point of view and means a schema change is made once, in one
place.

Each schema validates two distinct shapes: the outgoing task payload against
its own top-level `required`/`properties` (e.g. the analysis task payload is
`{"task", "requirement", "acceptance_criteria"}`), and the model's JSON
response against the nested `response` sub-schema (e.g.
`{"summary", "key_requirements"}`) — `llm.ApiBackend.generate` and
`cli_backends.CliAdapter.invoke` both enforce it in that order before ever
starting the request/subprocess. An earlier version of `prompts.py` described
only the response shape and handed that same schema to `validate_schema` for
the outgoing payload too, which made every real backend call reject its own
request before sending it; this was caught by
`tests/test_cli_integration.py` driving a real `ApiBackend` against a
loopback fixture rather than a function stub, and is fixed in `prompts.py`.
