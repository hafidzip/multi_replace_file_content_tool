"""
multi_replace_file_content Tool
================================
Edit multiple non-adjacent blocks in a single file in one atomic call.

Each chunk specifies its own search range and target text to replace.
Chunks are applied in reverse line-order so earlier line numbers remain
valid after each substitution.

Parameters:
  path   - Absolute path to the file to edit (required).
  chunks - List of replacement chunk objects (required, min 1).

Each chunk object has:
  start_line          - Start of search range, 1-indexed (required).
  end_line            - End of search range, 1-indexed inclusive (required).
  target_content      - Exact text to find within the range (required).
  replacement_content - New text to substitute in (required).
  allow_multiple      - Replace all occurrences in range (default false).
"""

import asyncio
import ctypes
import logging
import os
import sys
import tempfile
import threading
from typing import Any, Dict, List
import aiofiles
from openchadpy.tool_base import ToolBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Atomic replace helper
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _MOVEFILE_REPLACE_EXISTING: int = 0x00000001
    _MOVEFILE_WRITE_THROUGH: int = 0x00000008
    _REPLACEFILE_WRITE_THROUGH: int = 0x00000001
    _REPLACEFILE_IGNORE_MERGE_ERRORS: int = 0x00000002

    def _atomic_replace(src: str, dst: str) -> None:
        """Atomically replace *dst* with *src* using Win32 APIs.

        * Existing destination  -> ReplaceFileW   (preserves metadata, most atomic)
        * New destination       -> MoveFileExW    (single kernel rename)
        Both paths include WRITE_THROUGH to guarantee data hits disk before return.
        """
        if os.path.exists(dst):
            ok = _kernel32.ReplaceFileW(
                dst,   # lpReplacedFileName
                src,   # lpReplacementFileName
                None,  # lpBackupFileName  (no backup)
                _REPLACEFILE_WRITE_THROUGH | _REPLACEFILE_IGNORE_MERGE_ERRORS,
                None,
                None,
            )
            if not ok:
                raise OSError(
                    f"ReplaceFileW failed (error {ctypes.GetLastError()})"
                )
        else:
            ok = _kernel32.MoveFileExW(
                src,
                dst,
                _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
            )
            if not ok:
                raise OSError(
                    f"MoveFileExW failed (error {ctypes.GetLastError()})"
                )
else:
    def _atomic_replace(src: str, dst: str) -> None:  # type: ignore[misc]
        """Atomically replace *dst* with *src* (POSIX rename)."""
        os.replace(src, dst)


# ---------------------------------------------------------------------------
# Per-file lock registry
# ---------------------------------------------------------------------------
_file_locks: Dict[str, asyncio.Lock] = {}
_registry_lock = threading.Lock()


def _get_file_lock(path: str) -> asyncio.Lock:
    """Return (creating if needed) the asyncio.Lock for *path*.

    The canonical path (resolved symlinks + normalised case) is used as the
    key so different string representations of the same file share one lock.
    No deadlock is possible: each operation acquires at most one file lock.
    """
    canonical = os.path.normcase(os.path.realpath(path))
    with _registry_lock:
        if canonical not in _file_locks:
            _file_locks[canonical] = asyncio.Lock()
        return _file_locks[canonical]


class Tool(ToolBase):
    name = "multi_replace_file_content"
    description = (
        "Edit multiple non-adjacent blocks in a single file with one call. "
        "Each chunk describes a search range and the exact text to replace. "
        "Chunks are applied in reverse line order to preserve correct offsets. "
        "Use replace_file_content for a single contiguous edit."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to edit.",
            },
            "chunks": {
                "type": "array",
                "description": "Ordered list of replacement chunks to apply.",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "start_line": {
                            "type": "integer",
                            "description": "Start of search range, 1-indexed inclusive.",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "End of search range, 1-indexed inclusive.",
                        },
                        "target_content": {
                            "type": "string",
                            "description": (
                                "Exact text to find within the range. "
                                "Must match character-for-character including whitespace."
                            ),
                        },
                        "replacement_content": {
                            "type": "string",
                            "description": "New text to substitute in place of target_content.",
                        },
                        "allow_multiple": {
                            "type": "boolean",
                            "description": (
                                "When true, all occurrences within the range are replaced. "
                                "When false (default), errors if multiple matches are found."
                            ),
                        },
                    },
                    "required": [
                        "start_line",
                        "end_line",
                        "target_content",
                        "replacement_content",
                    ],
                },
            },
        },
        "required": ["path", "chunks"],
    }
    allowed_callers = ["direct", "code_execution"]

    async def execute(self, **kwargs) -> Dict[str, Any]:  # noqa: C901
        path: str = kwargs.get("path", "").strip()
        chunks: List[Dict[str, Any]] = kwargs.get("chunks", [])

        if not path:
            return {"error": "path is required and must not be empty."}
        if not chunks:
            return {"error": "chunks must be a non-empty list."}

        # --- Validate each chunk before touching the file ---
        errors: List[str] = []
        for i, chunk in enumerate(chunks):
            sl = chunk.get("start_line")
            el = chunk.get("end_line")
            tc = chunk.get("target_content")
            if sl is None or el is None:
                errors.append(f"Chunk {i}: start_line and end_line are required.")
            elif int(sl) < 1 or int(el) < int(sl):
                errors.append(
                    f"Chunk {i}: invalid range start_line={sl}, end_line={el}."
                )
            if not tc:
                errors.append(f"Chunk {i}: target_content must not be empty.")
            if "replacement_content" not in chunk:
                errors.append(f"Chunk {i}: replacement_content is required.")
        if errors:
            return {"error": "Validation errors in chunks:\n" + "\n".join(errors)}

        lock = _get_file_lock(path)
        await lock.acquire()
        try:
            if not os.path.isfile(path):
                return {"error": f"File not found: {path!r}"}

            # --- Read file ---
            try:
                async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = await f.read()
                all_lines: List[str] = content.splitlines(keepends=True)
            except Exception as e:
                return {"error": f"Failed to read file: {e}"}

            total_lines = len(all_lines)

            # --- Apply chunks in reverse start_line order to preserve offsets ---
            sorted_chunks = sorted(chunks, key=lambda c: int(c["start_line"]), reverse=True)

            results: List[Dict[str, Any]] = []
            for i, chunk in enumerate(sorted_chunks):
                sl = int(chunk["start_line"])
                el = min(int(chunk["end_line"]), total_lines)
                target = chunk["target_content"]
                replacement = chunk["replacement_content"]
                allow_multiple = bool(chunk.get("allow_multiple", False))

                slice_text = "".join(all_lines[sl - 1 : el])
                count = slice_text.count(target)

                if count == 0:
                    return {
                        "error": (
                            f"Chunk {i} (lines {sl}\u2013{el}): target_content not found."
                        ),
                        "partial_results": results,
                    }
                if count > 1 and not allow_multiple:
                    return {
                        "error": (
                            f"Chunk {i} (lines {sl}\u2013{el}): found {count} occurrences "
                            "but allow_multiple is false."
                        ),
                        "partial_results": results,
                    }

                if allow_multiple:
                    new_slice = slice_text.replace(target, replacement)
                else:
                    new_slice = slice_text.replace(target, replacement, 1)

                # Replace the lines in the buffer
                new_slice_lines = _splitlines_keep(new_slice)
                all_lines = all_lines[: sl - 1] + new_slice_lines + all_lines[el:]
                total_lines = len(all_lines)

                results.append(
                    {
                        "chunk_index": i,
                        "range": {"start_line": sl, "end_line": el},
                        "replacements_made": count if allow_multiple else 1,
                    }
                )

            # --- Write back (atomic: temp file + os.replace) ---
            tmp_path = ""
            try:
                parent_dir = os.path.dirname(path) or "."
                tmp_fd, tmp_path = tempfile.mkstemp(dir=parent_dir, suffix=".tmp")
                os.close(tmp_fd)

                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                    await f.write("".join(all_lines))

                # Flush data to disk before the rename so a crash cannot leave
                # the destination referencing unwritten sectors.
                with open(tmp_path, "rb") as sync_f:
                    os.fsync(sync_f.fileno())

                _atomic_replace(tmp_path, path)
                tmp_path = ""
            except Exception as e:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                return {"error": f"Failed to write file: {e}"}

            logger.info(
                f"[multi_replace_file_content] applied {len(results)} chunk(s) to {path!r}"
            )
            return {
                "path": path,
                "chunks_applied": len(results),
                "results": results,
            }
        finally:
            lock.release()



def _splitlines_keep(text: str) -> List[str]:
    """Split text into lines while preserving line endings (like readlines())."""
    lines: List[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            lines.append(text[start : i + 1])
            start = i + 1
    if start < len(text):
        lines.append(text[start:])
    return lines
