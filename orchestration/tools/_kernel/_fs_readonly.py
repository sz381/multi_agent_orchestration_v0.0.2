"""Read-only filesystem tools for exploring the workspace.

Provides 
view_file (read with line numbers), 
glob_tool (pattern matching), and
grep_tool (regex search).
"""

import os
import regex as re
import json
import fnmatch

from utils.common import get_workspace


# Directories pruned by both glob_tool and grep_tool: never returned and
# never descended into. Keeps dependency/cache noise out of the context.
# To scan inside one explicitly, set dir_path to it directly.
_EXCLUDE_DIRS = frozenset({
    # Version control
    ".git", ".svn", ".hg", ".bzr",
    # Virtual environments
    "venv", ".venv",
    # Python dependencies, caches and test artifacts
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".hypothesis", ".pyre", ".eggs", ".pdm-build",
    # Node / frontend deps and build caches
    "node_modules", "bower_components", "jspm_packages", ".npm", ".yarn",
    ".pnpm-store", ".turbo", ".next", ".nuxt", ".svelte-kit", ".vite",
    ".parcel-cache", ".webpack",
    # IDE / editor settings
    ".vscode", ".idea", ".intellij", ".cursor", ".eclipse", ".fleet", ".zed",
    # Tool / runtime caches
    ".qoder", ".cache", ".langchain", ".langsmith", ".chroma", ".streamlit",
    ".gradle", ".m2", ".cargo", ".rustup", ".terraform", ".serverless",
    ".amplify", ".trash", ".Trash",
    # Build outputs and coverage reports
    "dist", "build", "out", "target", "coverage", "htmlcov",
    # Log and temp dirs
    "log", "logs", ".log", "tmp", "temp",
})

# File-level noise filtered from glob results (and skipped by grep walk).
_EXCLUDE_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

# Hard caps for glob_tool (kept module-level so tests can shrink them).
_GLOB_MAX_RESULTS = 200
_GLOB_MAX_SCAN = 5000

# Maximum file size to read in bytes (1MB).
MAX_READ_SIZE = 1 * 1024 * 1024


def view_file(
    file_path: str,
    offset: int = 1,
    limit: int = 100,
    encoding: str = "utf-8",
    allow_external_reads: bool = False,
) -> str:
    """Read a file with line numbers, optionally starting at an offset.

    Supports large files by reading up to 1MB and chunking via offset.

    Args:
        file_path: Path to the file.
        offset: First line to show (1-based, default 1).
        limit: Max lines to return (1-1000, default 100).
        encoding: File encoding (default utf-8).
        allow_external_reads: Allow reading outside the workspace.

    Returns:
        JSON with status, line data, and pagination info.
    """
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        return json.dumps({
            "status": "error",
            "message": "limit must be an integer between 1 and 1000."
        }, ensure_ascii=False)

    if not isinstance(offset, int) or offset < 1:
        return json.dumps({
            "status": "error",
            "message": "offset must be a positive integer."
        }, ensure_ascii=False)

    if not file_path or not file_path.strip():
        return json.dumps({
            "status": "error",
            "message": "file_path must not be empty."
        }, ensure_ascii=False)

    try:
        safe_root = os.path.realpath(get_workspace())
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Cannot resolve workspace: {exc}"
        }, ensure_ascii=False)
    
    file_path = os.path.expanduser(file_path)

    try:
        if not os.path.isabs(file_path):
            file_path = os.path.realpath(os.path.join(safe_root, file_path))
        else:
            file_path = os.path.realpath(file_path)
    except OSError as exc:
        return json.dumps({
            "status": "error",
            "message": f"Invalid path: {exc}"
        }, ensure_ascii=False)
        
    safe_root = safe_root.rstrip(os.sep) + os.sep
    
    if not allow_external_reads and not file_path.startswith(safe_root):
        return json.dumps({
            "status": "error",
            "message": f"Access to '{file_path}' is denied."
        }, ensure_ascii=False)

    if not os.path.exists(file_path):
        return json.dumps({
            "status": "error",
            "message": f"File '{file_path}' does not exist."
        }, ensure_ascii=False)

    if os.path.isdir(file_path):
        return json.dumps({
            "status": "error",
            "message": f"'{file_path}' is a directory."
        }, ensure_ascii=False)

    try:
        with open(file_path, "r", encoding=encoding) as f:
            bytes_read = 0
            truncated = False
            lines: list[str] = []

            for raw_line in f:
                bytes_read += len(raw_line.encode("utf-8"))

                if bytes_read > MAX_READ_SIZE:
                    truncated = True
                    break

                lines.append(raw_line)
                
    except FileNotFoundError:
        return json.dumps({
            "status": "error",
            "message": f"File '{file_path}' does not exist."
        }, ensure_ascii=False)
    except IsADirectoryError:
        return json.dumps({
            "status": "error",
            "message": f"'{file_path}' is a directory."
        }, ensure_ascii=False)
    except PermissionError:
        return json.dumps({
            "status": "error",
            "message": f"Permission denied: '{file_path}'."
        }, ensure_ascii=False)
    except UnicodeDecodeError:
        return json.dumps({
            "status": "error",
            "message": (
                f"{file_path} cannot be decoded as UTF-8. "
                f"Retry with encoding='gbk' or 'latin-1' if needed."
            )
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Cannot read {file_path}: {exc}"
        }, ensure_ascii=False)

    total_lines = len(lines)

    if total_lines == 0:
        return json.dumps({
            "status": "ok",
            "path": file_path,
            "total_lines": 0,
            "start_line": 0,
            "end_line": 0,
            "remaining": 0,
            "truncated": False,
            "file_complete": True,
            "lines": [],
        }, ensure_ascii=False)

    if offset > total_lines:
        if truncated:
            return json.dumps({
                "status": "error",
                "message": (
                    f"Start line {offset} exceeds read limit ({total_lines} lines, "
                    f"{MAX_READ_SIZE / 1024 / 1024:.0f}MB). Use a smaller offset "
                    f"or view the file in chunks with offset=1 limit=100."
                )
            }, ensure_ascii=False)
            
        return json.dumps({
            "status": "error",
            "message": f"Start line {offset} exceeds total lines {total_lines}."
        }, ensure_ascii=False)

    start_idx = offset - 1
    end_idx = min(start_idx + limit, total_lines)
    selected_lines = lines[start_idx:end_idx]
    numbered_lines = [{"line_no": i, "content": line.rstrip("\n")} for i, line in enumerate(selected_lines, start=offset)]
    result = {
        "status": "ok",
        "path": file_path,
        "total_lines": total_lines,
        "start_line": offset,
        "end_line": end_idx,
        "remaining": total_lines - end_idx,
        "truncated": truncated,
        "file_complete": not truncated,
        "lines": numbered_lines,
    }

    if truncated:
        result["message"] = (
            f"File truncated at {MAX_READ_SIZE / 1024 / 1024:.0f}MB "
            f"(showing lines {offset}-{end_idx} of {total_lines} loaded). "
            f"Use offset={end_idx + 1} limit={limit} to continue reading."
        )

    return json.dumps(result, ensure_ascii=False)


def _glob_emit(path: str, results: list[str], limits: dict) -> None:
    """
    Record one match, enforcing the result/scan caps in ``limits``.
    """
    limits["total"] += 1
    if len(results) < _GLOB_MAX_RESULTS:
        results.append(path)
    if limits["total"] >= _GLOB_MAX_SCAN:
        limits["stop"] = True


def _glob_walk(
    dirpath: str,
    parts: list[str],
    results: list[str],
    limits: dict,
) -> None:
    """Recursively collect paths matching glob ``parts``.

    ``**`` matches zero or more directory levels; any other segment matches
    exactly one level via ``fnmatch``. Directories in ``_EXCLUDE_DIRS`` are
    pruned — never returned and never descended into. ``limits`` enforces
    the hard scan caps and early termination.
    """
    if limits["stop"]:
        return

    if not parts:
        _glob_emit(dirpath, results, limits)
        return

    head, *tail = parts

    try:
        entries = list(os.scandir(dirpath))
    except OSError:
        return

    if head == "**":
        # '**' matches zero levels: apply the remaining parts here.
        _glob_walk(dirpath, tail, results, limits)
        # '**' matches one or more levels: descend into subdirectories.
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and entry.name not in _EXCLUDE_DIRS:
                _glob_walk(entry.path, parts, results, limits)
                if limits["stop"]:
                    break
    else:
        for entry in entries:
            if not fnmatch.fnmatch(entry.name, head):
                continue
            if tail:
                if entry.is_dir(follow_symlinks=False) and entry.name not in _EXCLUDE_DIRS:
                    _glob_walk(entry.path, tail, results, limits)
            else:
                if entry.name in _EXCLUDE_FILES:
                    continue
                if entry.is_dir(follow_symlinks=False) and entry.name in _EXCLUDE_DIRS:
                    continue
                _glob_emit(entry.path, results, limits)
            if limits["stop"]:
                break


def glob_tool(
    pattern: str,
    dir_path: str = ".",
    allow_external_reads: bool = False,
) -> str:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. ``**/*.py``). Supports ``**`` for recursion.
        dir_path: Directory to search (default '.').
        allow_external_reads: Allow searching outside the workspace.

    Returns:
        JSON with matched file paths and count.
    """
    try:
        safe_root = os.path.realpath(get_workspace())
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Cannot resolve workspace: {exc}"
        }, ensure_ascii=False)

    if not pattern or not pattern.strip():
        return json.dumps({
            "status": "error",
            "message": "pattern must not be empty."
        }, ensure_ascii=False)

    if os.path.isabs(pattern) or ".." in pattern.split(os.sep):
        return json.dumps({
            "status": "error",
            "message": "pattern must be relative and must not contain '..'."
        }, ensure_ascii=False)

    if not dir_path or not dir_path.strip():
        return json.dumps({
            "status": "error",
            "message": "dir_path must not be empty."
        }, ensure_ascii=False)

    dir_path = os.path.expanduser(dir_path)

    try:
        if not os.path.isabs(dir_path):
            search_dir = os.path.realpath(os.path.join(safe_root, dir_path))
        else:
            search_dir = os.path.realpath(dir_path)
    except OSError as exc:
        return json.dumps({
            "status": "error",
            "message": f"Invalid path: {exc}"
        }, ensure_ascii=False)

    safe_root = safe_root.rstrip(os.sep) + os.sep

    if not allow_external_reads and not (search_dir + os.sep).startswith(safe_root):
        return json.dumps({
            "status": "error",
            "message": f"Access to '{dir_path}' is denied."
        }, ensure_ascii=False)

    if not os.path.isdir(search_dir):
        return json.dumps({
            "status": "error",
            "message": f"'{search_dir}' is not a directory."
        }, ensure_ascii=False)

    parts = [p for p in pattern.split("/") if p]

    if not parts:
        return json.dumps({
            "status": "error",
            "message": "pattern must not be empty."
        }, ensure_ascii=False)

    file_matches: list[str] = []
    limits = {"total": 0, "stop": False}

    try:
        _glob_walk(search_dir, parts, file_matches, limits)
    except RecursionError:
        return json.dumps({
            "status": "error",
            "message": "Scan failed: directory tree is too deep."
        }, ensure_ascii=False)
    except OSError as exc:
        return json.dumps({
            "status": "error",
            "message": f"Scan failed: {exc}"
        }, ensure_ascii=False)

    total = min(limits["total"], _GLOB_MAX_SCAN)
    file_matches.sort()
    truncated = total >= _GLOB_MAX_SCAN or len(file_matches) >= _GLOB_MAX_RESULTS

    return json.dumps({
        "status": "ok",
        "message": f"Found {total} files matching '{pattern}'" + (" (truncated)" if truncated else ""),
        "pattern": pattern,
        "count": total,
        "files": file_matches,
        "truncated": truncated,
    }, ensure_ascii=False)


def grep_tool(
    pattern: str,
    path: str = ".",
    glob_pattern: str | None = None,
    output_mode: str = "files_with_matches",
    context_lines: int = 2,
    head_limit: int = 200,
    offset: int = 0,
    case_sensitive: bool = True,
    multiline: bool = False,
    encoding: str = "utf-8",
    allow_external_reads: bool = False,
) -> str:
    """Search file contents with regex.

    Args:
        pattern: Regex pattern to search for.
        path: File or directory to search (default '.').
        glob_pattern: Filter files by name before searching.
        output_mode: One of ``files_with_matches``, ``content``, or ``count``.
        context_lines: Lines of context around each match (0-10).
        head_limit: Max results (0-1000, 0 = unlimited).
        offset: Skip first N results.
        case_sensitive: Whether to match case (default True).
        multiline: Whether ``.`` matches newlines (default False).
        encoding: File encoding (default utf-8).
        allow_external_reads: Allow searching outside the workspace.

    Returns:
        JSON with matches, count, and pagination info.
    """
    try:
        safe_root = os.path.realpath(get_workspace())
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Cannot resolve workspace: {exc}"
        }, ensure_ascii=False)

    if not path or not path.strip():
        return json.dumps({
            "status": "error",
            "message": "path must not be empty."
        }, ensure_ascii=False)

    if not pattern:
        return json.dumps({
            "status": "error",
            "message": "pattern cannot be empty"
        }, ensure_ascii=False)

    path = os.path.expanduser(path)

    try:
        if not os.path.isabs(path):
            real_path = os.path.realpath(os.path.join(safe_root, path))
        else:
            real_path = os.path.realpath(path)
    except OSError as exc:
        return json.dumps({
            "status": "error",
            "message": f"Invalid path: {exc}"
        }, ensure_ascii=False)

    safe_root = safe_root.rstrip(os.sep) + os.sep

    if not allow_external_reads and not (real_path + os.sep).startswith(safe_root):
        return json.dumps({
            "status": "error",
            "message": f"Access to '{path}' is denied."
        }, ensure_ascii=False)

    if output_mode not in ("files_with_matches", "content", "count"):
        return json.dumps({
            "status": "error",
            "message": f"Unknown output_mode: '{output_mode}'. Available: files_with_matches | content | count"
        }, ensure_ascii=False)
        
    if context_lines < 0 or context_lines > 10:
        return json.dumps({
            "status": "error",
            "message": "context_lines must be between 0 and 10."
        }, ensure_ascii=False)
        
    if head_limit < 0 or head_limit > 1000:
        return json.dumps({
            "status": "error",
            "message": "head_limit must be between 0 and 1000."
        }, ensure_ascii=False)
        
    if offset < 0:
        return json.dumps({
            "status": "error",
            "message": "offset cannot be negative"
        }, ensure_ascii=False)

    MAX_FILES = 5000
    files = []
    files_truncated = False
    
    if os.path.isfile(real_path):
        files.append(real_path)
    else:
        try:
            for dirpath, dirnames, filenames in os.walk(real_path):
                dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
                
                for fname in filenames:
                    if fname in _EXCLUDE_FILES:
                        continue
                    if len(files) >= MAX_FILES:
                        files_truncated = True
                        break
                    
                    full_path = os.path.join(dirpath, fname)
                    
                    if glob_pattern:
                        rel = os.path.relpath(full_path, real_path)
                        
                        if not fnmatch.fnmatch(rel, glob_pattern):
                            continue
                        
                    files.append(full_path)
                    
                if files_truncated:
                    break
                
        except OSError as exc:
            if not files:
                return json.dumps({
                    "status": "error",
                    "message": f"Cannot traverse directory: {exc}"
                }, ensure_ascii=False)

    flags = 0
    if not case_sensitive:
        flags |= re.IGNORECASE
        
    if multiline:
        flags |= re.DOTALL
        
    try:
        re_compiled = re.compile(pattern, flags)
    except re.error as exc:
        return json.dumps({
            "status": "error",
            "message": f"Invalid regex pattern: {exc}"
        }, ensure_ascii=False)

    MAX_FILE_SIZE = 1 * 1024 * 1024
    skipped_large_files: list[str] = []
    timed_out_files: list[str] = []
    file_matches: list[dict] = []
    _content_cache: dict[str, str] = {}
    
    for file_path in files:
        try:
            if not allow_external_reads and not os.path.realpath(file_path).startswith(safe_root):
                continue
            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                skipped_large_files.append(file_path)
                continue
            
            with open(file_path, "r", encoding=encoding) as f:
                file_content = f.read()

        except UnicodeDecodeError:
            continue
        except OSError:
            continue

        _matches_before = len(file_matches)

        if multiline:
            try:
                for m in re_compiled.finditer(file_content, timeout=2.0):
                    line_num = file_content[:m.start()].count("\n") + 1
                    line_text = m.group(0).split("\n")[0]
                    file_matches.append({
                        "file": file_path,
                        "line_num": line_num,
                        "line_text": line_text.rstrip()
                    })

            except TimeoutError:
                timed_out_files.append(file_path)
        else:
            lines = file_content.split("\n")

            for line_num, line_text in enumerate(lines, start=1):
                try:
                    if re_compiled.search(line_text, timeout=2.0):
                        file_matches.append({
                            "file": file_path,
                            "line_num": line_num,
                            "line_text": line_text.rstrip()
                        })

                except TimeoutError:
                    timed_out_files.append(file_path)
                    break

        if len(file_matches) > _matches_before:
            _content_cache[file_path] = file_content

    total_matches = len(file_matches)
    
    if total_matches == 0:
        msg = f"No matches for '{pattern}' in {len(files)} files"
        
        if files_truncated:
            msg += f" (file list truncated at {MAX_FILES})"
            
        if skipped_large_files:
            msg += f", {len(skipped_large_files)} large files skipped (>1MB)"
            
        if timed_out_files:
            msg += f", {len(timed_out_files)} files timed out"
            
        return json.dumps({
            "status": "ok",
            "output_mode": output_mode,
            "message": msg,
            "total_matches": 0,
            "total_files": 0,
            "files_scanned": len(files),
            "files_truncated": files_truncated,
            "skipped_large_files": len(skipped_large_files),
            "timed_out_files": len(timed_out_files),
        }, ensure_ascii=False)

    if offset >= total_matches:
        return json.dumps({
            "status": "error",
            "message": f"[ERROR] offset {offset} exceeds total matches {total_matches}"
        }, ensure_ascii=False)
        
    page_matches = file_matches[offset:offset + head_limit] if head_limit > 0 else file_matches[offset:]
    truncated = (offset + len(page_matches)) < total_matches
    _page_file_set = {m["file"] for m in page_matches}
    _content_cache = {k: v for k, v in _content_cache.items() if k in _page_file_set}

    if output_mode == "files_with_matches":
        visited_files_set: set[str] = set()
        unique_files_list: list[str] = []
        
        for m in page_matches:
            rel = os.path.relpath(m["file"], safe_root)
            
            if rel not in visited_files_set:
                visited_files_set.add(rel)
                unique_files_list.append(rel)
                
        return json.dumps({
            "status": "ok",
            "output_mode": "files_with_matches",
            "files": unique_files_list,
            "total_files": len(unique_files_list),
            "total_matches": total_matches,
            "truncated": truncated,
            "files_scanned": len(files),
            "files_truncated": files_truncated,
            "skipped_large_files": len(skipped_large_files),
            "timed_out_files": len(timed_out_files),
            "page": {"offset": offset, "limit": head_limit},
        }, ensure_ascii=False)

    if output_mode == "count":
        file_counts: dict[str, int] = {}
        
        for m in page_matches:
            rel = os.path.relpath(m["file"], safe_root)
            file_counts[rel] = file_counts.get(rel, 0) + 1
            
        return json.dumps({
            "status": "ok",
            "output_mode": "count",
            "results": file_counts,
            "total_occurrences": sum(file_counts.values()),
            "total_files": len(file_counts),
            "total_matches": total_matches,
            "truncated": truncated,
            "files_scanned": len(files),
            "files_truncated": files_truncated,
            "skipped_large_files": len(skipped_large_files),
            "timed_out_files": len(timed_out_files),
            "page": {"offset": offset, "limit": head_limit},
        }, ensure_ascii=False)

    if output_mode == "content":
        _file_lines_cache: dict[str, list[str]] = {}

        def _get_lines(fp: str) -> list[str]:
            if fp not in _file_lines_cache:
                if fp in _content_cache:
                    _file_lines_cache[fp] = _content_cache[fp].split("\n")
                else:
                    try:
                        with open(fp, "r", encoding=encoding) as f:
                            _file_lines_cache[fp] = f.readlines()
                    except (UnicodeDecodeError, OSError):
                        _file_lines_cache[fp] = []
                        
            return _file_lines_cache[fp]

        file_groups: dict[str, list[dict]] = {}
        
        for m in page_matches:
            rel = os.path.relpath(m["file"], safe_root)
            file_groups.setdefault(rel, []).append(m)

        results: dict[str, list[list[dict]]] = {}
        
        for rel, matches in file_groups.items():
            abs_path = os.path.join(safe_root, rel)
            file_lines = _get_lines(abs_path)
            
            if not file_lines:
                continue
            
            chunks: list[list[dict]] = []
            
            for m in matches:
                line_idx = m["line_num"] - 1
                start = max(0, line_idx - context_lines)
                end = min(len(file_lines), line_idx + context_lines + 1)
                chunk: list[dict] = []
                
                for i in range(start, end):
                    chunk.append({
                        "line_num": i + 1,
                        "content": file_lines[i].rstrip("\n"),
                        "match": (i + 1 == m["line_num"]),
                    })
                    
                chunks.append(chunk)
                
            results[rel] = chunks

        return json.dumps({
            "status": "ok",
            "output_mode": "content",
            "results": results,
            "total_matches": total_matches,
            "truncated": truncated,
            "files_scanned": len(files),
            "files_truncated": files_truncated,
            "skipped_large_files": len(skipped_large_files),
            "timed_out_files": len(timed_out_files),
            "page": {"offset": offset, "limit": head_limit},
        }, ensure_ascii=False)
