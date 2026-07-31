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


def validate_identity(state: dict, required_fields: tuple[str, ...], logger) -> dict[str, str]:
    """Validate and extract required identity fields from state.

    Args:
        state:          The sub-agent state dict.
        required_fields: Tuple of field names that must be present and non-empty.
        logger:         Logger instance for error/warning output.

    Returns:
        Dict of all required identity key-value pairs.

    Raises:
        KeyError:  If any required key is missing from state.
        ValueError: If any required field is an empty string.
    """
    # Check for missing keys.
    missing = [k for k in required_fields if k not in state]
    if missing:
        logger.error(
            "sub_agent_identity_missing",
            missing_keys=missing,
            available_keys=list(state.keys()),
        )
        raise KeyError(f"Missing required identity fields: {missing}")

    # Check for empty fields.
    identity = {}
    empty_fields = []

    for k in required_fields:
        v = state[k]
        if isinstance(v, str) and not v.strip():
            empty_fields.append(k)
        identity[k] = v

    if empty_fields:
        logger.warning(
            "sub_agent_identity_empty",
            empty_fields=empty_fields,
            sub_agent_id=identity.get("sub_agent_id", ""),
            task_id=identity.get("task_id", ""),
        )
        raise ValueError(f"Required identity fields are empty: {empty_fields}")

    return identity
