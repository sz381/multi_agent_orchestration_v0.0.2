"""
LLM-facing descriptions for the filesystem write tools.
"""

TOOL_DESCRIPTION = {
    "str_replace": (
        "Replace exact text in an existing file (atomic write).\n"
        "Params:\n"
        "- file_path: path to the file\n"
        "- old_str: exact text to replace — must match including whitespace/indentation. "
        "Include 2-3 surrounding lines for uniqueness.\n"
        "- new_str: replacement text\n"
        "- replace_all: replace all occurrences (default False)\n"
        "- encoding: file encoding (default 'utf-8', try 'gbk'/'latin-1')\n"
        "Errors: 'not found' → verify with view_file; 'N occurrences' → add context or use replace_all=True.\n"
        "Limits: max 1MB file. diff fields truncated at 4KB."
    ),
    "write_file": (
        "Create a new file or overwrite an existing one (atomic write). "
        "Creates parent directories automatically.\n"
        "Params:\n"
        "- file_path: path to the file\n"
        "- content: complete file content (max 1MB)\n"
        "- encoding: file encoding (default 'utf-8', try 'gbk'/'latin-1')\n"
        "Use str_replace for small edits to existing files — do NOT rewrite whole files for minor changes.\n"
        "Limits: content max 1MB. diff fields truncated at 4KB."
    ),
    "clean_dir": (
        "Safely delete files/directories inside the workspace. Use this INSTEAD of rm -rf in bash "
        "(rm -rf with absolute paths is blocked by the security policy).\n"
        "Params:\n"
        "- dir_path: target file or directory (workspace-relative or absolute)\n"
        "- patterns: optional list of name patterns (fnmatch, e.g. ['__pycache__', '*.pyc']). "
        "When given, only matching entries under dir_path are deleted and dir_path itself is KEPT.\n"
        "Examples: clean_dir('backend/venv') → removes the whole venv tree; "
        "clean_dir('backend', ['__pycache__', '*.pyc']) → strips caches, keeps backend.\n"
        "Guards: the workspace root can NEVER be deleted; '..'/symlink escapes rejected; "
        "max 500 items per call (narrow patterns or split dirs if exceeded).\n"
        "Deletion is PERMANENT — glob_tool first to confirm what matches."
    ),
}
