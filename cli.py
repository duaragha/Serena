"""CLI entry point for the chats tool."""

import click
from pathlib import Path
from rich.console import Console

console = Console()


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Browse, search, and organize your Claude Code conversations."""
    if ctx.invoked_subcommand is None:
        from ui.web import run_web
        run_web(host="0.0.0.0", port=8080, open_browser=True)


@main.command()
def tui():
    """Launch the terminal UI (legacy; slower than the web UI)."""
    from ui.tui import run
    run()


@main.command()
@click.option("--project", "-p", help="Filter by project name (substring match)")
@click.option("--device", "-d", type=click.Choice(["linux", "windows"]), help="Filter by device")
@click.option("--tag", "-t", help="Filter by tag")
@click.option("--starred", "-s", is_flag=True, help="Show only starred conversations")
@click.option("--limit", "-n", default=50, help="Max results (default 50)")
def list(project, device, tag, starred, limit):
    """List all conversations, grouped by time."""
    from core.indexer import update_index, list_sessions
    from chats.formatter import format_session_table

    with console.status("[cyan]Updating index..."):
        new, updated = update_index()

    if new or updated:
        console.print(f"[dim]Index updated: {new} new, {updated} changed[/dim]")

    sessions = list_sessions(
        project=project, device=device, tag=tag,
        starred_only=starred, limit=limit,
    )

    format_session_table(sessions, console)


@main.command()
@click.argument("query")
@click.option("--limit", "-n", default=20, help="Max results (default 20)")
def search(query, limit):
    """Full-text search across all conversations."""
    from core.indexer import update_index, search_fts, build_fts

    with console.status("[cyan]Updating index..."):
        update_index()

    results = search_fts(query, limit=limit)

    if not results:
        console.print("[yellow]Building search index (first time, may take a moment)...[/yellow]")
        with console.status("[cyan]Indexing messages for search..."):
            build_fts()
        results = search_fts(query, limit=limit)

    from chats.formatter import format_search_results
    format_search_results(results, console)


@main.command()
@click.argument("session_id")
def show(session_id):
    """Show a full conversation by ID (or partial ID)."""
    from core.indexer import update_index, get_session
    from core.parser import parse_full
    from chats.formatter import format_conversation

    with console.status("[cyan]Updating index..."):
        update_index()

    try:
        session = get_session(session_id)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    if not session:
        console.print(f"[red]No session found with ID '{session_id}'[/red]")
        return

    file_path = Path(session["file_path"])
    if not file_path.exists():
        console.print(f"[red]Session file not found: {file_path}[/red]")
        return

    with console.status("[cyan]Parsing conversation..."):
        messages = parse_full(file_path)

    format_conversation(messages, session, console)


@main.command()
@click.argument("session_id")
@click.argument("tag_name")
@click.option("--remove", "-r", is_flag=True, help="Remove the tag instead of adding it")
def tag(session_id, tag_name, remove):
    """Add or remove a tag on a session."""
    from core.indexer import update_index, add_tag, remove_tag

    with console.status("[cyan]Updating index..."):
        update_index()

    try:
        if remove:
            remove_tag(session_id, tag_name)
            console.print(f"[green]Removed tag '{tag_name}' from {session_id}[/green]")
        else:
            add_tag(session_id, tag_name)
            console.print(f"[green]Tagged {session_id} with '{tag_name}'[/green]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


@main.command()
@click.argument("session_id")
def star(session_id):
    """Toggle star/pin on a conversation."""
    from core.indexer import update_index, toggle_star

    with console.status("[cyan]Updating index..."):
        update_index()

    try:
        is_starred = toggle_star(session_id)
        if is_starred:
            console.print(f"[yellow]Starred {session_id}[/yellow]")
        else:
            console.print(f"[dim]Unstarred {session_id}[/dim]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


@main.command()
@click.argument("session_id")
@click.argument("title")
def rename(session_id, title):
    """Give a conversation a custom name."""
    from core.indexer import update_index, set_title

    with console.status("[cyan]Updating index..."):
        update_index()

    try:
        set_title(session_id, title)
        console.print(f"[green]Renamed {session_id} to '{title}'[/green]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


@main.command()
def projects():
    """List all projects with conversation counts."""
    from core.indexer import update_index, list_projects
    from chats.formatter import format_projects

    with console.status("[cyan]Updating index..."):
        update_index()

    project_list = list_projects()
    format_projects(project_list, console)


@main.command()
@click.option("--output", "-o", default="./chats-export", help="Output directory")
@click.option("--project", "-p", help="Filter by project")
@click.option("--tag", "-t", help="Filter by tag")
def export(output, project, tag):
    """Export conversations to markdown files."""
    from core.indexer import update_index, list_sessions
    from chats.exporter import export_all

    with console.status("[cyan]Updating index..."):
        update_index()

    sessions = list_sessions(project=project, tag=tag, limit=9999)

    if not sessions:
        console.print("[yellow]No sessions to export.[/yellow]")
        return

    output_dir = Path(output)

    def progress(current, total, sid):
        console.print(f"  [dim]Exporting {current}/{total} ({sid})...[/dim]", end="\r")

    paths = export_all(sessions, output_dir, progress_callback=progress)
    console.print(f"\n[green]Exported {len(paths)} conversations to {output_dir}[/green]")


@main.group(invoke_without_command=True)
@click.pass_context
def memory(ctx):
    """Manage persistent memories for Claude Code."""
    if ctx.invoked_subcommand is None:
        from memory.store import format_for_claude
        click.echo(format_for_claude())


main.add_command(memory)


MEMORY_TYPES = click.Choice(["user", "feedback", "project", "reference", "general"])


@memory.command("add")
@click.argument("content")
@click.option("--type", "-t", "mem_type", default="general", type=MEMORY_TYPES,
              help="Memory type (default: general)")
def memory_add(content, mem_type):
    """Add a new memory."""
    from memory.store import add_memory
    mid = add_memory(content, mem_type)
    console.print(f"[green]Memory #{mid} saved ({mem_type})[/green]")


@memory.command("remove")
@click.argument("memory_id", type=int)
def memory_remove(memory_id):
    """Remove a memory by ID."""
    from memory.store import delete_memory
    if delete_memory(memory_id):
        console.print(f"[green]Memory #{memory_id} deleted[/green]")
    else:
        console.print(f"[red]Memory #{memory_id} not found[/red]")


@memory.command("edit")
@click.argument("memory_id", type=int)
@click.argument("content")
@click.option("--type", "-t", "mem_type", default=None, type=MEMORY_TYPES,
              help="Change memory type")
def memory_edit(memory_id, content, mem_type):
    """Edit an existing memory."""
    from memory.store import get_memory, update_memory
    if not get_memory(memory_id):
        console.print(f"[red]Memory #{memory_id} not found[/red]")
        return
    update_memory(memory_id, content=content, mem_type=mem_type)
    console.print(f"[green]Memory #{memory_id} updated[/green]")


@memory.command("search")
@click.argument("query")
def memory_search(query):
    """Search memories by content."""
    from memory.store import search_memories
    results = search_memories(query)
    if not results:
        console.print("[yellow]No matching memories.[/yellow]")
        return
    for m in results:
        console.print(f"  [dim]#{m['id']}[/dim] [{m['type']}] {m['content']}")


@main.group(invoke_without_command=True)
@click.pass_context
def knowledge(ctx):
    """Browse and manage the knowledge base."""
    if ctx.invoked_subcommand is None:
        from knowledge.reader import list_topics, format_size
        topics = list_topics()
        if not topics:
            console.print("[yellow]No knowledge topics found.[/yellow]")
            return
        for t in topics:
            desc = t["description"][:80] + "..." if len(t["description"]) > 80 else t["description"]
            console.print(
                f"  [bold]{t['title']}[/bold] [dim]({t['slug']})[/dim]  "
                f"[dim]{t['file_count']} files, {format_size(t['total_size'])}[/dim]"
            )
            if desc:
                console.print(f"    [dim]{desc}[/dim]")


main.add_command(knowledge)


@knowledge.command("show")
@click.argument("slug")
def knowledge_show(slug):
    """Show all content for a knowledge topic."""
    from knowledge.reader import get_topic_content
    content = get_topic_content(slug)
    console.print(content)


@knowledge.command("delete")
@click.argument("slug")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def knowledge_delete(slug, yes):
    """Delete a knowledge topic and all its files."""
    from knowledge.reader import delete_topic, list_topics
    topics = {t["slug"]: t for t in list_topics()}
    if slug not in topics:
        console.print(f"[red]Topic '{slug}' not found.[/red]")
        return
    topic = topics[slug]
    if not yes:
        if not click.confirm(f"Delete '{topic['title']}' ({topic['file_count']} files)?"):
            return
    if delete_topic(slug):
        console.print(f"[green]Deleted '{topic['title']}'[/green]")
    else:
        console.print(f"[red]Failed to delete '{slug}'[/red]")


@knowledge.command("search")
@click.argument("query")
def knowledge_search(query):
    """Search across all knowledge files."""
    from core.indexer import search_knowledge_fts, build_knowledge_fts, update_knowledge_index

    update_knowledge_index()
    results = search_knowledge_fts(query, limit=20)
    if not results:
        console.print("[yellow]Building knowledge search index...[/yellow]")
        build_knowledge_fts()
        results = search_knowledge_fts(query, limit=20)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    for r in results:
        console.print(
            f"  [bold]{r['topic_title']}[/bold] / [dim]{r['filename']}[/dim]"
        )
        console.print(f"    {r['snippet']}\n")


@knowledge.command("link")
@click.argument("session_id")
@click.argument("topic_slug")
def knowledge_link(session_id, topic_slug):
    """Link a chat session to a knowledge topic."""
    from core.indexer import link_session_topic
    try:
        link_session_topic(session_id, topic_slug, "manual")
        console.print(f"[green]Linked {session_id[:8]} to '{topic_slug}'[/green]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")


@main.command()
@click.option("--all", "all_", is_flag=True, help="Retitle every session, including those with custom titles")
@click.option("--batch", "-b", default=8, help="Sessions per LLM call (default 8)")
@click.option("--limit", "-l", default=None, type=int, help="Stop after N sessions")
@click.option("--model", default="haiku", help="Claude model (default haiku)")
def retitle(all_, batch, limit, model):
    """Regenerate session titles with Claude (batched for speed)."""
    from pathlib import Path
    from core.indexer import list_sessions, set_title, _get_db
    from core.parser import parse_full
    from chats.llm_titles import generate_titles_batch

    # Pull everything (custom_title too) so we can filter precisely
    conn = _get_db()
    rows = conn.execute(
        "SELECT session_id, file_path, first_message, custom_title, title FROM sessions "
        "ORDER BY last_timestamp DESC"
    ).fetchall()
    conn.close()

    candidates = []
    for r in rows:
        if not all_ and r["custom_title"]:
            continue
        candidates.append(dict(r))

    if limit:
        candidates = candidates[:limit]

    if not candidates:
        console.print("[green]Nothing to retitle.[/green]")
        return

    console.print(f"[cyan]Retitling {len(candidates)} sessions via claude --model {model}, batch={batch}...[/cyan]")

    def first_assistant_text(file_path: str) -> str:
        try:
            msgs = parse_full(Path(file_path))
        except Exception:
            return ""
        for m in msgs:
            if m.role == "assistant" and m.text and not m.tool_name:
                return m.text
        return ""

    done = 0
    failed = 0
    total_batches = -(-len(candidates) // batch)
    for i in range(0, len(candidates), batch):
        chunk = candidates[i : i + batch]
        items = []
        for s in chunk:
            items.append({
                "id": s["session_id"][:8],
                "first_message": s.get("first_message") or "",
                "first_response": first_assistant_text(s["file_path"]),
            })
        bn = i // batch + 1
        console.print(f"  [dim]Batch {bn}/{total_batches} — {len(items)} sessions...[/dim]")
        titles = generate_titles_batch(items, model=model)
        if not titles:
            failed += len(chunk)
            continue
        for s in chunk:
            prefix = s["session_id"][:8]
            new_title = titles.get(prefix)
            if not new_title:
                failed += 1
                continue
            try:
                set_title(s["session_id"], new_title)
                done += 1
                console.print(f"    [green]{prefix}[/green] → {new_title}")
            except Exception as e:
                failed += 1
                console.print(f"    [red]{prefix} failed: {e}[/red]")

    console.print(f"\n[green]Done. Retitled {done}. Failed {failed}.[/green]")


@main.command()
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind (default 0.0.0.0)")
@click.option("--port", "-p", default=8080, help="Port (default 8080)")
def web(host, port):
    """Launch terminal-style web UI."""
    from ui.web import run_web
    run_web(host=host, port=port, open_browser=True)


@main.command()
def desktop():
    """Launch the desktop shell (native GTK on Linux, pywebview elsewhere)."""
    import sys as _sys
    if _sys.platform.startswith("linux"):
        from desktop.app_gtk import run
    else:
        from desktop.app import run
    run()


@main.command()
@click.option("--force", "-f", is_flag=True, help="Force full reindex")
def reindex(force):
    """Rebuild the session and knowledge index."""
    from core.indexer import drop_index, update_index, build_fts, update_knowledge_index, build_knowledge_fts

    if force:
        console.print("[yellow]Dropping existing index...[/yellow]")
        drop_index()

    def progress(current, total, sid):
        console.print(f"  [dim]Indexing {current}/{total} ({sid[:8]})...[/dim]", end="\r")

    new, updated = update_index(force=force, progress_callback=progress)
    console.print(f"\n[green]Sessions: {new} new, {updated} updated[/green]")

    console.print("[cyan]Building chat search index...[/cyan]")

    def fts_progress(current, total, sid):
        console.print(f"  [dim]FTS {current}/{total} ({sid})...[/dim]", end="\r")

    build_fts(progress_callback=fts_progress)
    console.print(f"\n[green]Chat search index ready.[/green]")

    console.print("[cyan]Indexing knowledge base...[/cyan]")

    def k_progress(current, total, slug):
        console.print(f"  [dim]Knowledge {current}/{total} ({slug})...[/dim]", end="\r")

    k_new, k_updated = update_knowledge_index(force=force, progress_callback=k_progress)
    console.print(f"\n[green]Knowledge: {k_new} new, {k_updated} updated[/green]")

    console.print("[cyan]Building knowledge search index...[/cyan]")
    build_knowledge_fts()
    console.print("[green]Knowledge search index ready.[/green]")


if __name__ == "__main__":
    main()
