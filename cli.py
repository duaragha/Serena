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
@click.option("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0 — reachable on your tailnet/LAN).")
@click.option("--port", default=8765, help="Port (default 8765).")
def serve(host, port):
    """Run the headless daemon so the Serena mobile app can connect over the network."""
    from core.chat_daemon import get_or_create_token
    from ui.web import run_web

    token = get_or_create_token()
    console.print(f"[bold]Serena daemon[/bold] → http://{host}:{port}")
    console.print(f"  WS endpoint : [cyan]ws://<this-host>:{port}/ws/chat[/cyan]")
    console.print(f"  Auth token  : [yellow]{token}[/yellow]")
    console.print(f"  In the app  : Settings → server URL [cyan]http://<tailnet-ip>:{port}/ws/chat[/cyan], paste token, mock off.")
    console.print("  (bind 0.0.0.0 is fine behind Tailscale; if you expose it any other way, keep the token secret.)")

    # Background re-index: new chats created on the host (PC) only show up after
    # the session index is rescanned. The desktop web UI polls ?refresh=1, but
    # the phone (/ws/chat) never triggers a rescan — so without this, new PC
    # chats never reach the phone. A periodic rescan surfaces them everywhere.
    import threading
    import time as _time

    def _reindex_loop():
        from core.indexer import update_index
        from core.chat_daemon import heal_sync_conflicts
        from core.autolink import auto_link_codex_chains
        while True:
            _time.sleep(20)
            try:
                # Heal Syncthing conflicts FIRST (union the loser's messages
                # back in) so the rescan indexes the merged, complete files.
                n = heal_sync_conflicts()
                if n:
                    print(f"[serve] healed {n} sync-conflict session(s)", flush=True)
                # Auto-link codex plan→exec chains into one thread group.
                try:
                    linked = auto_link_codex_chains()
                    if linked:
                        print(f"[serve] auto-linked {len(linked)} codex chain(s)", flush=True)
                except Exception as e:
                    print(f"[serve] autolink failed: {e}", flush=True)
                update_index()
            except Exception as e:
                print(f"[serve] reindex/heal failed: {e}", flush=True)

    threading.Thread(target=_reindex_loop, daemon=True).start()
    run_web(host=host, port=port, open_browser=False)


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
@click.argument("query")
@click.option("--limit", "-n", default=10, help="Max results (default 10)")
@click.option("--no-update", is_flag=True, help="Skip the disk rescan; trust the existing index")
def recall(query, limit, no_update):
    """Plain-text search across ALL chats (Claude + Codex).

    Matches against both message content (FTS5) AND chat titles, so chats
    you named in the Serena UI are findable by name. Designed for agents:
    outputs each match as a line block with sid prefix, date, agent, title,
    and a snippet. Title-only hits get a "[title]" tag so it's clear why
    they matched.
    """
    import sqlite3
    from core.indexer import update_index, search_fts, build_fts
    from core.config import DB_PATH
    from datetime import datetime

    if not no_update:
        update_index()

    # 1. FTS over message content
    fts_results = search_fts(query, limit=limit)
    if not fts_results:
        build_fts()
        fts_results = search_fts(query, limit=limit)

    seen: set[str] = set()
    merged: list[dict] = []
    for r in fts_results:
        sid = r.get("session_id")
        if sid and sid not in seen:
            seen.add(sid)
            r["_match_kind"] = "content"
            merged.append(r)

    # 2. Title match — case-insensitive LIKE on title + custom_title
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    pattern = f"%{query}%"
    rows = conn.execute("""
        SELECT session_id, title, custom_title, first_timestamp, agent, first_message
        FROM sessions
        WHERE title LIKE ? COLLATE NOCASE OR custom_title LIKE ? COLLATE NOCASE
        ORDER BY last_timestamp DESC
        LIMIT ?
    """, (pattern, pattern, limit)).fetchall()
    for row in rows:
        sid = row["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        first_msg = (row["first_message"] or "")[:160].replace("\n", " ")
        merged.append({
            "session_id": sid,
            "title": (row["custom_title"] or row["title"] or "Untitled"),
            "first_timestamp": row["first_timestamp"],
            "agent": row["agent"] or "claude",
            "snippet": first_msg,
            "_match_kind": "title",
        })
    conn.close()

    if not merged:
        click.echo(f"No matches for: {query}")
        return

    merged = merged[:limit]
    click.echo(f"# {len(merged)} match(es) for: {query}")
    click.echo("# Format: [date] [agent] sid8 — title  (kind)\n#         snippet\n# Run `chats show <sid>` for the full transcript.")
    click.echo("")
    for r in merged:
        ts = r.get("first_timestamp") or r.get("timestamp") or ""
        try:
            date = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            date = ts[:10] if ts else "????-??-??"
        agent = (r.get("agent") or "claude").lower()
        agent_tag = f"[{agent}]"
        sid8 = (r.get("session_id") or "")[:8]
        title = (r.get("title") or "Untitled").replace("\n", " ")
        snippet = (r.get("snippet") or "").replace("\n", " ")
        kind = f"({r.get('_match_kind', 'content')})"
        click.echo(f"[{date}] {agent_tag:<8} {sid8} — {title}  {kind}")
        click.echo(f"        {snippet}")
        click.echo("")


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


@memory.command("loops")
def memory_loops():
    """Print only open loops."""
    from memory.store import format_loops
    out = format_loops()
    if out:
        click.echo(out)


@memory.command("tasks")
def memory_tasks():
    """Print Raghav's open tasks (his deliberate todo list)."""
    from memory.store import format_tasks
    out = format_tasks()
    if out:
        click.echo(out)


@memory.command("active")
def memory_active():
    """Print tasks + open loops together (used by the per-turn hook)."""
    from memory.store import format_active
    out = format_active()
    if out:
        click.echo(out)


@memory.command("snooze")
@click.argument("memory_id", type=int)
@click.option("--days", "-d", default=7.0, help="How long to defer (default 7).")
def memory_snooze(memory_id, days):
    """Defer a task/loop: hide it from the nudge rail for N days."""
    from memory.store import snooze_memory
    if snooze_memory(memory_id, days):
        console.print(f"[green]#{memory_id} snoozed for {days:g} days[/green]")
    else:
        console.print(f"[red]#{memory_id} not found[/red]")


main.add_command(memory)


from memory.store import MEMORY_TYPES as _MEMORY_TYPE_NAMES
MEMORY_TYPES = click.Choice(_MEMORY_TYPE_NAMES)


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


@main.command(name="locket-sync")
def locket_sync():
    """Pull Serena's in-app Locket chats into the local index store."""
    from core.locket_scanner import sync_locket_chats, scan_locket_sessions, LOCKET_SYNC_ROOT
    from core.indexer import update_index, _get_db
    from core.parser import parse_messages_for_search

    n = sync_locket_chats()
    if n == 0:
        console.print("[yellow]No conversations synced (unconfigured, unreachable, or empty).[/yellow]")
        return
    console.print(f"[green]Synced {n} locket conversation(s) → {LOCKET_SYNC_ROOT}[/green]")
    new, updated = update_index()

    # Incremental FTS for just the locket sessions — recall only triggers a
    # full build_fts() when a query returns nothing, so without this the
    # synced chats stay invisible to search until some unrelated rebuild.
    conn = _get_db()
    fts_rows = 0
    for _, fp in scan_locket_sessions():
        sid = fp.stem
        conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (sid,))
        for role, text, ts in parse_messages_for_search(fp):
            conn.execute(
                "INSERT INTO messages_fts (content, session_id, role, timestamp) VALUES (?, ?, ?, ?)",
                (text, sid, role, ts),
            )
            fts_rows += 1
    conn.commit()
    conn.close()
    console.print(f"[green]Index: {new} new, {updated} updated · FTS: {fts_rows} locket messages[/green]")


@main.command()
@click.option("--force", "-f", is_flag=True, help="Force full reindex")
def reindex(force):
    """Rebuild the session and knowledge index."""
    from core.indexer import drop_index, update_index, build_fts, update_knowledge_index, build_knowledge_fts
    from core.locket_scanner import sync_locket_chats

    # Locket in-app chats ride the same index; refresh them first (fail-soft).
    try:
        synced = sync_locket_chats()
        if synced:
            console.print(f"[dim]Locket: synced {synced} conversation(s)[/dim]")
    except Exception:
        pass

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


@main.command(name="mark-done")
@click.argument("sid", required=False)
@click.option("--port", help="Serena Flask port. Auto-detected if omitted.")
def mark_done(sid, port):
    """Notify Serena that a chat finished a turn. Called from claude's Stop
    hook so the sidebar entry highlights. Reads `CLAUDE_CODE_SESSION_ID` env
    var if no sid is passed."""
    import os, json, urllib.request, socket
    sid = (sid or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        return  # silently no-op; the hook fires in many contexts where there's no sid
    p = port or _detect_serena_port()
    if not p:
        return
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{p}/api/chat-finished",
            data=json.dumps({"session_id": sid}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3).read()
    except (Exception,):
        pass  # best-effort; never block claude's Stop hook on this


@main.command(name="gen-image")
@click.option("--out", "-o", help="Output path (file or dir). If a dir, image gets a generated filename. Default: print where codex saved it.")
@click.option("--timeout", default=600, help="Max seconds to wait for image generation (default 600)")
@click.option("--reasoning", default="low", help="Codex reasoning effort: minimal/low/medium/high/xhigh (default low — orchestration only, doesn't affect gpt-image-2 quality)")
@click.argument("prompt", nargs=-1)
def gen_image(out, timeout, reasoning, prompt):
    """Generate an image via a fresh isolated codex `exec` session.

    Each call spawns a brand-new codex session that runs ONLY the imagegen
    skill, then exits. Avoids the linked-chat rollout bloat (codex stores
    each generated image as 2-4MB inline base64; running imagegen in a
    long-running chat eventually breaks the websocket with broken-pipe).

    Designed to be invoked by claude when Raghav asks for image generation
    — claude calls `chats gen-image "<prompt>"` instead of consulting the
    linked codex via ask-codex.

    Examples:
      chats gen-image "photorealistic mountain at sunset"
      chats gen-image -o ~/Pictures/hero.png "wide hero banner, dark blue gradient"
    """
    import os, subprocess, shutil, time, glob, re
    text = " ".join(prompt).strip()
    if not text:
        console.print("[red]prompt is required[/red]"); return

    codex_bin = shutil.which("codex") or "codex"
    gen_dir = os.path.expanduser("~/.codex/generated_images")
    pre_files = set(glob.glob(os.path.join(gen_dir, "**", "*"), recursive=True)) if os.path.isdir(gen_dir) else set()

    # Compose the prompt: $imagegen trigger + the actual ask. Codex sees
    # $imagegen and auto-loads the skill.
    full_prompt = f"$imagegen {text}\n\nAfter generating, print the saved file path on the last line as: SAVED:<path>"
    argv = [
        codex_bin, "exec",
        "--skip-git-repo-check",
        # gpt-5.4-mini drives orchestration; gpt-image-2 renders the image regardless.
        "-c", "model=\"gpt-5.4-mini\"",
        "-c", f"model_reasoning_effort=\"{reasoning}\"",
        # Strip MCP servers — gen-image needs none; they bloat every turn.
        # (approvals_reviewer already removed globally from ~/.codex/config.toml)
        "-c", "mcp_servers={}",
        full_prompt,
    ]

    console.print(f"[dim]Generating image (codex exec, fresh session)…[/dim]")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        console.print(f"[red]Timed out after {timeout}s.[/red]"); return
    if proc.returncode != 0:
        console.print(f"[red]codex exec failed (exit {proc.returncode}):[/red]")
        console.print(proc.stderr or proc.stdout); return

    # Try to extract the path from codex's stdout (we asked it to print SAVED:<path>)
    saved_path = None
    for line in proc.stdout.splitlines():
        m = re.match(r"^SAVED:(.+)$", line.strip())
        if m:
            saved_path = m.group(1).strip()
            break

    # Fallback: diff the generated_images dir
    if not saved_path:
        post_files = set(glob.glob(os.path.join(gen_dir, "**", "*"), recursive=True))
        new_files = [p for p in (post_files - pre_files) if os.path.isfile(p)]
        if new_files:
            new_files.sort(key=os.path.getmtime)
            saved_path = new_files[-1]

    if not saved_path or not os.path.exists(saved_path):
        console.print("[yellow]Image generated but couldn't locate the file. Codex output:[/yellow]")
        console.print(proc.stdout[-1500:] if len(proc.stdout) > 1500 else proc.stdout)
        return

    # Move/copy to --out if requested
    final = saved_path
    if out:
        out_path = os.path.expanduser(out)
        if os.path.isdir(out_path):
            out_path = os.path.join(out_path, os.path.basename(saved_path))
        try:
            shutil.move(saved_path, out_path)
            final = out_path
        except OSError as e:
            console.print(f"[yellow]Generated at {saved_path} (couldn't move to {out_path}: {e})[/yellow]")
            return

    console.print(f"[green]Saved:[/green] {final}")


@main.command(name="ask-claude")
@click.option("--sid", help="Target claude session id. If omitted, auto-detect linked claude sibling of the current codex chat.")
@click.option("--from-sid", help="Your codex session id (auto-detected from env/proc if omitted)")
@click.option("--timeout", default=300, help="Max seconds to wait for claude's response (default 300)")
@click.option("--port", help="Serena Flask port. Auto-detected if omitted.")
@click.argument("prompt", nargs=-1)
def ask_claude(sid, from_sid, timeout, port, prompt):
    """Send a prompt into a running claude VTE in Serena and return the
    response. The mirror of `chats ask-codex` — used by codex (or anyone with
    a session sid) to consult its linked claude partner.

    Examples:
      chats ask-claude "thoughts on this approach?"          # auto-finds linked claude
      chats ask-claude --sid 572aa6c9 "thoughts on this?"    # explicit target
    """
    import json, urllib.request, urllib.error, socket
    text = " ".join(prompt).strip()
    if not text:
        console.print("[red]prompt is required[/red]"); return

    p = port or _detect_serena_port()
    if not p:
        console.print("[red]Could not find a running Serena instance on localhost.[/red]"); return

    target_sid = sid
    if not target_sid:
        my_sid = from_sid or _detect_codex_sid()
        if not my_sid:
            console.print("[red]Could not detect current codex sid; pass --sid or --from-sid.[/red]"); return
        target_sid = _find_linked_sibling(my_sid, target_agent="claude")
        if not target_sid:
            console.print(f"[red]Codex {my_sid[:8]} has no linked claude sibling. Link one in Serena first.[/red]"); return

    body = {"target_sid": target_sid, "prompt": text, "timeout": timeout}
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{p}/api/claude-bridge",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout) as e:
        console.print(f"[red]Bridge call failed: {e}[/red]"); return

    if not payload.get("ok"):
        console.print(f"[red]{payload.get('message', 'unknown error')}[/red]")
        if payload.get("response"):
            console.print(payload["response"])
        return
    console.print(payload.get("response") or "(no response)")


@main.command(name="ask-codex")
@click.option("--sid", help="Target codex session id (8+ char prefix). If omitted, auto-detect the linked codex sibling of the current claude chat.")
@click.option("--from-sid", help="Your claude session id (auto-detected from process tree if omitted)")
@click.option("--timeout", default=300, help="Max seconds to wait for codex's response (default 300)")
@click.option("--port", help="Serena Flask port. Auto-detected if omitted.")
@click.argument("prompt", nargs=-1)
def ask_codex(sid, from_sid, timeout, port, prompt):
    """Send a prompt into a running codex VTE in Serena's split view and
    return the response. Designed for claude (or any agent) to consult its
    linked codex partner without spawning a fresh codex MCP session.

    Examples:
      chats ask-codex --sid 019ddecb "what does this file do?"
      chats ask-codex "your sid is auto-detected"   # finds linked codex
    """
    import os, json, urllib.request, urllib.error, socket
    from pathlib import Path

    text = " ".join(prompt).strip()
    if not text:
        console.print("[red]prompt is required[/red]"); return

    # Find Serena's port (it picks one at random per launch)
    p = port or _detect_serena_port()
    if not p:
        console.print("[red]Could not find a running Serena instance on localhost.[/red]"); return

    # Two paths:
    #   --sid explicitly provided → straight to /api/codex-bridge (caller knows
    #       which codex to drive; no spawn logic)
    #   --sid omitted → /api/ask-linked-codex which handles both already-linked
    #       and auto-spawn cases atomically (avoids the deadlock where codex
    #       doesn't write a session file until it gets first input)
    if sid:
        endpoint = "/api/codex-bridge"
        body = {"target_sid": sid, "prompt": text, "timeout": timeout}
    else:
        my_sid = from_sid or _detect_claude_sid()
        if not my_sid:
            console.print("[red]Could not detect current claude sid; pass --sid or --from-sid.[/red]"); return
        endpoint = "/api/ask-linked-codex"
        body = {"claude_sid": my_sid, "prompt": text, "timeout": timeout}

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{p}{endpoint}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout) as e:
        console.print(f"[red]Bridge call failed: {e}[/red]"); return

    if payload.get("spawned"):
        console.print(f"[dim]Auto-spawned linked codex {payload.get('codex_sid', '')[:8]}.[/dim]")
    if not payload.get("ok"):
        console.print(f"[red]{payload.get('message', 'unknown error')}[/red]")
        if payload.get("response"):
            console.print(payload["response"])
        return
    console.print(payload.get("response") or "(no response)")


def _detect_claude_sid() -> str | None:
    """Find the claude session id of the chat we're running inside.

    Three fallbacks (cheapest first):
      0. `CLAUDE_CODE_SESSION_ID` env var — claude exports this into every
         subshell. Works for new chats AND resumed chats.
      1. Walk the process tree looking for `claude -r <sid>` argv (works
         when Serena resumed an existing chat).
      2. For brand-new chats Serena spawns claude WITHOUT `-r` — the session
         id only exists once claude opens its own JSONL file. Find the
         claude binary in our ancestor chain, then read /proc/<pid>/fd/
         for an open file under ~/.claude/projects/<slug>/<UUID>.jsonl;
         the filename UUID is the session id.
    """
    import os, re, glob, sys
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if env_sid and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", env_sid):
        return env_sid

    # /proc-based fallbacks only work on Linux. Windows has no /proc — we'd
    # crash or hang trying to read `/proc/<pid>/cmdline`. On Windows we rely
    # entirely on the CLAUDE_CODE_SESSION_ID env var above; if that's not
    # set, give up gracefully.
    if sys.platform != "linux":
        return None

    UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    JSONL_RE = re.compile(r"/\.claude/projects/[^/]+/(" + UUID_RE.pattern + r")\.jsonl$")

    pid = os.getppid()
    claude_pids: list[int] = []
    for _ in range(8):
        if pid <= 1:
            break
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\x00")
        except OSError:
            break
        argv_s = [a.decode("utf-8", errors="replace") for a in argv if a]
        # Try argv first (works for resumed sessions)
        for i, a in enumerate(argv_s):
            if a == "-r" and i + 1 < len(argv_s):
                cand = argv_s[i + 1]
                if UUID_RE.fullmatch(cand) or len(cand) >= 8:
                    return cand
            if a.startswith("--resume="):
                return a.split("=", 1)[1]
        # Track which ancestors look like the claude binary so we can poke
        # at their open fds as a fallback.
        joined = " ".join(argv_s)
        if "/claude" in joined and "claude" in os.path.basename(argv_s[0] if argv_s else ""):
            claude_pids.append(pid)
        try:
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        pid = int(line.split()[1])
                        break
                else:
                    break
        except OSError:
            break

    # Fallback: read the claude process's open fds, pick the .jsonl under
    # ~/.claude/projects/ — that's the session file claude is writing to.
    for cpid in claude_pids:
        try:
            for fd in os.listdir(f"/proc/{cpid}/fd"):
                try:
                    target = os.readlink(f"/proc/{cpid}/fd/{fd}")
                except OSError:
                    continue
                m = JSONL_RE.search(target)
                if m:
                    return m.group(1)
        except OSError:
            continue
    return None


def _spawn_linked_codex(claude_sid: str, serena_port: int, timeout: int = 30) -> str | None:
    """Ask Serena to spawn a codex VTE in split view next to claude_sid and
    link them. Blocks until codex is up + linked, returns the new codex sid."""
    import json, urllib.request, urllib.error, socket
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{serena_port}/api/spawn-linked-codex",
            data=json.dumps({"claude_sid": claude_sid, "timeout": timeout}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout) as e:
        console.print(f"[red]Spawn call failed: {e}[/red]")
        return None
    if not payload.get("ok"):
        console.print(f"[red]{payload.get('message', 'spawn failed')}[/red]")
        return None
    return payload.get("codex_sid")


def _find_linked_codex(claude_sid: str) -> str | None:
    return _find_linked_sibling(claude_sid, target_agent="codex")


def _find_linked_sibling(my_sid: str, target_agent: str) -> str | None:
    """Look up the linked sibling for a given session whose agent is `target_agent`.
    Agent-agnostic helper used by both ask-codex (claude → codex) and ask-claude
    (codex → claude)."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from core import metadata as meta_sync
    except ImportError:
        return None
    gid = meta_sync.get_group(my_sid)
    if not gid:
        return None
    members = meta_sync.list_group_members(gid)
    try:
        from core.indexer import get_session
    except ImportError:
        return None
    target_agent = target_agent.lower()
    for m in members:
        if m == my_sid:
            continue
        s = get_session(m)
        if s and (s.get("agent") or "").lower() == target_agent:
            return m
    return None


def _detect_codex_sid() -> str | None:
    """Find the codex session id of the codex chat we're running inside.
    Mirror of _detect_claude_sid but for codex. Tries env vars first, then
    walks /proc looking for a `codex resume <sid>` ancestor."""
    import os, re, sys
    for var in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_COMPANION_SESSION_ID"):
        v = os.environ.get(var) or ""
        if v and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v):
            return v
    if sys.platform != "linux":
        return None
    UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    pid = os.getppid()
    for _ in range(10):
        if pid <= 1:
            break
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\x00")
        except OSError:
            break
        argv_s = [a.decode("utf-8", errors="replace") for a in argv if a]
        # Match `codex resume <UUID>` or `codex resume --last`
        for i, a in enumerate(argv_s):
            if a.endswith("/codex") and i + 2 < len(argv_s) and argv_s[i + 1] == "resume":
                cand = argv_s[i + 2]
                if UUID_RE.fullmatch(cand):
                    return cand
        try:
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        pid = int(line.split()[1])
                        break
                else:
                    break
        except OSError:
            break
    return None


def _detect_serena_port() -> int | None:
    """Find a running Serena Flask instance on localhost. Returns the
    NEWEST Serena's port (so post-relaunch wins over a stale older one).

    Cross-platform: uses `ss` on Linux + `netstat`/`Get-NetTCPConnection` on
    Windows. Falls back to a port-probe of common ranges if neither works.
    """
    import os, re, subprocess, sys, time
    candidates: list[tuple[float, int]] = []  # (start_time, port)

    if sys.platform == "linux":
        try:
            out = subprocess.check_output(["ss", "-tlnp"], text=True, timeout=2)
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return None
        port_re = re.compile(r"127\.0\.0\.1:(\d+)")
        pid_re = re.compile(r"pid=(\d+)")
        for line in out.splitlines():
            port_m = port_re.search(line)
            if not port_m:
                continue
            port = int(port_m.group(1))
            for pid_m in pid_re.finditer(line):
                pid = int(pid_m.group(1))
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        cmdline = fh.read().decode("utf-8", errors="replace")
                except OSError:
                    continue
                if "chats" in cmdline and ("desktop" in cmdline or "ui.web" in cmdline or "serena" in cmdline.lower()):
                    try:
                        start = os.stat(f"/proc/{pid}").st_mtime
                    except OSError:
                        start = 0.0
                    candidates.append((start, port))
                    break
    elif sys.platform == "win32":
        # Use netstat -ano to map ports to PIDs, then look up cmdline via wmic
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"], text=True, timeout=4
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return None
        # Lines like: "  TCP    127.0.0.1:50937    0.0.0.0:0    LISTENING    25224"
        row_re = re.compile(r"\s+TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)")
        seen_pids: dict[int, str] = {}
        for line in out.splitlines():
            m = row_re.search(line)
            if not m:
                continue
            port = int(m.group(1))
            pid = int(m.group(2))
            cmdline = seen_pids.get(pid)
            if cmdline is None:
                try:
                    cmd_out = subprocess.check_output(
                        ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/format:list"],
                        text=True, timeout=3, stderr=subprocess.DEVNULL,
                    )
                    cmdline = cmd_out
                except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
                    cmdline = ""
                seen_pids[pid] = cmdline
            if "cli.py" in cmdline and "desktop" in cmdline:
                # Use the PID itself as a (rough) age proxy — higher PID
                # likely newer in Windows's monotonic-ish PID assignment.
                candidates.append((float(pid), port))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]  # newest wins


if __name__ == "__main__":
    main()
