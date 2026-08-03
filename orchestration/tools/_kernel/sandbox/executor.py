"""
Execute bash commands inside macOS sandbox-exec + Seatbelt.
No fallback — if sandbox-exec fails, it's an error.
"""
import os
import signal
import subprocess
import tempfile

from orchestration.tools._kernel.sandbox.profile import generate_default, generate_air_gapped

MAX_OUTPUT_CHARS = 5000

_SANDBOX_ENV_STRIP = {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "PYTHONPATH", "GOPATH", "NODE_PATH", "PERL5LIB", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}

_SENSITIVE_ENV_KEYWORDS = [
    "API_KEY", "API_SECRET", "TOKEN", "SECRET", "PASSWORD",
    "CREDENTIAL", "PRIVATE_KEY",
]

# Project root — used to exclude the agent's own .venv from child processes
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

# Cache JAVA_HOME lookup so we only probe once
_JAVA_HOME_CACHE: str | None = None


def _is_sensitive_env(key: str) -> bool:
    """Check if an env var name looks like a secret (API key, token, etc.)."""
    upper = key.upper()
    return any(pattern in upper for pattern in _SENSITIVE_ENV_KEYWORDS)


def _find_java_home() -> str | None:
    """Probe JAVA_HOME if not set in env. macOS /usr/bin/java is a stub."""
    try:
        result = subprocess.run(
            ["/usr/libexec/java_home"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _sandbox_path() -> str:
    """Host PATH minus agent's own .venv/bin, everything else passes through."""
    host_path = os.environ.get("PATH", "")
    entries = []
    for entry in host_path.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith(_PROJECT_ROOT):
            continue
        entries.append(entry)
    return ":".join(entries) if entries else "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def _strip_sandbox_msgs(stderr: str) -> tuple[str, int]:
    """Filter macOS Seatbelt violation log lines from stderr.

    Returns the cleaned stderr and a count of violation lines removed.
    """
    lines = stderr.splitlines()
    kept = []
    violations = 0
    for line in lines:
        if (
            line.startswith("Sandbox:")
            or "Operation not permitted" in line
            or "Permission denied" in line
        ):
            violations += 1
        else:
            kept.append(line)
    return ("\n".join(kept), violations)


def _exec(profile_path: str, cmd: str, cwd: str, timeout: int) -> dict:
    """Run a command via sandbox-exec with the given profile.

    Uses Popen + start_new_session so that on timeout the entire
    process group (bash + all children) can be killed atomically.
    """
    # Don't leak the agent's venv or API keys into child processes.
    # Without this, child inherits VIRTUAL_ENV and the user's project
    # ends up using the agent's Python venv, breaking all dependencies.
    env = {k: v for k, v in os.environ.items() if k not in _SANDBOX_ENV_STRIP and not _is_sensitive_env(k)}
    
    # Exclude the agent's .venv/bin from PATH. Default TMPDIR and HOME
    # to /tmp so child processes don't read ~/.gitconfig etc.
    env["PATH"] = _sandbox_path()
    env.setdefault("TMPDIR", "/tmp")
    env.setdefault("HOME", "/tmp")

    # macOS /usr/bin/java is a stub — Gradle/Maven need JAVA_HOME set explicitly.
    if "JAVA_HOME" not in env:
        global _JAVA_HOME_CACHE
        if _JAVA_HOME_CACHE is None:
            _JAVA_HOME_CACHE = _find_java_home()
        if _JAVA_HOME_CACHE:
            env["JAVA_HOME"] = _JAVA_HOME_CACHE

    # pipefail: pipeline exit code = rightmost non-zero segment (not the last segment).
    # Prevents `cmd 2>&1 | tail -40` from masking a real failure with tail's exit 0.
    args = ["sandbox-exec", "-f", profile_path, "bash", "-o", "pipefail", "-c", cmd]

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,  # new session = new process group for clean kill
    )

    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        stderr, violations = _strip_sandbox_msgs(stderr)
        
    except subprocess.TimeoutExpired:
        # Kill the entire process group: bash + all children
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return {"exit_code": None, "stdout": "", "stderr": "", "timeout": True, "sandbox_violations": 0}

    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timeout": False,
        "sandbox_violations": violations,
    }


def run(cmd: str, workspace: str, cwd: str = ".", timeout: int = 30, allow_network: bool = True) -> dict:
    """
    Execute a bash command inside Seatbelt sandbox.

    Args:
        cmd: bash command to execute
        workspace: project root absolute path
        cwd: working directory (absolute path)
        timeout: timeout in seconds (default 30)
        allow_network: Use network profile if True, air-gapped if False.

    Returns:
        {"exit_code": int|None, "stdout": str, "stderr": str, "timeout": bool, "sandbox_violations": int}
    """
    profile_text = (
        generate_default(workspace=workspace)   
        if allow_network
        else generate_air_gapped(workspace=workspace)
    )

    # Write the sandbox profile to a temp file, then clean up after execution.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False) as pf:
        pf.write(profile_text)
        profile_path = pf.name

    try:
        result = _exec(profile_path, cmd, cwd, timeout)
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass

    # Truncate output
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if len(stdout) > MAX_OUTPUT_CHARS:
        stdout = stdout[:MAX_OUTPUT_CHARS] + f"\n... (stdout truncated, showing {MAX_OUTPUT_CHARS} chars)"
    if len(stderr) > MAX_OUTPUT_CHARS:
        stderr = stderr[:MAX_OUTPUT_CHARS] + f"\n... (stderr truncated, showing {MAX_OUTPUT_CHARS} chars)"

    return {
        "exit_code": result["exit_code"],
        "stdout": stdout,
        "stderr": stderr,
        "timeout": result.get("timeout", False),
        "sandbox_violations": result.get("sandbox_violations", 0),
    }
