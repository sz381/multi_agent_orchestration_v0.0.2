import os
import json
import asyncio
import tempfile

from utils.common import get_workspace


_file_locks: dict[str, asyncio.Lock] = {}


def _get_file_lock(file_path: str) -> asyncio.Lock:
    return _file_locks.setdefault(file_path, asyncio.Lock())


async def str_replace(
    file_path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
    encoding: str = "utf-8",
) -> str:
    if not file_path or not file_path.strip():
        return json.dumps({
            "status": "error",
            "message": "file_path must not be empty."
        }, ensure_ascii=False)

    try:
        "".encode(encoding)
    except LookupError:
        return json.dumps({
            "status": "error",
            "message": f"Unknown encoding: '{encoding}'. Try 'utf-8', 'gbk', or 'latin-1'."
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
    if not file_path.startswith(safe_root):
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

    if not old_str:
        return json.dumps({
            "status": "error",
            "message": "old_str cannot be empty."
        }, ensure_ascii=False)

    async with _get_file_lock(file_path):
        try:
            with open(file_path, "r", encoding=encoding) as f:
                content = f.read(MAX_WRITE_SIZE + 1)
        except UnicodeDecodeError:
            return json.dumps({
                "status": "error",
                "message": f"{file_path} cannot be decoded as {encoding}. Retry with encoding='gbk' or 'latin-1'."
            }, ensure_ascii=False)
        except PermissionError:
            return json.dumps({
                "status": "error",
                "message": f"Permission denied: '{file_path}'."
            }, ensure_ascii=False)
        except OSError as exc:
            return json.dumps({
                "status": "error",
                "message": f"Cannot read {file_path}: {exc}"
            }, ensure_ascii=False)

        if len(content) > MAX_WRITE_SIZE:
            return json.dumps({
                "status": "error",
                "message": f"File '{file_path}' exceeds {MAX_WRITE_SIZE // 1024 // 1024}MB limit."
            }, ensure_ascii=False)

        count = content.count(old_str)
        if count == 0:
            return json.dumps({
                "status": "error",
                "message": f"Text not found in {file_path}. Use view_file to verify file content."
            }, ensure_ascii=False)

        if old_str == new_str:
            return json.dumps({
                "status": "ok",
                "message": f"[UNCHANGED] No changes to '{file_path}' — old_str and new_str are identical.",
                "diff": None,
            }, ensure_ascii=False)

        if count > 1 and not replace_all:
            return json.dumps({
                "status": "error",
                "message": f"Text matches {count} occurrences in {file_path}. Add more context to make it unique, or use replace_all=True."
            }, ensure_ascii=False)

        if replace_all:
            new_content = content.replace(old_str, new_str)
        else:
            new_content = content.replace(old_str, new_str, 1)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(file_path))
        try:
            with os.fdopen(tmp_fd, "w", encoding=encoding) as f:
                f.write(new_content)
            os.replace(tmp_path, file_path)
        except UnicodeEncodeError:
            return json.dumps({
                "status": "error",
                "message": f"new_str contains characters not encodable as {encoding}. Try encoding='utf-8'."
            }, ensure_ascii=False)
        except OSError as exc:
            return json.dumps({
                "status": "error",
                "message": f"Cannot write to {file_path}: {exc}"
            }, ensure_ascii=False)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return json.dumps({
            "status": "ok",
            "message": f"[REPLACED{' ALL' if replace_all else ''}] {file_path}"
                       + (f" ({count} occurrences)" if replace_all and count > 0 else ""),
            "path": file_path,
            "diff": {
                "old": old_str[:MAX_DIFF_SIZE] + "\n... [truncated]" if len(old_str) > MAX_DIFF_SIZE else old_str,
                "new": new_str[:MAX_DIFF_SIZE] + "\n... [truncated]" if len(new_str) > MAX_DIFF_SIZE else new_str,
                "count": count if replace_all else 1,
                "replace_all": replace_all,
            },
        }, ensure_ascii=False)


MAX_WRITE_SIZE = 1 * 1024 * 1024
MAX_DIFF_SIZE = 4096


async def write_file(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
) -> str:
    if not file_path or not file_path.strip():
        return json.dumps({
            "status": "error",
            "message": "file_path must not be empty."
        }, ensure_ascii=False)

    try:
        "".encode(encoding)
    except LookupError:
        return json.dumps({
            "status": "error",
            "message": f"Unknown encoding: '{encoding}'. Try 'utf-8', 'gbk', or 'latin-1'."
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
    if not file_path.startswith(safe_root):
        return json.dumps({
            "status": "error",
            "message": f"Access to '{file_path}' is denied."
        }, ensure_ascii=False)

    if not content:
        content = ""

    try:
        content_size = len(content.encode(encoding))
    except UnicodeEncodeError:
        return json.dumps({
            "status": "error",
            "message": f"Content contains characters not encodable as {encoding}. Try encoding='utf-8'."
        }, ensure_ascii=False)

    if content_size > MAX_WRITE_SIZE:
        return json.dumps({
            "status": "error",
            "message": f"Content size exceeds {MAX_WRITE_SIZE // 1024 // 1024}MB limit."
        }, ensure_ascii=False)

    if os.path.isdir(file_path):
        return json.dumps({
            "status": "error",
            "message": f"'{file_path}' is a directory."
        }, ensure_ascii=False)

    if os.path.dirname(file_path):
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

    async with _get_file_lock(file_path):
        existed = os.path.exists(file_path)
        old_content = ""

        if existed:
            if os.path.isdir(file_path):
                return json.dumps({
                    "status": "error",
                    "message": f"'{file_path}' is a directory."
                }, ensure_ascii=False)
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    old_content = f.read()
            except UnicodeDecodeError:
                return json.dumps({
                    "status": "error",
                    "message": f"{file_path} cannot be decoded as {encoding}. Retry with encoding='gbk' or 'latin-1'."
                }, ensure_ascii=False)
            except PermissionError:
                return json.dumps({
                    "status": "error",
                    "message": f"Permission denied: '{file_path}'."
                }, ensure_ascii=False)
            except OSError as exc:
                return json.dumps({
                    "status": "error",
                    "message": f"Cannot read {file_path}: {exc}"
                }, ensure_ascii=False)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(file_path))
        try:
            with os.fdopen(tmp_fd, "w", encoding=encoding) as f:
                f.write(content)
            os.replace(tmp_path, file_path)
        except OSError as exc:
            return json.dumps({
                "status": "error",
                "message": f"Cannot write to {file_path}: {exc}"
            }, ensure_ascii=False)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        action = "OVERWRITTEN" if existed else "CREATED"

        return json.dumps({
            "status": "ok",
            "message": f"[{action}] {file_path} ({line_count} lines)",
            "path": file_path,
            "diff": {
                "old": old_content[:MAX_DIFF_SIZE] + "\n... [truncated]" if len(old_content) > MAX_DIFF_SIZE else old_content,
                "new": content[:MAX_DIFF_SIZE] + "\n... [truncated]" if len(content) > MAX_DIFF_SIZE else content,
            },
        }, ensure_ascii=False)
