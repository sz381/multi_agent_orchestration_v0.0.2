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
}
