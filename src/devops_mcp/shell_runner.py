"""
shell_runner.py — Sandboxed subprocess executor for the devops-mcp server.

Why sandboxing matters:
  Giving an AI model the ability to run arbitrary shell commands is a serious
  security risk.  If the MCP server ever runs in a multi-user environment, or
  if a prompt-injection attack tricks Claude into calling run_command with
  malicious input, unrestricted execution could cause real damage.

  We apply three layers of defense:

  1. ALLOWLIST — only executables explicitly listed in ALLOWED_COMMANDS can
     be invoked.  Anything else is rejected before a subprocess is spawned.

  2. ARGUMENT SANITISATION — we split the command string into tokens and check
     that the *first* token (the executable) is on the allowlist.  We never
     pass the command to the shell with shell=True (which would allow
     shell metacharacters like ; | & ` $() to chain arbitrary commands).

  3. TIMEOUT — every command has a hard wall-clock timeout (default 10 s).
     If the process doesn't finish in time it is killed and an error is returned.
     This prevents runaway processes from blocking the MCP server.

  What we deliberately do NOT do (not needed for a local dev tool):
  - chroot / container isolation (overkill for local dev use)
  - Network namespace restriction
  - Resource limits (ulimit) — the timeout + allowlist is sufficient here

How subprocess.run works:
  subprocess.run(args, capture_output=True, text=True, timeout=N)
  - args: list of strings — first element is the executable, rest are args
  - capture_output=True: redirect stdout+stderr to pipes (don't print to terminal)
  - text=True: decode output as UTF-8 automatically
  - timeout=N: raise subprocess.TimeoutExpired if the process runs > N seconds
  - check=False: don't raise on non-zero exit code (we handle it ourselves)

  NOT using shell=True is critical — it means the OS exec()s the binary
  directly without a shell interpreter, so no shell metacharacters are
  interpreted.
"""

import shlex
import subprocess
from dataclasses import dataclass


@dataclass
class CommandResult:
    """Return value from run_command()."""
    command:     str        # the original command string
    exit_code:   int        # 0 = success, non-zero = error
    stdout:      str        # captured standard output
    stderr:      str        # captured standard error
    timed_out:   bool       # True if the timeout was hit
    error:       str        # human-readable error message (empty on success)

    def to_dict(self) -> dict:
        return {
            "command":   self.command,
            "exit_code": self.exit_code,
            "stdout":    self.stdout,
            "stderr":    self.stderr,
            "timed_out": self.timed_out,
            "error":     self.error,
        }


def run_command(
    command: str,
    allowed_commands: set[str],
    timeout: int = 10,
    cwd: str | None = None,
) -> CommandResult:
    """
    Execute a shell command safely within the configured sandbox constraints.

    Args:
        command:          The full command string, e.g. "ls -la /tmp"
        allowed_commands: Set of permitted executable names, e.g. {"ls", "cat", "git"}
        timeout:          Hard timeout in seconds (default 10).
        cwd:              Working directory for the command.  None = inherit
                          the server process's cwd.

    Returns:
        CommandResult with exit code, stdout, stderr, and any error message.

    Security:
        - The executable (first token) must be in allowed_commands.
        - shell=False prevents metacharacter injection.
        - Timeout kills runaway processes.
    """
    # --- Parse the command string into tokens ---
    # shlex.split handles quoted strings correctly:
    #   'git log --oneline -n 5'  →  ['git', 'log', '--oneline', '-n', '5']
    #   'cat "my file.txt"'       →  ['cat', 'my file.txt']
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return CommandResult(
            command=command, exit_code=1, stdout="", stderr="",
            timed_out=False,
            error=f"Failed to parse command: {e}",
        )

    if not tokens:
        return CommandResult(
            command=command, exit_code=1, stdout="", stderr="",
            timed_out=False,
            error="Empty command.",
        )

    # --- Allowlist check ---
    # We check only the basename of the executable so that full paths like
    # /bin/ls still work — but note that /bin/ls and ls are the same binary.
    executable = tokens[0]
    exe_basename = executable.split("/")[-1]  # e.g. "/usr/bin/git" → "git"

    if exe_basename not in allowed_commands:
        return CommandResult(
            command=command, exit_code=1, stdout="", stderr="",
            timed_out=False,
            error=(
                f"'{exe_basename}' is not in the allowed commands list. "
                f"Allowed: {', '.join(sorted(allowed_commands))}"
            ),
        )

    # --- Execute ---
    try:
        result = subprocess.run(
            tokens,
            capture_output=True,   # pipe stdout and stderr
            text=True,             # decode as UTF-8
            timeout=timeout,
            cwd=cwd,
            shell=False,           # IMPORTANT: never use shell=True
        )
        return CommandResult(
            command=command,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=False,
            error="" if result.returncode == 0 else f"Command exited with code {result.returncode}",
        )

    except subprocess.TimeoutExpired:
        return CommandResult(
            command=command, exit_code=-1, stdout="", stderr="",
            timed_out=True,
            error=f"Command timed out after {timeout} seconds.",
        )
    except FileNotFoundError:
        return CommandResult(
            command=command, exit_code=1, stdout="", stderr="",
            timed_out=False,
            error=f"Executable '{executable}' not found on PATH.",
        )
    except PermissionError:
        return CommandResult(
            command=command, exit_code=1, stdout="", stderr="",
            timed_out=False,
            error=f"Permission denied executing '{executable}'.",
        )
    except Exception as e:
        return CommandResult(
            command=command, exit_code=1, stdout="", stderr="",
            timed_out=False,
            error=f"Unexpected error: {type(e).__name__}: {e}",
        )


def parse_allowed_commands(env_value: str) -> set[str]:
    """
    Parse the ALLOWED_COMMANDS environment variable into a set of strings.

    Args:
        env_value: Comma-separated string, e.g. "ls,cat,git,df,pwd,echo"

    Returns:
        Set of stripped, non-empty command names.
    """
    return {cmd.strip() for cmd in env_value.split(",") if cmd.strip()}
