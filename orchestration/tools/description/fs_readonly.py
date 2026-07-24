TOOL_DESCRIPTION = {
    "glob": (
        "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Supports ** for recursive matching. Returns matching file paths and count.\n"
        "Parameters:\n"
        "- pattern: glob pattern to match\n"
        "- dir_path: directory to search in (default '.')\n"
        "- allow_external_reads: allow reading outside workspace (default False)\n"
    ),
    "view_file": (
        "Read a file with line numbers. Supports pagination via offset/limit.\n"
        "Parameters:\n"
        "- file_path: path to the file\n"
        "- offset: start line (1-based, default 1)\n"
        "- limit: max lines to read (default 100)\n"
        "- allow_external_reads: allow reading outside workspace (default False)\n"
    ),
    "grep": (
        "Search file contents with regex. Use to FIND code locations before reading.\n"
        "Parameters:\n"
        "- pattern: regex pattern\n"
        "- path: file or directory to search (default '.')\n"
        "- glob_pattern: filter files by name, e.g. '*.py' (default all)\n"
        "- output_mode: 'files_with_matches' (default) | 'content' | 'count'\n"
        "- context_lines: lines around match in content mode (default 2)\n"
        "- head_limit: max results (default 200)\n"
        "- offset: skip first N results\n"
        "- case_sensitive: match case (default True)\n"
        "- multiline: allow . to match newlines (default False)\n"
        "- allow_external_reads: allow searching outside workspace (default False)\n"
    ),
}
