# devops-mcp

A Model Context Protocol (MCP) server that gives Claude a window into your local
development environment — Docker containers, system resources, running processes,
and a sandboxed shell — so you can ask questions and diagnose issues in plain
English.

## What it does

| Tool | Description |
|---|---|
| `list_containers` | List all Docker containers (name, image, status, ports) |
| `get_container_logs` | Fetch the last N log lines from any container |
| `get_container_stats` | Live CPU / memory / network / disk stats for a container |
| `list_processes` | Top processes by CPU or memory (via psutil) |
| `get_system_info` | Hostname, OS, CPU count & usage, RAM, disk, load average |
| `run_command` | Run a whitelisted shell command safely (no shell injection) |

## Architecture

```
Claude Desktop
     │  MCP (stdio)
     ▼
devops_mcp/server.py        ← MCP server, 6 tools, lifespan startup
     ├── docker_client.py   ← Docker SDK wrapper (handles dangling images)
     ├── system_info.py     ← psutil: system info + process list
     └── shell_runner.py    ← subprocess: allowlist, shell=False, timeout
```

**Key design decisions:**

- **`asyncio.to_thread()`** — Docker SDK and psutil are synchronous. Every tool
  wraps its blocking call in `asyncio.to_thread()` so the MCP event loop is never
  blocked.
- **`AppState` lifespan** — the Docker client is opened once at startup (not per
  request) and injected into every tool via `ctx.request_context.lifespan_context`.
- **Sandboxed shell** — `run_command` uses `shell=False` (no shell injection),
  an allowlist check, and a hard timeout (default 10 s). There is no way to run
  a command not in the allowlist.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- Docker Desktop running locally
- No external API keys needed

## Setup

```bash
git clone https://github.com/Kamalesh-Kavin/devops-mcp
cd devops-mcp

# Install dependencies
uv sync

# Copy and edit environment config
cp .env.example .env
# Edit .env if you want to change ALLOWED_COMMANDS or COMMAND_TIMEOUT_SECONDS
```

## Run the smoke test

```bash
uv run python smoke_test.py
```

All 8 test sections should print `PASS`.

## Wire into Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
"devops-assistant": {
  "command": "/Users/yourname/.local/bin/uv",
  "args": [
    "--directory", "/path/to/devops-mcp",
    "run", "python", "-m", "devops_mcp.server"
  ]
}
```

Restart Claude Desktop, then try:

- *"List all running Docker containers"*
- *"Show me the last 20 log lines from iip-api-1"*
- *"What is the CPU and memory usage on this machine?"*
- *"Which process is using the most memory right now?"*
- *"Run `git status` in /path/to/my/project"*

## Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_COMMANDS` | `ls,cat,git,df,du,pwd,echo,env,whoami,uname,curl,ps,wc,head,tail,find,grep` | Comma-separated shell commands that `run_command` will execute |
| `COMMAND_TIMEOUT_SECONDS` | `10` | Hard timeout; the process is killed if it exceeds this |

## Security note

`run_command` uses `subprocess.run(..., shell=False)`. The first token of your
command is checked against the allowlist before execution. Arbitrary shell
pipelines, redirection, and command chaining (`&&`, `|`, `;`) are not supported
by design.
