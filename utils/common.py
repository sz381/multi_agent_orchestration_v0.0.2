"""
Shared utilities with no better home.
"""

import os

from utils.settings import settings


def get_workspace() -> str:
    """
    Return the configured workspace directory.

    Raises ``RuntimeError`` if ``workspace_dir`` is not set in settings.
    """
    workspace = settings.workspace_dir

    if not workspace:
        raise RuntimeError(
            "workspace_dir is not configured. Set it in .env or pass it explicitly."
        )

    return os.path.abspath(workspace)
