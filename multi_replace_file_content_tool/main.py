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

import logging
import os
from typing import Any, Dict, List
import aiofiles
from openchadpy.tool_base import ToolBase

logger = logging.getLogger(__name__)


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
        if not os.path.isfile(path):
            return {"error": f"File not found: {path!r}"}

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
                        f"Chunk {i} (lines {sl}–{el}): target_content not found."
                    ),
                    "partial_results": results,
                }
            if count > 1 and not allow_multiple:
                return {
                    "error": (
                        f"Chunk {i} (lines {sl}–{el}): found {count} occurrences "
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

        # --- Write back ---
        try:
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                await f.write("".join(all_lines))
        except Exception as e:
            return {"error": f"Failed to write file: {e}"}

        logger.info(
            f"[multi_replace_file_content] applied {len(results)} chunk(s) to {path!r}"
        )
        return {
            "path": path,
            "chunks_applied": len(results),
            "results": results,
        }


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
