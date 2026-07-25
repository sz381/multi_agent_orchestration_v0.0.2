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

# 项目根目录 —— 用于过滤 agent 自身 .venv 不传给子进程
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

# 只在首次调用时探测，避免每次 bash 都跑 subprocess
_JAVA_HOME_CACHE: str | None = None


def _is_sensitive_env(key: str) -> bool:
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
    """过滤 macOS Seatbelt 违规日志，保留业务 stderr。

    macOS 上 sandbox-exec 违规表现为 "Operation not permitted" /
    "Permission denied" 行（部分系统用 "Sandbox:" 前缀）。
    过滤这些行并返回违规次数。
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
    # 不把 agent 自己的 venv 传进去，过滤掉 API_KEY 等密钥
    # 为什么要过滤？如果不做这一步，子进程会继承 agent 的 VIRTUAL_ENV=/agent/.venv ，
    # 导致用户的项目用的 Python 变成 agent 的 venv，依赖全乱。
    env = {k: v for k, v in os.environ.items() if k not in _SANDBOX_ENV_STRIP and not _is_sensitive_env(k)}
    
    # PATH 排除 agent 的 .venv/bin，其他系统路径保留
    # 如果没设 TMPDIR，默认 /tmp
    # HOME 指向 /tmp，避免子进程读 ~/.gitconfig 等  
    env["PATH"] = _sandbox_path()
    env.setdefault("TMPDIR", "/tmp")
    env.setdefault("HOME", "/tmp")

    # macOS 的 /usr/bin/java 不指向任何 JDK，是个"占位符"。如果不设 JAVA_HOME ，Gradle/Maven 等工具会找不到 JDK。
    if "JAVA_HOME" not in env:
        global _JAVA_HOME_CACHE
        if _JAVA_HOME_CACHE is None:
            _JAVA_HOME_CACHE = _find_java_home()
        if _JAVA_HOME_CACHE:
            env["JAVA_HOME"] = _JAVA_HOME_CACHE

    # 构造命令
    args = ["sandbox-exec", "-f", profile_path, "bash", "-c", cmd]

    # 启动进程
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,     # 捕获标准输出
        stderr=subprocess.PIPE,     # 捕获标准错误
        cwd=cwd,                    # 设置工作目录
        env=env,                    # 净化后的环境变量
        start_new_session=True,     # 新会话 = 新进程组，方便整组杀，防止孤儿进程
    )

    try:
        # 等待 + 超时，communicate() 是阻塞的——等进程跑完或超时
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        stderr, violations = _strip_sandbox_msgs(stderr)
        
    # 超时处理
    except subprocess.TimeoutExpired:
        # 杀整个进程组：bash + 所有子进程（npm install / pip 等）
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
        allow_network: True 用联网 profile，False 用断网 profile（默认 True）

    Returns:
        {"exit_code": int|None, "stdout": str, "stderr": str, "timeout": bool, "sandbox_violations": int}
    """
    profile_text = (
        generate_default(workspace=workspace)   
        if allow_network
        else generate_air_gapped(workspace=workspace)
    )

    # 根据配置好的 profile 文件生成对应的临时的 “.sb” 沙盒配置文件，然后执行结束后 unlink。 
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
