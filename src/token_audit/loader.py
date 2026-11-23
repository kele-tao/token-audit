"""File loaders: plain text, markdown, tool schemas and chat exports.

Every loader returns a Document with a list of Segments.  Loaders never raise on
malformed content: they attach a note and return what they could parse, so a
scan over a directory never dies because one file is odd.
"""
from __future__ import annotations

import json
import pathlib
from typing import Iterable

from .models import Document, Kind, Segment

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".xml", ".html", ".csv", ".py", ".ts", ".js",
    ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".sh", ".ps1", ".sql",
}
SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "dist", "build", ".tox", ".idea", ".vscode",
}
MAX_FILE_BYTES = 8 * 1024 * 1024
FENCE = chr(96) * 3


def _decode(data: bytes) -> str:
    """Decode bytes as UTF-8, falling back to common code pages."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def split_markdown(text: str, source: str = "") -> list[Segment]:
    """Split markdown into blocks: headings, paragraphs, tables, lists, code.

    A block ends at a blank line, at a fence boundary or when the line type
    changes between prose, table and list.  Finer blocks make duplicate and
    near-duplicate detection meaningful, because a shared fragment is a block
    rather than a whole file.
    """
    segments: list[Segment] = []
    blocks: list[list[str]] = []
    buffer: list[str] = []
    start_line = 1
    in_fence = False

    def flush() -> None:
        nonlocal buffer
        if buffer:
            blocks.append(buffer)
        buffer = []

    for index, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            if not buffer:
                start_line = index
            buffer.append(line)
            in_fence = not in_fence
            if not in_fence:
                flush()
            continue
        if in_fence:
            buffer.append(line)
            continue
        if not stripped:
            flush()
            continue
        if not buffer:
            start_line = index
        buffer.append(line)
    flush()

    for block in blocks:
        body = "\n".join(block).strip("\n")
        if not body.strip():
            continue
        first = body.lstrip().splitlines()[0].strip()
        if first.startswith("|"):
            kind = Kind.TABLE
        elif first.startswith(("- ", "* ")) or first[:2].rstrip(".").isdigit() or first.lower().startswith(("example ", "input:", "output:")):
            kind = Kind.EXAMPLE
        elif first.lower().startswith(("you are", "system:", "system prompt")):
            kind = Kind.SYSTEM
        else:
            kind = Kind.MARKDOWN
        segments.append(Segment(text=body, kind=kind, line=start_line, source=source))
    # line numbers are approximate for merged blocks; recompute from the source
    cursor = 1
    for segment in segments:
        head = segment.text.splitlines()[0]
        found = text.find(head, 0)
        if found >= 0:
            cursor = text.count("\n", 0, found) + 1
        segments[segments.index(segment)] = Segment(
            text=segment.text, kind=segment.kind, line=cursor, source=source
        )
    return segments


def message_text(content) -> str:
    """Flatten an OpenAI/Anthropic message content field into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block_type == "tool_use":
                    parts.append(
                        json.dumps(block.get("input", {}), ensure_ascii=False, sort_keys=True)
                    )
                elif block_type == "tool_result":
                    parts.append(message_text(block.get("content", "")))
                elif block_type == "image":
                    parts.append("[image]")
                elif "content" in block:
                    parts.append(message_text(block["content"]))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return ""


def _role_kind(role: str) -> Kind:
    """Map a chat role to a segment kind."""
    role = (role or "").lower()
    if role in ("system", "developer"):
        return Kind.SYSTEM
    if role == "assistant":
        return Kind.ASSISTANT
    if role in ("tool", "function"):
        return Kind.TOOL
    return Kind.USER


def load_chat_jsonl(text: str, path: str) -> tuple[list[Segment], list[str]]:
    """Parse OpenAI or Anthropic JSONL chat exports."""
    segments: list[Segment] = []
    notes: list[str] = []
    bad_lines = 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if not isinstance(record, dict):
            bad_lines += 1
            continue
        messages = record.get("messages")
        if messages is None and isinstance(record.get("request"), dict):
            messages = record["request"].get("messages")
        if messages is None and isinstance(record.get("body"), dict):
            messages = record["body"].get("messages")
        system_value = record.get("system")
        if not messages and isinstance(record.get("prompt"), str):
            segments.append(
                Segment(
                    text=record["prompt"],
                    kind=Kind.USER,
                    label="prompt line %d" % number,
                    line=number,
                    source=path,
                )
            )
            continue
        if messages is None:
            if system_value:
                segments.append(
                    Segment(
                        text=message_text(system_value),
                        kind=Kind.SYSTEM,
                        label="system line %d" % number,
                        line=number,
                        source=path,
                    )
                )
            continue
        if system_value:
            segments.append(
                Segment(
                    text=message_text(system_value),
                    kind=Kind.SYSTEM,
                    label="system line %d" % number,
                    line=number,
                    source=path,
                )
            )
        for position, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "user"))
            body = message_text(message.get("content", ""))
            if not body:
                continue
            segments.append(
                Segment(
                    text=body,
                    kind=_role_kind(role),
                    label="%s #%d" % (role, position),
                    line=number,
                    source=path,
                )
            )
    if bad_lines:
        notes.append("%d malformed JSONL line(s) skipped" % bad_lines)
    if not segments:
        notes.append("no chat messages recognised in %s" % path)
    return segments, notes


def _is_schema(payload) -> bool:
    """True when a JSON payload looks like a tool/function schema."""
    if not isinstance(payload, dict):
        return False
    if set(payload) & {"parameters", "input_schema", "$schema", "properties"}:
        return True
    inner = payload.get("function")
    return isinstance(inner, dict) and bool(set(inner) & {"parameters", "properties"})


def load_json(text: str, path: str) -> tuple[list[Segment], list[str]]:
    """Parse a tool schema or a JSON payload into string segments."""
    notes: list[str] = []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        notes.append("invalid JSON (%s); treated as text" % exc.msg)
        return [Segment(text=text, kind=Kind.TEXT, source=path)], notes
    segments: list[Segment] = []
    described = (
        "description", "prompt", "system", "instruction", "template", "content",
        "message", "title", "summary",
    )

    def walk(node, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                label = trail + "." + str(key) if trail else str(key)
                if isinstance(value, str) and (lowered in described or len(value) > 60):
                    kind = Kind.SYSTEM if lowered in described else Kind.TEXT
                    segments.append(Segment(text=value, kind=kind, label=label))
                else:
                    walk(value, label)
        elif isinstance(node, list):
            for position, item in enumerate(node):
                walk(item, "%s[%d]" % (trail, position))

    walk(payload, "")
    if not segments:
        segments.append(
            Segment(
                text=json.dumps(payload, ensure_ascii=False, indent=2),
                kind=Kind.SCHEMA,
                source=path,
            )
        )
    if _is_schema(payload):
        notes.append("tool schema detected")
    return segments, notes


def load_file(path: pathlib.Path, root: pathlib.Path | None = None) -> Document | None:
    """Load one file into a Document, or return None when it is not text."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    relative = str(path.relative_to(root)) if root else str(path)
    if size > MAX_FILE_BYTES:
        return Document(
            path=relative,
            kind="skipped",
            size_bytes=size,
            notes=["file larger than %d bytes; skipped" % MAX_FILE_BYTES],
        )
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        return Document(
            path=relative,
            kind="unreadable",
            size_bytes=size,
            notes=["could not read file: %s" % exc],
        )
    text = _decode(data)
    notes: list[str] = []
    if suffix == ".jsonl":
        segments, notes = load_chat_jsonl(text, relative)
        kind = "chat-jsonl"
    elif suffix == ".json":
        segments, notes = load_json(text, relative)
        kind = "json"
    elif suffix in (".md", ".markdown"):
        segments = split_markdown(text, relative)
        kind = "markdown"
    else:
        segments = (
            [Segment(text=text, kind=Kind.TEXT, source=relative)] if text.strip() else []
        )
        kind = "text"
    return Document(
        path=relative,
        kind=kind,
        size_bytes=size,
        segments=segments,
        notes=notes,
        raw_text=text,
    )


def iter_source_files(target: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield candidate files under a file or directory, deterministically."""
    if target.is_file():
        yield target
        return
    stack = [target]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                stack.append(entry)
            else:
                yield entry
