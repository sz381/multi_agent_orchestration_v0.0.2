"""Bash tool: execute shell commands inside a macOS Seatbelt sandbox.

Commands are validated against a blocklist before execution.
"""

import os
import re
import json
import time

from orchestration.tools._kernel.sandbox.executor import run as sandbox_run
from utils.common import get_workspace

BLACKLIST_PATTERNS = [
    r"\brm\s+-rf\s+/",
    r"-rf\s+/",
    r"\bsudo\b",
    r"\bchmod\s+777",
    r"\bmkfs\.",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\bcurl.*\|\s*(\$\(.*\)|(ba)?sh)",
    r"\bwget.*\|\s*(\$\(.*\)|(ba)?sh)",
    r"\bbase64\b.*\|\s*(ba)?sh",
    r"\bpython.*-c.*base64.*\|.*sh",
    r"\bshutdown\b",
    r"\breboot\b",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
]


def bash(
    cmd: str,
    cwd: str = ".",
    timeout: int = 30,
    allow_network: bool = True,
) -> str:
    """Execute a shell command inside the Seatbelt sandbox.

    Args:
        cmd: The shell command to run.
        cwd: Working directory relative to workspace (default '.').
        timeout: Max seconds before kill (default 30).
        allow_network: Whether to allow network access (default True).

    Returns:
        JSON with exit_code, stdout, stderr, elapsed, and sandbox_violations.
    """
    for pattern in BLACKLIST_PATTERNS:
        if re.search(pattern, cmd):
            return json.dumps({
                "status": "error",
                "tool_name": "bash",
                "message": f"Command blocked by security policy: {cmd}",
            }, ensure_ascii=False)

    safe_root = get_workspace()
    safe_root = safe_root.rstrip(os.sep) + os.sep
    if not os.path.isabs(cwd):
        cwd = os.path.realpath(os.path.join(safe_root, cwd))
    if not (cwd + os.sep).startswith(safe_root):
        return json.dumps({
            "status": "error",
            "tool_name": "bash",
            "message": f"cwd '{cwd}' is outside the workspace.",
        }, ensure_ascii=False)

    if timeout <= 0:
        return json.dumps({
            "status": "error",
            "tool_name": "bash",
            "message": f"timeout must be > 0, got {timeout}.",
        }, ensure_ascii=False)

    started = time.monotonic()
    try:
        result = sandbox_run(
            cmd=cmd,
            workspace=safe_root,
            cwd=cwd,
            timeout=timeout,
            allow_network=allow_network,
        )
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "tool_name": "bash",
            "message": f"Bash execution failed: {exc}",
        }, ensure_ascii=False)
    elapsed = time.monotonic() - started

    return json.dumps({
        "status": "ok",
        "tool_name": "bash",
        "command": cmd,
        "exit_code": result["exit_code"],
        "stdout": result["stdout"].strip(),
        "stderr": result["stderr"].strip(),
        "timeout": result["timeout"],
        "sandbox_violations": result.get("sandbox_violations", 0),
        "elapsed": round(elapsed, 1),
    }, ensure_ascii=False)
