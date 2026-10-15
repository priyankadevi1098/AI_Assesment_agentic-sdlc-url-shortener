# agentic-sdlc-url-shortener

A governed SDLC agent that turns a URL-shortener requirement into reviewed,
runnable application source, tests, an API definition and documentation. It
supports a Copilot CLI, Claude Code CLI, Codex CLI or OpenAI-compatible API
backend, with an explicit human approval gate before any generated code
executes. Standard library only — no agent framework, no third-party runtime
dependency, no Docker.

This README documents what is implemented: request/state contracts, backend
adapters, the executable orchestration graph, brownfield/workspace handling,
the full approval/execution/repair, resume/replan/rollback, and
release-evidence/reporting phases of the plan, and a single generic
`workflow.run_graph(...)` driver — wired behind the CLI's `run`/`resume`
subcommands — that walks an arbitrary validated `graph.TaskGraph` end to end:
analysis, decomposition, the parallel design/risk join, implementation
staging, pausing for execution approval, real local validation, and pausing
for release-readiness approval. `resume` is implemented for real, not a stub;
see "Running the CLI" below for the full command surface and "Status" for
what remains explicitly out of scope.

## Prerequisites

- Python 3.11 or newer (tested with the `py -3` launcher on Windows).
- For the API backend: `LLM_MODEL` and `LLM_API_KEY` set as environment
  variables (`LLM_API_KEY` may be omitted only for a loopback `http://` endpoint,
  e.g. local Ollama); `LLM_BASE_URL` optional, defaults to the OpenAI-compatible
  HTTPS endpoint. Never paste a credential into chat — set it in your shell.
- For a CLI backend: install and authenticate that CLI using its own official
  setup. See `docs/backend-setup.md` for each adapter's real, evidence-backed
  status: **Copilot CLI is unsupported** (installed and investigated on the
  build machine — its own `--help` ties non-interactive mode to the
  `--allow-all-tools` broad-permission grant, with no documented way around
  it); **Claude Code CLI and Codex CLI are supported, verified against each
  CLI's official public documentation** (neither is installed in the build
  environment, so this is implemented and fixture-tested only — not yet
  exercised against a real authenticated installation).

## Input: the request file

A run starts from a JSON request file with these fields:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | short scenario name, non-empty |
| `mode` | `"greenfield"` \| `"brownfield"` | yes | brownfield also needs `--project` on the CLI |
| `requirement` | string | yes | natural-language specification, non-empty |
| `acceptance_criteria` | array of strings | yes | at least one observable behavior |
| `answers` | object | no | clarification answers, supplied on resume |

An invalid request (missing/empty required field, wrong `mode`) is rejected at
the boundary with a `RequestValidationError` naming the offending field —
nothing downstream re-validates it.

Example requests ship in `examples/requests/`:

- `greenfield.json` — create links, redirect, persist in SQLite, click counts,
  health/analytics, HTTP(S) validation, unknown-code handling.
- `brownfield.json` — add optional link expiration to a previously generated
  service, preserving existing links/APIs and migrating the schema safely.
- `ambiguous.json` — an underspecified request, paired with
  `ambiguous-answers.json` showing how clarification answers are supplied on resume.

## Running the CLI

Start a new run:

```powershell
py -3 -m sdlc_agent run --request examples\requests\greenfield.json --backend auto
```

`--backend` accepts `auto`, `copilot`, `claude-code`, `codex` or `api`. Under
`auto`, if more than one backend is usable, pass `--backend-preference
{copilot|claude-code|codex|api}` to choose explicitly — the tool will not
silently decide which assistant or provider receives your requirement and
source. Before any content is transmitted, the CLI shows the selected backend
and asks for destination approval (`--yes` to skip the interactive prompt in
a scripted run). Brownfield runs also take `--project <path>`, pointing at a
project this tool previously generated.

Each run gets its own directory under `runs\<run-id>\`, with state persisted
atomically to `state.json`. The run walks the canonical governed pipeline
(`workflow.build_default_graph` driven by `workflow.run_graph`) until it
completes, fails validation, or pauses for a human decision — never printing
"completed" for a run that is actually paused or failed.

Inspect a run (request/mode, backend, current status):

```powershell
py -3 -m sdlc_agent show --run <run-id>
```

Resume a paused run with exactly one of three actions per call:

```powershell
# After manually reviewing the staged candidate and diff:
py -3 -m sdlc_agent resume --run <run-id> --approve execute_candidate

# After real local compile/test validation passes, a separate release-readiness
# decision (records a human decision only; never deploys or publishes):
py -3 -m sdlc_agent resume --run <run-id> --approve release_ready

# Supply clarification answers for an underspecified requirement (accepts
# either a bare {"question": "answer", ...} mapping, or a request-shaped file
# with a nested "answers" object, e.g. examples/requests/ambiguous-answers.json —
# the CLI unwraps the nested object and never folds the rest of that
# document's fields into the answers):
py -3 -m sdlc_agent resume --run <run-id> --answers examples\requests\ambiguous-answers.json

# Change the requirement itself (bumps the plan revision and invalidates every
# downstream stage/approval it affects; shows exactly which ones):
py -3 -m sdlc_agent resume --run <run-id> --request examples\requests\updated-request.json
```

A repair that changes the candidate always invalidates execution approval;
inspect the new diff and re-approve with `--approve execute_candidate` before
it runs again.

Start the reviewed, generated service once a run has completed:

```powershell
py -3 runs\<run-id>\candidate\app.py --port 8000 --database runs\<run-id>\service.sqlite3
```

(Exact flags depend on what the candidate's own `app.py`/`README.md`
document for that run — inspect `runs\<run-id>\candidate\README.md` first.)

### Running the CLI integration tests

`tests/test_cli_integration.py` drives the real `py -3 -m sdlc_agent`
`run`/`resume`/`show` command surface as subprocesses — not the library
functions underneath it — against a local loopback HTTP fixture server
standing in for the `api` backend. It needs no live credentials: the fixture
binds `127.0.0.1` on an OS-assigned free port and `LLM_BASE_URL` points at it
as plain loopback HTTP, the one exception `llm.LlmConfig.from_env` already
documents to its HTTPS-only rule — never a real network or LLM call. Run it
with:

```powershell
py -3 -m unittest tests.test_cli_integration -v
```

## Status

Implemented: CLI/request/state contracts, backend adapters (CLI preflight
detection + API fallback with bounded retries), the executable-orchestration
task graph and workflow join logic, a single generic `workflow.run_graph(...)`
driver wired behind both the `run` and `resume` CLI subcommands, brownfield
workspace reading and file-bundle validation, execution/release approval
governance bound to exact candidate hashes, real local compile/test command
execution and independent tool-owned acceptance checks, repair with bounded
attempts and pre-repair snapshot/restore, resume-time stage invalidation for
changed requirements and clarification answers, release-evidence reporting
(JSONL event log, JSON/Markdown summaries, honest metrics including
MTTR-unavailable), and public CLI integration tests
(`tests/test_cli_integration.py`) that drive the whole `run`/`resume`/`show`
surface as real subprocesses against a loopback fixture.

Backend CLI verification is evidence-backed but partial by design: Copilot CLI
is installed on the build machine and its own `--help` output is the evidence
that it stays unsupported (see `docs/backend-setup.md`); Claude Code CLI and
Codex CLI are not installed here, so their verified profiles rest on each
CLI's official public documentation rather than a live run — implemented and
fixture-tested, not yet exercised against a real authenticated installation.
No live LLM API call is made anywhere in this test suite either; the `api`
backend is exercised against the loopback fixture described above, never a
real provider. See `docs/architecture.md`, `docs/implementation-notes.md`,
`docs/security-and-limitations.md`, `docs/troubleshooting.md` and
`docs/interview-qa.md` for the rest of the detail.
