"""
description for the bash tool.
"""

TOOL_DESCRIPTION = {
    "bash": (
        "Execute a shell command inside a macOS Seatbelt sandbox.\n"
        "Parameters:\n"
        "- cmd: the command to run (e.g. 'pytest tests/ -v')\n"
        "- cwd: working directory relative to workspace (default '.')\n"
        "- timeout: max seconds before kill (default 30)\n"
        "- allow_network: enable outbound network access (default True)\n"
        "\n"
        "Each call is isolated — cd does NOT persist. Always pass cwd:\n"
        "  cmd='npm install --cache /tmp/npm-cache', cwd='frontend'   ← correct\n"
        "  cmd='cd frontend && npm install --cache /tmp/npm-cache'    ← correct\n"
        "\n"
        "Sandbox: file writes restricted to workspace + /tmp; "
        "dangerous commands (rm -rf /, sudo, curl|sh) blocked.\n"
        "macOS environment: `timeout` is NOT available (use `gtimeout` or `perl -e 'alarm N'`). "
        "No apt-get/yum/snap — use brew or pip/npm.\n"
        "Use for: tests, install deps, compile, git, lint, build.\n"
        "NOT for: long-running servers (dev server, uvicorn).\n"
        "Returns: exit_code, stdout, stderr, elapsed, sandbox_violations."
    ),
}
