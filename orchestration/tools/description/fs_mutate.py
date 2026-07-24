TOOL_DESCRIPTION = {
    "str_replace": (
        "Replace exact text in an existing file (atomic). Include 2-3 lines of context "
        "in old_str for uniqueness. Use replace_all=True to replace all occurrences.\n"
        "Parameters:\n"
        "- file_path: path to the file\n"
        "- old_str: exact text to replace (must match including whitespace)\n"
        "- new_str: replacement text\n"
        "- replace_all: replace all occurrences (default False)\n"
    ),
    "write_file": (
        "Create a new file or overwrite an existing one (atomic). "
        "Creates parent directories automatically. Use str_replace for edits to existing files.\n"
        "Parameters:\n"
        "- file_path: path to the file\n"
        "- content: complete file content\n"
    ),
}
