"""
Generate macOS Seatbelt sandbox profile for sandbox-exec.
"""
import os


def _generate(workspace: str, allow_network: bool = True) -> str:
    """
    Generate a Seatbelt .sb profile string.

    Strategy:
      - (deny default): deny everything by default
      - file-read* globally: simple, never misses a path (like Qwen Code)
      - file-write* only inside workspace + /tmp
      - network: all or nothing
    """
    header = "(version 1)\n(deny default)\n"

    basic = """
            (allow process-exec*)
            (allow process-fork)
            (allow signal (target self))
            (allow sysctl-read)
            (allow mach-lookup)
            (allow file-map-executable)
            """

    reads = """
            (allow file-read*)
            """

    filesystem = f"""
            (allow file-read* file-write* (subpath "{workspace}"))
            (allow file-read* file-write* (subpath "/tmp"))
            (allow file-read* file-write* (subpath "/private/tmp"))
            (allow file-read* file-write* (subpath "/var/folders"))
            (allow file-read* file-write* (subpath "/private/var/folders"))
            (allow file-read* file-write* (literal "/dev/null"))
            """

    home = os.path.expanduser("~")
    denies = f"""
            (deny file-read*
                (subpath "{home}/.trae-cn")
                (subpath "{home}/.ssh")
                (literal "{home}/.zsh_history")
                (literal "{home}/.bash_history")
                (subpath "{home}/Library/Application Support/Google/Chrome")
                (subpath "{home}/Library/Application Support/Chromium")
                (subpath "{home}/Library/Keychains")
            )
            """

    if allow_network:
        network = "(allow network*)\n"
    else:
        network = "(deny network*)\n"

    return header + basic + filesystem + reads + denies + network


def generate_default(workspace: str) -> str:
    """Network mode: global read + workspace write + full network access."""
    return _generate(workspace, allow_network=True)


def generate_air_gapped(workspace: str) -> str:
    """Air-gapped mode: global read + workspace write + no network."""
    return _generate(workspace, allow_network=False)
