"""
server.py — The MCP server for devops-mcp (local dev co-pilot).

This file wires together docker_client, system_info, and shell_runner
and exposes them to Claude via the MCP protocol as six tools.

Tools:
  1. list_containers     — list all Docker containers (running + stopped)
  2. get_container_logs  — fetch recent stdout/stderr from a container
  3. get_container_stats — live CPU/memory/network/disk stats for a container
  4. list_processes      — top N processes by CPU or memory via psutil
  5. get_system_info     — hostname, OS, CPU, RAM, disk, load average
  6. run_command         — execute an allowlisted shell command safely

Design note — sync vs async:
  The Docker SDK and psutil are both synchronous libraries.  Our MCP server
  runs on an async event loop (asyncio).  Running synchronous blocking code
  directly in an async function stalls the event loop and prevents other
  requests from being processed concurrently.

  The solution: asyncio.to_thread(fn, *args) runs the synchronous function
  in a separate thread-pool thread, freeing the event loop while it blocks.
  This is the standard pattern for integrating sync libraries with async code.
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import docker.errors
from docker import DockerClient
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from devops_mcp.docker_client import (
    get_client,
    list_containers,
    get_container_logs,
    get_container_stats,
    health_check as docker_health_check,
)
from devops_mcp.system_info import get_system_info, list_processes
from devops_mcp.shell_runner import run_command, parse_allowed_commands

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

_env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_env_path)

DOCKER_HOST       = os.getenv("DOCKER_HOST", "")   # empty → docker.from_env()
ALLOWED_COMMANDS  = parse_allowed_commands(
    os.getenv(
        "ALLOWED_COMMANDS",
        "ls,cat,git,df,du,pwd,echo,env,whoami,uname,curl,ps,wc,head,tail,find,grep",
    )
)
COMMAND_TIMEOUT   = int(os.getenv("COMMAND_TIMEOUT", "10"))
SANDBOX_DIR       = os.getenv("SANDBOX_DIR", "") or None  # None → inherit cwd


# --------------------------------------------------------------------------- #
# Shared state                                                                #
# --------------------------------------------------------------------------- #

class AppState:
    """Holds the Docker client (opened once at startup, shared across tools)."""
    def __init__(self, docker_client):
        self.docker = docker_client


# --------------------------------------------------------------------------- #
# Lifespan                                                                    #
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppState]:
    """
    Open the Docker client at server startup; close it on shutdown.

    We test the Docker connection with a health check ping so users get
    an immediate, clear error if Docker Desktop isn't running — rather
    than a cryptic exception buried in the first tool call.
    """
    # Connect to Docker (synchronous, but quick — just opens the socket)
    try:
        docker_client = get_client(DOCKER_HOST if DOCKER_HOST else None)
        reachable = await asyncio.to_thread(docker_health_check, docker_client)
    except Exception as e:
        docker_client = None
        reachable = False
        print(f"[devops-mcp] WARNING: Could not connect to Docker: {e}", file=sys.stderr, flush=True)

    if reachable:
        print("[devops-mcp] Docker daemon reachable", file=sys.stderr, flush=True)
    else:
        print(
            "[devops-mcp] WARNING: Docker daemon not reachable. "
            "Docker tools will return errors until Docker Desktop is running.",
            file=sys.stderr,
            flush=True,
        )

    print(
        f"[devops-mcp] Allowed shell commands: {', '.join(sorted(ALLOWED_COMMANDS))}",
        file=sys.stderr,
        flush=True,
    )

    state = AppState(docker_client=docker_client)

    try:
        yield state
    finally:
        if docker_client:
            docker_client.close()
        print("[devops-mcp] Shutdown complete.", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# MCP server                                                                  #
# --------------------------------------------------------------------------- #

mcp = FastMCP(name="devops-assistant", lifespan=lifespan)


def _state() -> AppState:
    """Retrieve the AppState yielded by the lifespan context manager."""
    return mcp.get_context().request_context.lifespan_context


def _require_docker() -> DockerClient:
    """
    Return the Docker client, or raise a clear error if it's unavailable.
    Used at the top of every Docker tool.
    """
    state = _state()
    if state.docker is None:
        raise RuntimeError(
            "Docker client is not available. "
            "Is Docker Desktop running? Check the server logs."
        )
    return state.docker  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Tool 1 — list_containers                                                    #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def list_containers_tool(running_only: bool = False) -> str:
    """
    List all Docker containers on the local machine.

    Shows container name, image, current status, and port bindings.
    Useful for a quick overview of what's running (and what isn't).

    Args:
        running_only: If True, show only currently running containers.
                      Default False shows all containers including stopped ones.

    Returns:
        Formatted table of containers, or a message if none exist.
    """
    client = _require_docker()

    try:
        containers = await asyncio.to_thread(
            list_containers, client, not running_only
        )
    except Exception as e:
        return f"Error listing containers: {e}"

    if not containers:
        status_msg = "running" if running_only else "any"
        return f"No {status_msg} containers found."

    lines = [f"Found {len(containers)} container(s):\n"]
    for c in containers:
        # Format port bindings: {8080/tcp: [{HostPort: 8080}]} → "8080→8080"
        port_strs = []
        for container_port, bindings in (c["ports"] or {}).items():
            if bindings:
                for b in bindings:
                    host_port = b.get("HostPort", "?")
                    port_strs.append(f"{host_port}→{container_port}")

        ports_str = ", ".join(port_strs) if port_strs else "none"
        status_icon = "●" if c["status"] == "running" else "○"

        lines.append(
            f"  {status_icon} {c['name']}  [{c['status']}]\n"
            f"    ID:     {c['id']}\n"
            f"    Image:  {c['image']}\n"
            f"    Ports:  {ports_str}\n"
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool 2 — get_container_logs                                                 #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_container_logs_tool(
    container: str,
    tail: int = 100,
    timestamps: bool = True,
) -> str:
    """
    Fetch recent log output from a Docker container.

    Retrieves the last N lines of combined stdout+stderr for the named
    container. Works on both running and stopped containers.

    Args:
        container:  Container name or ID (e.g. "iip-db-1" or "a3f9d2").
        tail:       Number of lines to fetch from the end (default 100, max 1000).
        timestamps: Prefix each line with its Docker timestamp (default True).

    Returns:
        Log output as a string, or an error message if the container is not found.
    """
    client = _require_docker()
    tail = max(1, min(tail, 1000))

    try:
        logs = await asyncio.to_thread(
            get_container_logs, client, container, tail, timestamps
        )
    except docker.errors.NotFound:
        return f"Container '{container}' not found. Use list_containers to see available containers."
    except Exception as e:
        return f"Error fetching logs for '{container}': {e}"

    if not logs.strip():
        return f"Container '{container}' has no log output."

    return f"Last {tail} lines of logs for '{container}':\n\n{logs}"


# --------------------------------------------------------------------------- #
# Tool 3 — get_container_stats                                                #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_container_stats_tool(container: str) -> str:
    """
    Get live resource usage stats for a running Docker container.

    Reports CPU percentage, memory usage and limit, network I/O, and
    block (disk) I/O. Only works for containers that are currently running.

    Args:
        container: Container name or ID (must be running).

    Returns:
        Formatted stats snapshot, or an error if not running / not found.
    """
    client = _require_docker()

    try:
        stats = await asyncio.to_thread(get_container_stats, client, container)
    except docker.errors.NotFound:
        return f"Container '{container}' not found."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Error fetching stats for '{container}': {e}"

    return (
        f"Stats for '{stats['name']}' [{stats['status']}]:\n\n"
        f"  CPU:          {stats['cpu_percent']}%\n"
        f"  Memory:       {stats['mem_usage_mb']} MiB / {stats['mem_limit_mb']} MiB  "
        f"({stats['mem_percent']}%)\n"
        f"  Network I/O:  ↓ {stats['net_rx_mb']} MiB received  "
        f"↑ {stats['net_tx_mb']} MiB sent\n"
        f"  Block I/O:    ↓ {stats['block_read_mb']} MiB read  "
        f"↑ {stats['block_write_mb']} MiB written\n"
    )


# --------------------------------------------------------------------------- #
# Tool 4 — list_processes                                                     #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def list_processes_tool(
    sort_by: str = "cpu",
    limit: int = 20,
) -> str:
    """
    List the top processes running on the host machine.

    Uses psutil to read the system process table and returns the processes
    with the highest resource consumption. Useful for diagnosing what is
    eating CPU or memory on your machine.

    Args:
        sort_by: "cpu" (default) to sort by CPU usage, or "memory" for RAM.
        limit:   Number of processes to return (default 20, max 50).

    Returns:
        Formatted process table sorted by the chosen metric.
    """
    if sort_by not in ("cpu", "memory"):
        sort_by = "cpu"
    limit = max(1, min(limit, 50))

    try:
        procs = await asyncio.to_thread(list_processes, sort_by, limit)
    except Exception as e:
        return f"Error reading process list: {e}"

    if not procs:
        return "No processes found."

    metric = "cpu_percent" if sort_by == "cpu" else "mem_percent"
    lines = [f"Top {len(procs)} processes by {sort_by.upper()}:\n"]

    for p in procs:
        lines.append(
            f"  [{p['pid']:>6}]  {p['name']:<25}  "
            f"cpu={p['cpu_percent']:>5.1f}%  "
            f"mem={p['mem_percent']:>5.1f}%  ({p['mem_rss_mb']} MiB)\n"
            f"           {p['status']:<10}  user={p['username']}\n"
            f"           {p['cmdline']}\n"
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool 5 — get_system_info                                                    #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_system_info_tool() -> str:
    """
    Get a comprehensive snapshot of the host machine's system resources.

    Reports hostname, OS version, CPU count and usage, RAM usage,
    disk space on the root filesystem, load average, and uptime.

    Returns:
        Formatted system info string.
    """
    try:
        info = await asyncio.to_thread(get_system_info)
    except Exception as e:
        return f"Error reading system info: {e}"

    load = info["load_avg"]
    load_str = (
        f"{load[0]} / {load[1]} / {load[2]}  (1m / 5m / 15m)"
        if load else "N/A (Windows)"
    )

    disk = info["disk_root"]
    freq = f"{info['cpu_freq_mhz']} MHz" if info["cpu_freq_mhz"] else "N/A"

    return (
        f"System Info — {info['hostname']}\n\n"
        f"  OS:           {info['os']}\n"
        f"  Python:       {info['python_version']}\n"
        f"  Boot time:    {info['boot_time']}\n\n"
        f"  CPU cores:    {info['cpu_physical']} physical / {info['cpu_count']} logical\n"
        f"  CPU freq:     {freq}\n"
        f"  CPU usage:    {info['cpu_percent']}%\n"
        f"  Load avg:     {load_str}\n\n"
        f"  RAM total:    {info['mem_total_gb']} GiB\n"
        f"  RAM used:     {info['mem_used_gb']} GiB  ({info['mem_percent']}%)\n"
        f"  RAM free:     {info['mem_available_gb']} GiB\n"
        f"  Swap used:    {info['swap_used_gb']} / {info['swap_total_gb']} GiB\n\n"
        f"  Disk ({disk['path']}):\n"
        f"    Total:  {disk['total_gb']} GiB\n"
        f"    Used:   {disk['used_gb']} GiB  ({disk['percent']}%)\n"
        f"    Free:   {disk['free_gb']} GiB\n"
    )


# --------------------------------------------------------------------------- #
# Tool 6 — run_command                                                        #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def run_command_tool(command: str) -> str:
    """
    Run a sandboxed shell command on the host machine.

    Only commands from the configured allowlist can be executed.
    All commands run with a hard timeout to prevent runaway processes.
    The shell is NOT invoked — commands are exec'd directly, so shell
    metacharacters (; | & ` $()) have no special meaning.

    Allowed commands (configured via ALLOWED_COMMANDS in .env):
    ls, cat, git, df, du, pwd, echo, env, whoami, uname,
    curl, ps, wc, head, tail, find, grep

    Args:
        command: The full command string, e.g. "ls -la /tmp" or "git log --oneline -n 5"

    Returns:
        The command's stdout, stderr, and exit code.
        An error message if the command is not allowed or times out.
    """
    # run_command is synchronous (subprocess) — run in thread pool
    result = await asyncio.to_thread(
        run_command,
        command,
        ALLOWED_COMMANDS,
        COMMAND_TIMEOUT,
        SANDBOX_DIR,
    )

    if result.timed_out:
        return f"Command timed out after {COMMAND_TIMEOUT}s: `{command}`"

    if result.error and not result.stdout and not result.stderr:
        return f"Error: {result.error}"

    parts = []
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    if result.exit_code != 0:
        parts.append(f"exit code: {result.exit_code}")
        if result.error:
            parts.append(f"({result.error})")

    return "\n".join(parts) if parts else "(no output)"


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def main() -> None:
    """Start the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
