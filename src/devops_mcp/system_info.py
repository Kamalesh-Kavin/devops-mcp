"""
system_info.py — Host system metrics via psutil.

psutil (process and system utilities) is a cross-platform library for
retrieving information on running processes and system utilization
(CPU, memory, disks, network, sensors).

Why psutil instead of shelling out to `top` / `ps`?
  - Cross-platform: same API on macOS, Linux, Windows
  - Typed Python objects instead of parsing text
  - More granular than CLI tools (per-CPU stats, per-connection details)
  - No subprocess overhead

Concepts used here:

1. psutil.cpu_percent(interval=1)
   Measures CPU usage over a 1-second window.  Without an interval it
   returns 0.0 for the very first call because there's no baseline.
   interval=1 blocks for 1 second but gives an accurate reading.
   We use interval=None + a prior call pattern in get_system_info() for speed.

2. psutil.virtual_memory()
   Returns a named tuple: total, available, percent, used, free, …
   "available" is the amount that can be given to processes *right now*
   (includes reclaimable file cache) — more useful than "free".

3. psutil.disk_usage(path)
   Returns total/used/free/percent for the filesystem at `path`.
   We always check "/" (root) and any additional paths the server cares about.

4. psutil.process_iter(attrs=[...])
   Yields Process objects, collecting only the specified attributes in one
   efficient kernel call per process (avoids repeated /proc reads).
   We sort by CPU or memory and return the top N.

All functions here are synchronous (psutil is sync).  The MCP server
calls them via asyncio.to_thread() to avoid blocking the event loop.
"""

import os
import platform
import psutil
from datetime import datetime, timezone


def get_system_info() -> dict:
    """
    Return a snapshot of overall host system metrics.

    Returns:
        {
            "hostname":       str,
            "os":             str,   # e.g. "macOS 14.5"
            "python_version": str,
            "cpu_count":      int,   # logical cores
            "cpu_physical":   int,   # physical cores
            "cpu_percent":    float, # usage over last 0.1s
            "cpu_freq_mhz":   float | None,
            "mem_total_gb":   float,
            "mem_used_gb":    float,
            "mem_available_gb": float,
            "mem_percent":    float,
            "swap_total_gb":  float,
            "swap_used_gb":   float,
            "disk_root":      dict,  # usage for "/"
            "load_avg":       list[float] | None,  # 1/5/15 min (None on Windows)
            "boot_time":      str,   # ISO-8601
        }
    """
    import sys

    # CPU
    cpu_count_logical  = psutil.cpu_count(logical=True)  or 0
    cpu_count_physical = psutil.cpu_count(logical=False) or 0
    # interval=0.1: short measurement window — good enough for a snapshot
    cpu_percent = psutil.cpu_percent(interval=0.1)
    cpu_freq = psutil.cpu_freq()

    # Memory
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    def to_gb(b: int) -> float:
        return round(b / (1024 ** 3), 2)

    # Disk — root filesystem
    disk = psutil.disk_usage("/")
    disk_root = {
        "path":       "/",
        "total_gb":   to_gb(disk.total),
        "used_gb":    to_gb(disk.used),
        "free_gb":    to_gb(disk.free),
        "percent":    disk.percent,
    }

    # Load average (unavailable on Windows)
    try:
        la = list(os.getloadavg())
        load_avg = [round(x, 2) for x in la]
    except (AttributeError, OSError):
        load_avg = None

    # OS info
    uname = platform.uname()
    os_str = f"{uname.system} {uname.release}"

    # Boot time
    boot_ts = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).isoformat()

    return {
        "hostname":         uname.node,
        "os":               os_str,
        "python_version":   sys.version.split()[0],
        "cpu_count":        cpu_count_logical,
        "cpu_physical":     cpu_count_physical,
        "cpu_percent":      cpu_percent,
        "cpu_freq_mhz":     round(cpu_freq.current, 1) if cpu_freq else None,
        "mem_total_gb":     to_gb(vm.total),
        "mem_used_gb":      to_gb(vm.used),
        "mem_available_gb": to_gb(vm.available),
        "mem_percent":      vm.percent,
        "swap_total_gb":    to_gb(swap.total),
        "swap_used_gb":     to_gb(swap.used),
        "disk_root":        disk_root,
        "load_avg":         load_avg,
        "boot_time":        boot_ts,
    }


def list_processes(
    sort_by: str = "cpu",
    limit: int = 20,
) -> list[dict]:
    """
    Return the top N processes sorted by CPU or memory usage.

    Why we collect attrs up front:
      psutil.process_iter(attrs=[...]) fetches all specified attributes
      in a single /proc read per process rather than one syscall per
      attribute.  This is significantly faster for large process tables.

    Args:
        sort_by: "cpu" (default) or "memory" — which metric to rank by.
        limit:   Maximum number of processes to return (default 20).

    Returns:
        List of dicts, sorted descending:
        {
            "pid":        int,
            "name":       str,
            "status":     str,   # "running", "sleeping", "zombie", …
            "cpu_percent": float,
            "mem_percent": float,
            "mem_rss_mb":  float, # resident set size in MiB
            "username":   str,
            "cmdline":    str,   # first 120 chars of the command line
            "created":    str,   # ISO-8601
        }
    """
    attrs = [
        "pid", "name", "status",
        "cpu_percent", "memory_percent", "memory_info",
        "username", "cmdline", "create_time",
    ]

    procs = []
    for proc in psutil.process_iter(attrs=attrs):
        try:
            info = proc.info  # dict of the attrs we requested
            if info["cpu_percent"] is None:
                info["cpu_percent"] = 0.0

            mem_rss = 0.0
            if info.get("memory_info"):
                mem_rss = round(info["memory_info"].rss / (1024 * 1024), 2)

            cmdline = " ".join(info.get("cmdline") or [])
            cmdline = cmdline[:120] + ("…" if len(cmdline) > 120 else "")

            created = ""
            if info.get("create_time"):
                created = datetime.fromtimestamp(
                    info["create_time"], tz=timezone.utc
                ).isoformat()

            procs.append({
                "pid":         info["pid"],
                "name":        info["name"] or "",
                "status":      info["status"] or "",
                "cpu_percent": round(info["cpu_percent"] or 0.0, 2),
                "mem_percent": round(info.get("memory_percent") or 0.0, 2),
                "mem_rss_mb":  mem_rss,
                "username":    info.get("username") or "",
                "cmdline":     cmdline,
                "created":     created,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Processes can disappear between listing and reading — skip them
            continue

    # Sort descending by the chosen metric
    key = "cpu_percent" if sort_by == "cpu" else "mem_percent"
    procs.sort(key=lambda p: p[key], reverse=True)

    return procs[:limit]
