"""
smoke_test.py — End-to-end test of devops-mcp components.

Tests every module directly (no MCP server needed).
Run with:
    uv run python smoke_test.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DOCKER_HOST = os.getenv("DOCKER_HOST", "")


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def ok(msg: str) -> None:
    print(f"    PASS: {msg}")


def fail(msg: str) -> None:
    print(f"    FAIL: {msg}")
    sys.exit(1)


# ------------------------------------------------------------------ #
# 1. Docker — list containers                                         #
# ------------------------------------------------------------------ #
section("1. Docker — list containers")

from devops_mcp.docker_client import get_client, list_containers, health_check

try:
    client = get_client(DOCKER_HOST if DOCKER_HOST else None)
    if not health_check(client):
        fail("Docker daemon not reachable. Is Docker Desktop running?")
    ok("Docker daemon is reachable")
except Exception as e:
    fail(f"Could not connect to Docker: {e}")

containers = list_containers(client, all_containers=True)
ok(f"Found {len(containers)} container(s) total")
running = [c for c in containers if c["status"] == "running"]
ok(f"  {len(running)} currently running")
for c in containers[:5]:  # print first 5
    print(f"    {'●' if c['status'] == 'running' else '○'} "
          f"{c['name']:<30} {c['image']:<25} [{c['status']}]")


# ------------------------------------------------------------------ #
# 2. Docker — container logs (pick a running container if available)  #
# ------------------------------------------------------------------ #
section("2. Docker — container logs")

from devops_mcp.docker_client import get_container_logs

if running:
    target = running[0]["name"]
    logs = get_container_logs(client, target, tail=5, timestamps=False)
    ok(f"Fetched last 5 log lines from '{target}'")
    for line in logs.strip().splitlines()[:3]:
        print(f"    {line[:100]}")
else:
    ok("No running containers — skipping log test")


# ------------------------------------------------------------------ #
# 3. Docker — container stats (pick a running container)              #
# ------------------------------------------------------------------ #
section("3. Docker — container stats")

from devops_mcp.docker_client import get_container_stats

if running:
    target = running[0]["name"]
    stats = get_container_stats(client, target)
    ok(f"Stats for '{stats['name']}':")
    print(f"    CPU: {stats['cpu_percent']}%   "
          f"MEM: {stats['mem_usage_mb']} MiB / {stats['mem_limit_mb']} MiB  "
          f"({stats['mem_percent']}%)")
    print(f"    Net ↓{stats['net_rx_mb']} MiB  ↑{stats['net_tx_mb']} MiB  "
          f"  Disk ↓{stats['block_read_mb']} MiB  ↑{stats['block_write_mb']} MiB")
else:
    ok("No running containers — skipping stats test")

client.close()


# ------------------------------------------------------------------ #
# 4. System info                                                      #
# ------------------------------------------------------------------ #
section("4. System info (psutil)")

from devops_mcp.system_info import get_system_info

info = get_system_info()
ok(f"Hostname: {info['hostname']}")
ok(f"OS: {info['os']}")
ok(f"CPU: {info['cpu_count']} logical cores @ {info['cpu_freq_mhz']} MHz, {info['cpu_percent']}% used")
ok(f"RAM: {info['mem_used_gb']} / {info['mem_total_gb']} GiB ({info['mem_percent']}%)")
ok(f"Disk /: {info['disk_root']['used_gb']} / {info['disk_root']['total_gb']} GiB ({info['disk_root']['percent']}%)")
if info["load_avg"]:
    ok(f"Load avg: {info['load_avg']}")


# ------------------------------------------------------------------ #
# 5. Process list                                                     #
# ------------------------------------------------------------------ #
section("5. Process list (psutil)")

from devops_mcp.system_info import list_processes

procs = list_processes(sort_by="cpu", limit=5)
assert len(procs) > 0, "No processes returned"
ok(f"Top 5 by CPU:")
for p in procs:
    print(f"    [{p['pid']:>6}]  {p['name']:<25}  cpu={p['cpu_percent']:>5.1f}%  "
          f"mem={p['mem_rss_mb']:>7.1f} MiB")

procs_mem = list_processes(sort_by="memory", limit=5)
ok(f"Top 5 by memory returned ({len(procs_mem)} entries)")


# ------------------------------------------------------------------ #
# 6. Shell runner — allowed command                                   #
# ------------------------------------------------------------------ #
section("6. Shell runner — allowed command")

from devops_mcp.shell_runner import run_command, parse_allowed_commands

ALLOWED = parse_allowed_commands("ls,cat,git,df,du,pwd,echo,env,whoami,uname")

result = run_command("echo hello devops-mcp", ALLOWED, timeout=5)
assert result.exit_code == 0, f"echo failed: {result}"
assert "hello devops-mcp" in result.stdout, f"Unexpected output: {result.stdout}"
ok(f"echo: '{result.stdout.strip()}'")

result2 = run_command("pwd", ALLOWED, timeout=5)
assert result2.exit_code == 0
ok(f"pwd: '{result2.stdout.strip()}'")

result3 = run_command("uname -s", ALLOWED, timeout=5)
assert result3.exit_code == 0
ok(f"uname -s: '{result3.stdout.strip()}'")


# ------------------------------------------------------------------ #
# 7. Shell runner — blocked command                                   #
# ------------------------------------------------------------------ #
section("7. Shell runner — blocked command (security check)")

blocked = run_command("rm -rf /tmp/test_devops_mcp", ALLOWED, timeout=5)
assert blocked.exit_code != 0 or blocked.error, "rm should have been blocked!"
assert "not in the allowed commands" in blocked.error
ok(f"'rm' correctly blocked: {blocked.error}")

blocked2 = run_command("python3 -c 'import os; os.system(\"id\")'", ALLOWED, timeout=5)
assert "not in the allowed commands" in blocked2.error
ok(f"'python3' correctly blocked")


# ------------------------------------------------------------------ #
# 8. Shell runner — timeout                                           #
# ------------------------------------------------------------------ #
section("8. Shell runner — timeout enforcement")

ALLOWED_WITH_SLEEP = ALLOWED | {"sleep"}
timed_out = run_command("sleep 5", ALLOWED_WITH_SLEEP, timeout=1)
assert timed_out.timed_out, "Expected timeout!"
ok(f"'sleep 5' correctly timed out after 1s")


# ------------------------------------------------------------------ #
# Done                                                                #
# ------------------------------------------------------------------ #
print(f"\n{'=' * 60}")
print("  ALL TESTS PASSED")
print("=" * 60)
