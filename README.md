# token-tracker

Measure and optimize Claude Code and Codex token usage, including Codex in JetBrains.
Token Tracker reads existing local logs and provides a dashboard with usage, estimated
costs and project-specific optimization opportunities.

## Why Token Tracker?

- **Usage directly in your agent’s chat:** report tokens and estimated cost after each prompt.
- **Evidence-based optimization:** the audit skill examines expensive sessions and archived prompts, then proposes measurable changes tailored to your project. You choose which to apply.
- **Discussion leaderboard:** rank usage within the selected period, search titles and first prompts, and inspect linked subagents.
- **Detailed breakdowns:** compare projects, models, fresh/cached input, output and recorded MCP calls/errors.
- **Local processing:** the tracker processes logs locally; audit excerpts supplied to an agent follow that agent’s privacy settings.

### Platform compatibility

- **macOS:** primary tested platform, including real-log ingestion, dashboard and server service operation.
- **Linux:** console operation tested in a Linux container. Automatic service installation is not supplied.
- **Windows:** Python tests pass; native dashboard startup and real-log ingestion remain unconfirmed. Use the PowerShell commands below, or the cross-platform Make shortcuts; the bundled `.sh` scripts need a POSIX shell.

The included background-service installers are **macOS-only**.

![token-tracker dashboard — synthetic demo data](docs/dashboard.png)

## Installation

Clone this repository into a writable directory. Install [uv](https://docs.astral.sh/uv/getting-started/installation/);
Python 3.10+ is required and uv can download a compatible version if needed.
Run all commands from the repository directory.

### macOS and Linux

```sh
uv sync
uv run python -m tracker.ingest
uv run python -m uvicorn tracker.api:app --host 127.0.0.1 --port 8732
```

### Windows (PowerShell)

Install uv, then close and reopen PowerShell:

```powershell
winget install --id=astral-sh.uv -e
```

```powershell
uv sync
uv run python -X utf8 -m tracker.ingest
uv run python -X utf8 -m uvicorn tracker.api:app --host 127.0.0.1 --port 8732
```

`-X utf8` keeps text reads consistent across Windows locales. Native Windows and
WSL have different home folders; use the environment containing your agent logs.

### Make shortcuts

With GNU Make installed, three targets wrap the commands above and work on macOS,
Linux and Windows:

- `make setup`: `uv sync` plus an editable install.
- `make ingest`: run the ingestion.
- `make server`: serve the dashboard, **with `--reload`** so editing the source
  restarts the process. Set `RELOAD=0` for the same behaviour as the plain commands
  above, or `HOST=... PORT=...` to move the listener.

The other targets (`agent`, `down`, `logs`, and the `server-service` family) call
shell scripts or launchd and are macOS-only.

Open **http://127.0.0.1:8732/** on the machine running the server and keep its terminal
open. In a VM, use the browser inside the VM.

## Usage

Select a project and period to explore consumption. The leaderboard and its details
count only usage inside that period; **All** uses the complete imported history.

**Starting the server does not import new logs.** Click **re-ingest** or **Refresh usage**,
or rerun the ingestion command. The leaderboard shows the last successful ingestion
and guidance when empty results may be caused by stale data.

Default sources: `~/.codex/sessions/` and `~/.claude/projects/`. No supported logs means
an empty dashboard. Generated SQLite databases remain in the repository directory.

### Report usage in the agent’s chat

Copy [these instructions](docs/AGENTS_TOKEN_USAGE.md) into your agent’s `AGENTS.md`.
The agent takes a snapshot before work and requests the logged delta afterward:

```text
Tokens: 27,987 / input: 477 fresh, 27,264 cached / output 246 / est. cost: $0.04 / MCP: 0 calls.
```

You can ask the agent to stop or resume measuring. Reports exclude separate agent
sessions and the final answer generated after measurement. See the [agent usage guide](docs/AGENT_USAGE.md).

### Audit and optimize a project

Install the complete [token-tracker-audit](skills/token-tracker-audit/SKILL.md) folder
in your agent’s skill directory. For Codex on macOS/Linux:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/token-tracker-audit "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Invoke `$token-tracker-audit` in the target project. It asks for the analysis period,
uses archived examples, flags insufficient evidence, and proposes numbered changes
with expected gains and validation steps. Nothing is applied until you select the changes.
Other agents use their own skill installation mechanism.

## Server operations

### Console

Stop with **Ctrl+C**, then rerun the server command from [Installation](#installation).
On macOS/Linux: `uv run python -m uvicorn tracker.api:app --host 127.0.0.1 --port 8732`.
On Windows: `uv run python -X utf8 -m uvicorn tracker.api:app --host 127.0.0.1 --port 8732`.
Check `http://127.0.0.1:8732/api/health` after restarting.

### Automatic startup (macOS only)

Run `uv sync` first and stop any console server before installing the service.

- `make server-service`: install and start the server at user login.
- `make server-status`: check status.
- `make server-restart`: restart after updates.
- `make server-stop` / `make server-start`: stop or start the service.
- `make server-uninstall`: remove automatic server startup.
- `make agent`: separately install ingestion every five minutes.
- `make down`: remove scheduled ingestion.

Server logs: `~/Library/Logs/token-tracker-server.log` and `.error.log`.
Ingestion logs: `~/Library/Logs/token-tracker.log`. Before moving the repository,
uninstall these jobs and reinstall from the new location.

Linux and Windows need their own service/scheduler configuration; no installer is
supplied. Run under the user owning the logs, with the repository as working directory.
The HTTP server and scheduled ingestion are separate jobs.

If the port is occupied, check for an existing server before starting another.
Agent sandbox restrictions can block localhost access even when the server is healthy;
use the integration’s permitted access route. Restarting does not fix permissions.
This section is also available through `GET /api/docs/operations`.

## Estimates and limitations

Costs are **API-equivalent estimates**, not invoices or subscription quota usage.
Unknown prices remain unpriced; partial estimates exclude those amounts but retain tokens.
Reasoning is included in output. MCP text-size estimates are not billed token counts.

Rates live in `prices.json`; **recompute prices** explicitly updates stored estimates.
For an older incompatible database, follow the [backup and rebuild guide](docs/CODEX_REPAIR.md).
See [discussion details and API](docs/DISCUSSIONS.md) for grouping and attribution rules.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
