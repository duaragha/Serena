"""Format sessions and messages for terminal display."""

from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from rich.panel import Panel
from rich.markdown import Markdown

from core.parser import Message


def _time_group(timestamp_str: str | None) -> str:
    """Assign a session to a time group based on its timestamp."""
    if not timestamp_str:
        return "Unknown"

    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now()
    except ValueError:
        return "Unknown"

    delta = now - ts
    if delta.days == 0:
        return "Today"
    elif delta.days == 1:
        return "Yesterday"
    elif delta.days <= 7:
        return "This Week"
    elif delta.days <= 14:
        return "Last Week"
    elif delta.days <= 30:
        return "This Month"
    elif delta.days <= 60:
        return "Last Month"
    else:
        return ts.strftime("%B %Y")


def _shorten_project(project: str) -> str:
    """Shorten project dir for display.

    Cross-platform: derives the user's home-dir slug at runtime so this works
    for any user on Linux/macOS/Windows.
    """
    from pathlib import Path

    home = str(Path.home()).replace("\\", "/")
    if home.lower().startswith("c:/"):
        home_slug = "C--" + home[3:].replace("/", "-")
    else:
        home_slug = "-" + home.lstrip("/").replace("/", "-")

    docs_proj = home_slug + "-Documents-Projects-"
    proj = home_slug + "-Projects-"
    if project.startswith(docs_proj):
        return project[len(docs_proj):]
    if project.startswith(proj):
        return project[len(proj):]
    if project.startswith(home_slug):
        rest = project[len(home_slug):]
        return "~" + rest if rest else "~"
    return project


def format_session_table(sessions: list[dict], console: Console):
    """Display sessions grouped by time, Claude.ai style."""
    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    # Group by time
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        group = _time_group(s.get("first_timestamp"))
        groups.setdefault(group, []).append(s)

    for group_name, group_sessions in groups.items():
        console.print(f"\n[bold]{group_name}[/bold]")
        console.print()

        for s in group_sessions:
            short_id = s["session_id"][:8]
            title = s.get("display_title", "Untitled chat")
            project = _shorten_project(s.get("project_dir", ""))
            device = s.get("device", "")
            starred = s.get("starred", 0)
            tags = s.get("tags", [])

            star = "[yellow]*[/yellow] " if starred else "  "
            device_badge = f"[green]{device}[/green]" if device == "linux" else f"[blue]{device}[/blue]"

            tag_str = ""
            if tags:
                tag_str = " " + " ".join(f"[magenta]#{t}[/magenta]" for t in tags)

            console.print(
                f"  {star}[dim]{short_id}[/dim]  "
                f"[bold]{escape(title)}[/bold]  "
                f"[dim]{escape(project)}[/dim]  "
                f"{device_badge}"
                f"{tag_str}"
            )

    console.print(f"\n[dim]{len(sessions)} conversations[/dim]")


def format_conversation(messages: list[Message], session: dict, console: Console):
    """Pretty-print a full conversation."""
    title = session.get("display_title") or session.get("custom_title") or session.get("title") or "Untitled"
    starred = "[yellow]*[/yellow] " if session.get("starred") else ""

    meta_lines = []
    if session.get("first_timestamp"):
        meta_lines.append(f"Date: {session['first_timestamp'][:16].replace('T', ' ')}")
    if session.get("cwd"):
        meta_lines.append(f"Directory: {session['cwd']}")
    if session.get("model"):
        meta_lines.append(f"Model: {session['model']}")
    if session.get("git_branch"):
        meta_lines.append(f"Branch: {session['git_branch']}")
    if session.get("device"):
        meta_lines.append(f"Device: {session['device']}")
    if session.get("tags"):
        meta_lines.append(f"Tags: {', '.join(session['tags'])}")

    console.print(Panel(
        "\n".join(meta_lines),
        title=f"{starred}[bold]{escape(title)}[/bold] [dim]({session['session_id'][:8]})[/dim]",
        border_style="cyan",
    ))
    console.print()

    for msg in messages:
        if msg.role == "user":
            console.print(Text("You", style="bold green"))
            console.print(Markdown(msg.text))
            console.print()

        elif msg.role == "assistant":
            if msg.tool_name:
                tool_display = f"  [dim]Tool: {msg.tool_name}[/dim]"
                if msg.tool_input:
                    tool_display += f"  [dim italic]{escape(msg.tool_input[:100])}[/dim italic]"
                console.print(tool_display)
            elif msg.text:
                console.print(Text("Claude", style="bold blue"))
                console.print(Markdown(msg.text))
                console.print()

        elif msg.role == "tool_result":
            text = msg.text.strip()
            if text:
                console.print(f"  [dim]{escape(text[:200])}[/dim]")


def format_search_results(results: list[dict], console: Console):
    """Display search results."""
    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    for r in results:
        short_id = r["session_id"][:8]
        snippet = r.get("snippet", "")
        snippet = snippet.replace(">>>", "[bold yellow]").replace("<<<", "[/bold yellow]")
        role = r.get("role", "")
        title = r.get("title", "")
        date = r.get("first_timestamp", "")[:10]
        starred = "[yellow]*[/yellow] " if r.get("starred") else ""

        console.print(f"  {starred}[dim]{short_id}[/dim] [dim]{date}[/dim]  [bold]{escape(title)}[/bold]")
        console.print(f"    [dim]{role}:[/dim] {snippet}")
        console.print()

    console.print(f"[dim]{len(results)} results[/dim]")


def format_projects(projects: list[dict], console: Console):
    """Display project list."""
    if not projects:
        console.print("[yellow]No projects found.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan", padding=(0, 1), expand=True)
    table.add_column("Project", ratio=1, no_wrap=True)
    table.add_column("Chats", width=6, justify="right")
    table.add_column("Latest", width=12, no_wrap=True)
    table.add_column("Dev", width=7, no_wrap=True)

    for p in projects:
        project = _shorten_project(p["project_dir"])
        count = str(p["chat_count"])
        latest = (p.get("latest") or "")[:10]
        device = p.get("device", "")
        device_style = "green" if device == "linux" else "blue" if device == "windows" else "dim"

        table.add_row(
            escape(project),
            count,
            latest,
            Text(device, style=device_style),
        )

    console.print(table)
