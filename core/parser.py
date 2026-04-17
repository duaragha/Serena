"""Parse Claude Code session .jsonl files."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Message:
    role: str  # "user", "assistant", "system"
    text: str
    timestamp: datetime
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None


@dataclass
class SessionMeta:
    session_id: str
    project_dir: str
    cwd: str | None = None
    last_cwd: str | None = None
    device: str = "unknown"
    first_message: str = ""
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    message_count: int = 0
    model: str | None = None
    git_branch: str | None = None
    slug: str | None = None
    file_path: str = ""
    file_size: int = 0
    file_mtime: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0


def _extract_text(content) -> str:
    """Extract readable text from message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and text:
                        parts.append(f"[tool result] {text[:200]}")
        return "\n".join(parts)
    return ""


def _detect_device(project_dir: str, cwd: str | None = None) -> str:
    if project_dir.startswith("C--"):
        return "windows"
    if project_dir.startswith("-home-") or project_dir.startswith("-root-"):
        return "linux"
    if cwd:
        if cwd.startswith("C:") or cwd.startswith("D:"):
            return "windows"
        if cwd.startswith("/"):
            return "linux"
    return "unknown"


def _decode_project_path(project_dir: str, cwd: str | None = None) -> str:
    """Best-effort decode of directory name. Prefer cwd when available."""
    if cwd:
        return cwd
    if project_dir.startswith("C--"):
        # C--Users-ragha-Projects-foo -> C:\Users\ragha\Projects\foo
        return project_dir.replace("C--", "C:\\", 1).replace("-", "\\")
    if project_dir.startswith("-"):
        # -home-raghav-Projects-foo -> /home/raghav/Projects/foo
        return "/" + project_dir[1:].replace("-", "/")
    return project_dir


def parse_metadata(file_path: Path, project_dir: str) -> SessionMeta:
    """Quick scan: extract metadata from the first few relevant records."""
    session_id = file_path.stem
    meta = SessionMeta(
        session_id=session_id,
        project_dir=project_dir,
        file_path=str(file_path),
        file_size=file_path.stat().st_size,
        file_mtime=file_path.stat().st_mtime,
    )

    user_count = 0
    assistant_count = 0

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type")
                timestamp_str = record.get("timestamp")

                if rec_type == "user" and "message" in record:
                    msg = record["message"]
                    content = msg.get("content", "")
                    text = _extract_text(content)

                    # Skip tool results for first message detection
                    if isinstance(content, list):
                        is_tool_result = any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content
                        )
                        if is_tool_result:
                            continue

                    user_count += 1

                    if record.get("cwd"):
                        if not meta.cwd:
                            meta.cwd = record["cwd"]
                        meta.last_cwd = record["cwd"]
                    if not meta.git_branch and record.get("gitBranch"):
                        meta.git_branch = record["gitBranch"]
                    if not meta.first_message and text:
                        # Skip messages that are purely XML noise (commands, caveats)
                        import re as _re
                        cleaned = _re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=_re.DOTALL).strip()
                        if cleaned:
                            meta.first_message = text[:300]

                    if timestamp_str:
                        try:
                            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            if not meta.first_timestamp:
                                meta.first_timestamp = ts
                            meta.last_timestamp = ts
                        except ValueError:
                            pass

                elif rec_type == "assistant" and "message" in record:
                    assistant_count += 1
                    msg = record["message"]
                    if not meta.model and msg.get("model"):
                        meta.model = msg["model"]
                    if not meta.slug and record.get("slug"):
                        meta.slug = record["slug"]

                    usage = msg.get("usage", {})
                    if usage:
                        meta.input_tokens += usage.get("input_tokens", 0)
                        meta.output_tokens += usage.get("output_tokens", 0)
                        meta.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                        meta.cache_create_tokens += usage.get("cache_creation_input_tokens", 0)

                    if timestamp_str:
                        try:
                            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            meta.last_timestamp = ts
                        except ValueError:
                            pass

    except (OSError, PermissionError):
        pass

    meta.message_count = user_count + assistant_count
    meta.device = _detect_device(project_dir, meta.cwd)
    return meta


def parse_full(file_path: Path) -> list[Message]:
    """Full parse: extract all conversation turns for display."""
    messages = []
    # Track seen message IDs to deduplicate partial vs complete assistant responses
    seen_msg_ids: dict[str, int] = {}  # msg_id -> index in messages list

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type")
                if rec_type not in ("user", "assistant"):
                    continue

                msg = record.get("message", {})
                role = msg.get("role", rec_type)
                content = msg.get("content", "")
                timestamp_str = record.get("timestamp", "")

                try:
                    ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    ts = datetime.now()

                if role == "user":
                    # Check if it's a tool result
                    if isinstance(content, list):
                        is_tool_result = any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content
                        )
                        if is_tool_result:
                            # Extract tool output for context
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_result":
                                    output = block.get("content", "")
                                    if isinstance(output, str) and output.strip():
                                        messages.append(Message(
                                            role="tool_result",
                                            text=output[:500],
                                            timestamp=ts,
                                        ))
                            continue

                    text = _extract_text(content)
                    if text.strip():
                        messages.append(Message(role="user", text=text, timestamp=ts))

                elif role == "assistant":
                    msg_id = msg.get("id", "")
                    stop_reason = msg.get("stop_reason")

                    # Extract content
                    tool_name = None
                    tool_input_str = None
                    text_parts = []

                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text_parts.append(block["text"])
                            elif block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                inp = block.get("input", {})
                                # Show command for Bash, query for search tools, etc.
                                if isinstance(inp, dict):
                                    if "command" in inp:
                                        tool_input_str = inp["command"]
                                    elif "pattern" in inp:
                                        tool_input_str = inp["pattern"]
                                    elif "file_path" in inp:
                                        tool_input_str = inp["file_path"]
                                    elif "prompt" in inp:
                                        tool_input_str = inp["prompt"][:150]
                            # Skip thinking blocks entirely

                    text = "\n".join(text_parts)

                    if not text and not tool_name:
                        continue

                    # Deduplicate: if we've seen this msg_id, replace with the newer (more complete) version
                    if msg_id and msg_id in seen_msg_ids:
                        idx = seen_msg_ids[msg_id]
                        if text or tool_name:
                            messages[idx] = Message(
                                role="assistant",
                                text=text,
                                timestamp=ts,
                                tool_name=tool_name,
                                tool_input=tool_input_str,
                            )
                        continue

                    m = Message(
                        role="assistant",
                        text=text,
                        timestamp=ts,
                        tool_name=tool_name,
                        tool_input=tool_input_str,
                    )
                    if msg_id:
                        seen_msg_ids[msg_id] = len(messages)
                    messages.append(m)

    except (OSError, PermissionError):
        pass

    return messages


def parse_messages_for_search(file_path: Path) -> list[tuple[str, str, str]]:
    """Extract (role, text, timestamp) tuples for FTS indexing. Skips tool noise."""
    results = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type")
                if rec_type not in ("user", "assistant"):
                    continue

                msg = record.get("message", {})
                content = msg.get("content", "")
                ts = record.get("timestamp", "")

                if rec_type == "user":
                    if isinstance(content, list):
                        # Skip tool results
                        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                            continue
                    text = _extract_text(content)
                    if text.strip():
                        results.append(("user", text, ts))

                elif rec_type == "assistant":
                    if isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block["text"])
                        text = "\n".join(parts)
                        if text.strip():
                            results.append(("assistant", text, ts))

    except (OSError, PermissionError):
        pass

    return results
