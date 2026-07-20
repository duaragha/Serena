"""Export conversations to markdown files."""

from pathlib import Path

from core.parser import Message, parse_full


def export_session(session: dict, messages: list[Message], output_dir: Path) -> Path:
    """Export a single session to markdown. Returns the file path."""
    project = session.get("project_dir", "unknown")
    date = (session.get("first_timestamp") or "unknown")[:10]
    short_id = session["session_id"][:8]
    first_msg = session.get("first_message", "chat")[:40]
    # Sanitize for filename
    slug = "".join(c if c.isalnum() or c in " -_" else "" for c in first_msg).strip()
    slug = slug.replace(" ", "-")[:40] or "chat"

    project_dir = output_dir / project
    project_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{date}_{short_id}_{slug}.md"
    filepath = project_dir / filename

    lines = []
    lines.append(f"# {first_msg}")
    lines.append("")
    lines.append(f"- **Date:** {date}")
    lines.append(f"- **Session:** `{session['session_id']}`")
    if session.get("cwd"):
        lines.append(f"- **Directory:** `{session['cwd']}`")
    if session.get("model"):
        lines.append(f"- **Model:** {session['model']}")
    if session.get("device"):
        lines.append(f"- **Device:** {session['device']}")
    if session.get("git_branch"):
        lines.append(f"- **Branch:** {session['git_branch']}")
    if session.get("tags"):
        lines.append(f"- **Tags:** {', '.join(session['tags'])}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for msg in messages:
        if msg.role == "user":
            lines.append("## You")
            lines.append("")
            lines.append(msg.text)
            lines.append("")

        elif msg.role == "assistant":
            if msg.tool_name:
                tool_line = f"> **Tool:** `{msg.tool_name}`"
                if msg.tool_input:
                    tool_line += f" — `{msg.tool_input[:120]}`"
                lines.append(tool_line)
                lines.append("")
            elif msg.text:
                lines.append("## Claude")
                lines.append("")
                lines.append(msg.text)
                lines.append("")

        elif msg.role == "tool_result":
            text = msg.text.strip()
            if text:
                lines.append(f"> ```")
                for tline in text[:300].split("\n"):
                    lines.append(f"> {tline}")
                lines.append(f"> ```")
                lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


def export_all(sessions: list[dict], output_dir: Path, progress_callback=None) -> list[Path]:
    """Export multiple sessions. Returns list of created file paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    for i, session in enumerate(sessions):
        if progress_callback:
            progress_callback(i + 1, len(sessions), session["session_id"][:8])

        file_path = Path(session["file_path"])
        if not file_path.exists():
            continue

        messages = parse_full(file_path)
        if not messages:
            continue

        path = export_session(session, messages, output_dir)
        paths.append(path)

    return paths
