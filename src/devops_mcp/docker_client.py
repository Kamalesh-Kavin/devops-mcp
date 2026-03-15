"""
docker_client.py — Wrapper around the Docker Python SDK.

The Docker Python SDK (pip install docker) communicates with the Docker daemon
via a Unix socket (/var/run/docker.sock on macOS/Linux).  It mirrors the
Docker CLI but gives us a proper Python API with typed objects.

Key concepts:

1. docker.from_env()
   Creates a client by reading the DOCKER_HOST environment variable (or the
   default socket path).  This is the standard way to connect — it respects
   whatever Docker context the user has active (Docker Desktop, remote host,
   etc.).

2. Container objects
   The SDK returns Container objects with attributes like:
     - container.id          — full 64-char SHA
     - container.short_id    — first 12 chars (what the CLI shows)
     - container.name        — human-readable name
     - container.status      — "running", "exited", "paused", etc.
     - container.image       — Image object (use container.image.tags)
     - container.attrs       — full raw JSON inspect data

3. Stats streaming
   container.stats(stream=False) returns a single snapshot of CPU/memory
   metrics.  With stream=True it yields an infinite stream — we always use
   stream=False for a one-shot read.

4. CPU % calculation
   Docker reports raw CPU nanoseconds, not a percentage.  To compute the
   percentage we need two snapshots and apply the same formula the Docker
   CLI uses:
     cpu_delta    = cpu_stats.cpu_usage.total_usage - precpu_stats.cpu_usage.total_usage
     system_delta = cpu_stats.system_cpu_usage - precpu_stats.system_cpu_usage
     cpu_percent  = (cpu_delta / system_delta) * num_cpus * 100.0

   The single-shot stats() call conveniently includes both the current sample
   (cpu_stats) and the previous sample (precpu_stats) in the same dict.

All functions in this module are synchronous — the Docker SDK is synchronous,
and we run it from an async MCP server using asyncio.to_thread() so we never
block the event loop.
"""

import docker
import docker.errors
from docker import DockerClient
from docker.models.containers import Container
from typing import Any, cast


def get_client(docker_host: str | None = None) -> DockerClient:
    """
    Connect to the Docker daemon.

    Args:
        docker_host: Optional override for the socket URL, e.g.
                     "unix:///var/run/docker.sock".
                     If None, docker.from_env() reads DOCKER_HOST from env.

    Returns:
        An authenticated DockerClient.

    Raises:
        docker.errors.DockerException: if the daemon is unreachable.
    """
    if docker_host:
        return docker.DockerClient(base_url=docker_host)
    return docker.from_env()


def list_containers(client: DockerClient, all_containers: bool = True) -> list[dict]:
    """
    Return a summary of every container known to Docker.

    Args:
        client:         Open DockerClient.
        all_containers: If True (default), include stopped/exited containers.
                        If False, return only running containers.

    Returns:
        List of dicts, one per container:
        {
            "id":      str,   # short 12-char ID
            "name":    str,   # container name (without leading /)
            "image":   str,   # image tag, e.g. "postgres:16"
            "status":  str,   # "running", "exited", "paused", …
            "created": str,   # ISO-8601 creation time
            "ports":   dict,  # port bindings, e.g. {"5432/tcp": [{"HostPort": "5432"}]}
        }
        Sorted with running containers first, then by name.
    """
    containers: list[Container] = client.containers.list(all=all_containers)

    result = []
    for c in containers:
        # c.image fetches the image object from Docker — this can raise
        # ImageNotFound if the image was deleted but the container still exists
        # (a "dangling" container).  Fall back to the raw image ID in that case.
        try:
            img: Any = c.image
            image_tags: list[str] = img.tags if img else []
            image_str: str = image_tags[0] if image_tags else (img.short_id if img else "unknown")
        except docker.errors.ImageNotFound:
            # Read the image ID directly from the container's attrs dict
            image_str = (c.attrs or {}).get("Image", "unknown (image deleted)")

        # Container names have a leading "/" — strip it for readability
        name: str = cast(str, c.name).lstrip("/")

        # Port bindings live in c.ports — may be None for stopped containers
        ports = c.ports or {}

        result.append({
            "id":      c.short_id,
            "name":    name,
            "image":   image_str,
            "status":  c.status,
            "created": (c.attrs or {}).get("Created", ""),
            "ports":   ports,
        })

    # Sort: running first, then alphabetical by name
    result.sort(key=lambda x: (0 if x["status"] == "running" else 1, x["name"]))
    return result


def get_container_logs(
    client: DockerClient,
    container_name_or_id: str,
    tail: int = 100,
    timestamps: bool = True,
) -> str:
    """
    Fetch recent stdout+stderr logs from a container.

    Args:
        client:               Open DockerClient.
        container_name_or_id: Container name or ID (partial IDs work).
        tail:                 Number of lines from the end to return (default 100).
        timestamps:           Prefix each line with its Docker timestamp.

    Returns:
        Log output as a single string.

    Raises:
        docker.errors.NotFound: if the container doesn't exist.
    """
    container: Container = client.containers.get(container_name_or_id)

    # logs() returns bytes — decode to str
    raw: bytes = container.logs(
        stdout=True,
        stderr=True,
        timestamps=timestamps,
        tail=tail,
    )
    return raw.decode("utf-8", errors="replace")


def get_container_stats(client: DockerClient, container_name_or_id: str) -> dict:
    """
    Return a single snapshot of CPU and memory stats for a running container.

    The Docker stats API returns raw counters.  We do the math here so the
    MCP tool can return human-readable numbers.

    Args:
        client:               Open DockerClient.
        container_name_or_id: Container name or ID.

    Returns:
        {
            "id":           str,
            "name":         str,
            "status":       str,
            "cpu_percent":  float,   # e.g. 2.34 (%)
            "mem_usage_mb": float,   # current RSS in MiB
            "mem_limit_mb": float,   # container memory limit in MiB
            "mem_percent":  float,   # mem_usage / mem_limit * 100
            "net_rx_mb":    float,   # network bytes received (MiB)
            "net_tx_mb":    float,   # network bytes sent (MiB)
            "block_read_mb":  float, # disk read (MiB)
            "block_write_mb": float, # disk write (MiB)
        }

    Raises:
        docker.errors.NotFound: container not found.
        RuntimeError: container is not running (no live stats).
    """
    container: Container = client.containers.get(container_name_or_id)

    if container.status != "running":
        raise RuntimeError(
            f"Container '{container.name}' is {container.status}, not running. "
            "Stats are only available for running containers."
        )

    # stream=False → one dict snapshot; cast to Any to escape broken SDK stubs
    raw: Any = container.stats(stream=False)

    # --- CPU % ---
    cpu_delta = (
        raw["cpu_stats"]["cpu_usage"]["total_usage"]
        - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    )
    system_delta = (
        raw["cpu_stats"].get("system_cpu_usage", 0)
        - raw["precpu_stats"].get("system_cpu_usage", 0)
    )
    num_cpus = raw["cpu_stats"].get("online_cpus") or len(
        raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])
    )
    cpu_percent = 0.0
    if system_delta > 0 and cpu_delta > 0:
        cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

    # --- Memory ---
    mem_stats = raw.get("memory_stats", {})
    # "usage" includes file cache — subtract cache for true RSS
    mem_cache = mem_stats.get("stats", {}).get("cache", 0)
    mem_usage = mem_stats.get("usage", 0) - mem_cache
    mem_limit = mem_stats.get("limit", 0)
    mem_percent = (mem_usage / mem_limit * 100.0) if mem_limit > 0 else 0.0

    # --- Network I/O ---
    networks = raw.get("networks", {})
    net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
    net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

    # --- Block I/O ---
    blk_stats = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    blk_read  = sum(b["value"] for b in blk_stats if b.get("op") == "read")
    blk_write = sum(b["value"] for b in blk_stats if b.get("op") == "write")

    def to_mib(b: int) -> float:
        return round(b / (1024 * 1024), 2)

    return {
        "id":             container.short_id,
        "name":           cast(str, container.name).lstrip("/"),
        "status":         container.status,
        "cpu_percent":    round(cpu_percent, 2),
        "mem_usage_mb":   to_mib(mem_usage),
        "mem_limit_mb":   to_mib(mem_limit),
        "mem_percent":    round(mem_percent, 2),
        "net_rx_mb":      to_mib(net_rx),
        "net_tx_mb":      to_mib(net_tx),
        "block_read_mb":  to_mib(blk_read),
        "block_write_mb": to_mib(blk_write),
    }


def health_check(client: DockerClient) -> bool:
    """
    Ping the Docker daemon.  Returns True if reachable, False otherwise.
    """
    try:
        return client.ping()
    except Exception:
        return False
