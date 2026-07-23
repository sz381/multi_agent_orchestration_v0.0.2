import os
import re
import json
import glob
import fnmatch

from utils.common import get_workspace


def view_file(
    file_path: str,
    offset: int = 1,
    limit: int = 100,
    allow_external_reads: bool = False,
) -> str:
    safe_root = get_workspace()
    
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
        return json.dumps(
            {"status": "error",
            "message": f"File '{file_path}' does not exist."
        }, ensure_ascii=False)
        
    if os.path.isdir(file_path):
        return json.dumps({
            "status": "error",
            "message": f"'{file_path}' is a directory."
        }, ensure_ascii=False)
    MAX_FILE_SIZE = 1 * 1024 * 1024

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            bytes_read = 0
            lines: list[str] = []
            for raw_line in f:
                bytes_read += len(raw_line.encode("utf-8"))
                if bytes_read > MAX_FILE_SIZE:
                    return json.dumps({
                        "status": "error",
                        "message": f"File too large (> {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)."
                    }, ensure_ascii=False)
                lines.append(raw_line)
    except UnicodeDecodeError:
        return json.dumps({
            "status": "error",
            "message": f"{file_path} is a binary file, cannot display as text."
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
            "lines": [],
        }, ensure_ascii=False)

    offset = 1 if offset < 1 else offset
    if offset > total_lines:
        return json.dumps({
            "status": "error",
            "message": f"Start line {offset} exceeds total lines {total_lines}."
        }, ensure_ascii=False)

    start_idx = offset - 1
    end_idx = min(start_idx + limit, total_lines)
    selected_lines = lines[start_idx:end_idx]
    numbered_lines = [{"line_no": i, "content": line.rstrip("\n")} for i, line in enumerate(selected_lines, start=offset)]

    return json.dumps({
        "status": "ok",
        "path": file_path,
        "total_lines": total_lines,
        "start_line": offset,
        "end_line": end_idx,
        "remaining": total_lines - end_idx,
        "lines": numbered_lines,
    }, ensure_ascii=False)


def glob_tool(
    pattern: str,
    dir_path: str = ".",
    allow_external_reads: bool = False,
) -> str:
    safe_root = get_workspace()
    
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

    full_pattern = os.path.join(search_dir, pattern)
    file_matches = glob.glob(full_pattern, recursive=True)

    MAX_RESULTS = 100
    total = len(file_matches)
    truncated = total > MAX_RESULTS
    if truncated:
        file_matches = file_matches[:MAX_RESULTS]

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
    allow_external_reads: bool = False,
) -> str:
    safe_root = get_workspace()
    
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
        
    if not os.path.exists(real_path):
        return json.dumps({
            "status": "error",
            "message": f"File '{path}' does not exist."
        }, ensure_ascii=False)
        
    if not pattern:
        return json.dumps({
            "status": "error",
            "message": "pattern cannot be empty"
        }, ensure_ascii=False)
        
    if output_mode not in ("files_with_matches", "content", "count"):
        return json.dumps({
            "status": "error",
            "message": f"Unknown output_mode: '{output_mode}'. Available: files_with_matches | content | count"
        }, ensure_ascii=False)
        
    if context_lines < 0:
        return json.dumps({
            "status": "error",
            "message": "context_lines cannot be negative"
        }, ensure_ascii=False)
        
    if head_limit < 0:
        return json.dumps({
            "status": "error",
            "message": "head_limit cannot be negative"
        }, ensure_ascii=False)
        
    if offset < 0:
        return json.dumps({
            "status": "error",
            "message": "offset cannot be negative"
        }, ensure_ascii=False)

    files = []
    if os.path.isfile(real_path):
        files.append(real_path)
    else:
        for dirpath, dirnames, filenames in os.walk(real_path):
            skip_dirs = {".git", "__pycache__", "node_modules", ".venv", ".tox", ".mypy_cache", ".pytest_cache"}
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                full_path = os.path.join(dirpath, fname)
                if glob_pattern:
                    rel = os.path.relpath(full_path, real_path)
                    if not fnmatch.fnmatch(rel, glob_pattern):
                        continue
                files.append(full_path)

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

    file_matches: list[dict] = []
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
        except (UnicodeDecodeError, Exception):
            continue

        if multiline:
            for m in re_compiled.finditer(file_content):
                line_num = file_content[:m.start()].count("\n") + 1
                line_text = m.group(0).split("\n")[0]
                file_matches.append({
                    "file": file_path,
                    "line_num": line_num,
                    "line_text": line_text.rstrip()
                })
        else:
            lines = file_content.split("\n")
            for line_num, line_text in enumerate(lines, start=1):
                if re_compiled.search(line_text):
                    file_matches.append({
                        "file": file_path,
                        "line_num": line_num,
                        "line_text": line_text.rstrip()
                    })

    total_matches = len(file_matches)
    if total_matches == 0:
        return json.dumps({
            "status": "ok",
            "message": f"No matches for '{pattern}' in {len(files)} files"
        }, ensure_ascii=False)

    if offset >= total_matches:
        return json.dumps({
            "status": "error",
            "message": f"[ERROR] offset {offset} exceeds total matches {total_matches}"
        }, ensure_ascii=False)
    page_matches = file_matches[offset:offset + head_limit] if head_limit > 0 else file_matches[offset:]
    truncated = (offset + len(page_matches)) < total_matches

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
            "page": {"offset": offset, "limit": head_limit},
        }, ensure_ascii=False)

    if output_mode == "content":
        _file_lines_cache: dict[str, list[str]] = {}

        def _get_lines(fp: str) -> list[str]:
            if fp not in _file_lines_cache:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        _file_lines_cache[fp] = f.readlines()
                except (UnicodeDecodeError, Exception):
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
                        "match": (file_lines[i].rstrip("\n") == m["line_text"]),
                    })
                chunks.append(chunk)
            results[rel] = chunks

        return json.dumps({
            "status": "ok",
            "output_mode": "content",
            "results": results,
            "total_matches": total_matches,
            "truncated": truncated,
            "page": {"offset": offset, "limit": head_limit},
        }, ensure_ascii=False)
