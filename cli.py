"""CLI entry point for the chats tool."""

from __future__ import annotations

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
        # Phone-Serena's observations (Locket nightly pass) — fail-soft so
        # an unreachable Locket never breaks the session digest.
        try:
            from memory.locket_mirror import fetch_observations
            obs = fetch_observations(limit=8)
            if obs:
                click.echo("\n## What phone-me noticed (locket observations)")
                for o in obs:
                    click.echo(f"- {o}")
        except Exception:
            pass


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
    result = snooze_memory(memory_id, days)
    if isinstance(result, str):
        console.print(f"[yellow]Snooze proposed, not applied: {result}[/yellow]")
    elif result:
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
    result = add_memory(content, mem_type)
    if isinstance(result, str):
        console.print(f"[yellow]Memory proposed, not applied: {result}[/yellow]")
    else:
        console.print(f"[green]Memory #{result} saved ({mem_type})[/green]")


@memory.command("sync")
@click.option("--dry-run", is_flag=True, help="Show memory changes without writing local files")
@click.option(
    "--prune-stale-laptop",
    is_flag=True,
    help="Archive remote source=laptop rows that no longer exist locally",
)
@click.option(
    "--type",
    "type_filter",
    type=click.Choice(["task", "loop", "feedback", "user", "project", "reference", "general"]),
    help="Only push this local memory type to Locket",
)
def memory_sync(dry_run, prune_stale_laptop, type_filter):
    """Sync memory/task changes between local files and Locket."""
    from memory.locket_mirror import pull, push_local
    from core.locket_sync_state import last_success
    r = pull(dry_run=dry_run)
    if not r.get("ok"):
        console.print("[yellow]Could not reach Locket memory API.[/yellow]")
        console.print(f"[dim]Last successful memory sync: {last_success('memory')}[/dim]")
        return
    total = r["created"] + r["updated"] + r["deleted"]
    if total == 0:
        label = "would change" if dry_run else "changed"
        console.print(f"[dim]Memory in sync with Locket — 0 files {label}.[/dim]")
    else:
        verb = "Would sync" if dry_run else "Synced"
        console.print(
            f"[green]{verb} from Locket to laptop: +{r['created']} new, "
            f"{r['updated']} updated, {r['deleted']} removed[/green]"
        )
    pushed = push_local(
        prune_stale_laptop=prune_stale_laptop,
        dry_run=dry_run,
        type_filter=type_filter,
    )
    if not pushed.get("ok"):
        console.print("[yellow]Could not push laptop memories to Locket.[/yellow]")
    else:
        verb = "Would push" if dry_run else "Pushed"
        console.print(
            f"[green]{verb} laptop to Locket: +{pushed['pushed']} new, "
            f"{pushed['linked']} linked, {pushed['pruned']} pruned[/green]"
        )
    if not dry_run:
        console.print(f"[dim]Last successful memory sync: {last_success('memory')}[/dim]")


@memory.command("remove")
@click.argument("memory_id", type=int)
def memory_remove(memory_id):
    """Remove a memory by ID."""
    from memory.store import delete_memory
    result = delete_memory(memory_id)
    if isinstance(result, str):
        console.print(f"[yellow]Forgetting proposed, not applied: {result}[/yellow]")
    elif result:
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
    result = update_memory(memory_id, content=content, mem_type=mem_type)
    if isinstance(result, str):
        console.print(f"[yellow]Edit proposed, not applied: {result}[/yellow]")
    else:
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


@memory.command("ledger")
@click.argument("key")
@click.option("--goal", help="What we're trying to do.")
@click.option("--facts", help="What we actually know, verified.")
@click.option("--decision", help="What got decided.")
@click.option("--promise", help="What either agent committed to.")
@click.option("--risk", help="The one thing worth watching.")
@click.option("--next", "next_action", help="The next concrete move.")
def memory_ledger(key, goal, facts, decision, promise, risk, next_action):
    """Create or update the ledger card for KEY (a short stable slug, e.g.
    'persona-tuning'). Only options you pass get changed — the rest keep
    their current value. Run with just KEY and no options to print current
    state without changing anything."""
    from memory.store import find_ledger, upsert_ledger, LEDGER_FIELDS
    passed = {
        "goal": goal, "facts": facts, "decision": decision,
        "promise": promise, "risk": risk, "next_action": next_action,
    }
    passed = {k: v for k, v in passed.items() if v is not None}
    if passed:
        mid = upsert_ledger(key, **passed)
        console.print(f"[green]Ledger '{key}' updated (#{mid})[/green]")
    m = find_ledger(key)
    if not m:
        console.print(f"[yellow]No ledger for '{key}' yet. Pass --goal etc. to create one.[/yellow]")
        return
    for f in LEDGER_FIELDS:
        v = (m.get(f) or "").strip()
        if v:
            console.print(f"  [dim]{f}:[/dim] {v}")


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


@click.group()
def coding():
    """Inspect and steer Serena's private coding jobs."""


main.add_command(coding)


def _coding_origin() -> dict:
    """A terminal control has its own auditable authority protocol."""
    return {"text": "coding job control from the local terminal", "protocol": "cli"}


def _coding_rows(limit: int):
    from core.voice_inbox import get_default_voice_inbox

    return get_default_voice_inbox().recent_jobs(limit=limit)


@coding.command("list")
@click.option("--limit", "-n", default=10, help="How many recent jobs to show (default 10).")
def coding_list(limit):
    """List recent coding jobs, newest first."""
    jobs = _coding_rows(limit)
    if not jobs:
        console.print("[dim]no coding jobs on record[/dim]")
        return
    for job in jobs:
        state = job["state"] or "queued"
        colour = {
            "working": "cyan",
            "completed": "green",
            "failed": "red",
            "cancelled": "yellow",
        }.get(state, "white")
        console.print(
            f"[{colour}]{job['item_id'][:8]}[/{colour}] "
            f"{state:<13} {job['project'] or '?':<18} {job['request'][:70]}"
        )


@coding.command("status")
@click.argument("reference", required=False, default="")
@click.option("--json", "as_json", is_flag=True, help="Print the full durable snapshot as JSON.")
def coding_status(reference, as_json):
    """Show one job's state, changes, tests, evidence, and review."""
    from core.coding_job_controls import job_status

    result = job_status(reference)
    if not result.ok:
        console.print(f"[red]{result.reason}[/red]")
        raise SystemExit(1)
    if as_json:
        import json as _json

        console.print_json(_json.dumps(result.job))
        return
    console.print(result.spoken)


@coding.command("cancel")
@click.argument("reference", required=False, default="")
def coding_cancel(reference):
    """Stop a running coding job."""
    _run_coding_control("cancel", reference, "")


@coding.command("steer")
@click.argument("text")
@click.option("--job", "reference", default="", help="Job id prefix or project name.")
def coding_steer(reference, text):
    """Hand a correction to a running job's persisted Codex session."""
    _run_coding_control("steer", reference, text)


@coding.command("resume")
@click.argument("reference", required=False, default="")
def coding_resume(reference):
    """Pick a stopped job back up in its persisted Codex session."""
    _run_coding_control("resume", reference, "")


def _run_coding_control(action: str, reference: str, text: str) -> None:
    from core.coding_job_controls import control_job

    result = control_job(action, reference=reference, text=text, origin=_coding_origin())
    if not result.ok:
        console.print(f"[red]{result.reason}[/red]")
        raise SystemExit(1)
    console.print(f"[green]{result.spoken}[/green]")


@main.command()
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind (default 0.0.0.0)")
@click.option("--port", "-p", default=8080, help="Port (default 8080)")
def web(host, port):
    """Launch terminal-style web UI."""
    from ui.web import run_web
    run_web(host=host, port=port, open_browser=True)


@main.command()
def desktop():
    """Launch the legacy desktop shell (native GTK on Linux, pywebview elsewhere)."""
    import os as _os
    import sys as _sys
    from pathlib import Path as _Path
    legacy_dir = _Path(__file__).resolve().parent / "archive" / "desktop-gtk-legacy"
    if legacy_dir.is_dir() and str(legacy_dir) not in _sys.path:
        _sys.path.insert(0, str(legacy_dir))
    _os.environ.setdefault("SERENA_CALL_RUNTIME", "lazy")
    if _sys.platform.startswith("linux"):
        from desktop.app_gtk import run
    else:
        from desktop.app import run
    run()


@main.command(name="dev")
@click.option(
    "--no-hot-reload",
    is_flag=True,
    help="Serve the imported page instead of re-reading it from disk.",
)
def dev(no_hot_reload):
    """Run the Electron app from source, with live UI reload.

    The packaged AppImage bundles a frozen copy of the interface, so editing
    ui/web.py cannot change what it shows. This runs the same Electron shell
    unpackaged against this checkout: the page is re-read from disk per
    request and the window refreshes itself moments after a save, without
    restarting the backend, so terminals and live agent sessions survive.
    """
    import os as _os
    import shutil as _shutil
    import subprocess as _subprocess
    from pathlib import Path as _Path

    app_dir = _Path(__file__).resolve().parent / "desktop-electron"
    if not (app_dir / "package.json").is_file():
        raise SystemExit(f"the Electron app is missing at {app_dir}")
    npm = _shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is required to run the desktop app from source")
    if not (app_dir / "node_modules").is_dir():
        click.echo("installing Electron dependencies (first run only)…")
        _subprocess.run([npm, "install"], cwd=str(app_dir), check=True)

    env = dict(_os.environ)
    env["SERENA_UI_HOTRELOAD"] = "0" if no_hot_reload else "1"
    env.setdefault("SERENA_CALL_RUNTIME", "lazy")
    if no_hot_reload:
        click.echo("running from source (hot reload off)")
    else:
        click.echo("running from source — save ui/web.py and the window reloads itself")
    raise SystemExit(
        _subprocess.run([npm, "run", "dev"], cwd=str(app_dir), env=env).returncode
    )


@main.command(name="text")
@click.argument("message", nargs=-1, required=True)
def text(message):
    """Text Raghav's phone via the Serena telegram bot (proactive ping).

    Usage: chats text "build's done, come look"
    Credentials: ~/.config/serena/telegram.env (TELEGRAM_BOT_TOKEN/CHAT_ID).
    Note: replies go to Locket's webhook brain (phone Serena), not back
    to the session that sent the text.
    """
    import json
    import urllib.request
    from pathlib import Path

    env_path = Path.home() / ".config" / "serena" / "telegram.env"
    if not env_path.exists():
        console.print("[red]no telegram.env — see serena memory 'notification rail'[/red]")
        raise SystemExit(1)
    creds = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    token = creds.get("TELEGRAM_BOT_TOKEN")
    chat_id = creds.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        console.print("[red]telegram.env missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID[/red]")
        raise SystemExit(1)

    body = json.dumps({"chat_id": chat_id, "text": " ".join(message)}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        ok = json.load(resp).get("ok")
    console.print("[green]sent[/green]" if ok else "[red]telegram rejected the send[/red]")
    if not ok:
        raise SystemExit(1)


@main.command(name="locket-sync")
@click.option("--dry-run", is_flag=True, help="Show chat sync counts without writing files")
def locket_sync(dry_run):
    """Pull Serena's in-app Locket chats into the local index store."""
    from core.locket_scanner import sync_locket_chats, scan_locket_sessions, LOCKET_SYNC_ROOT
    from core.indexer import update_index, _get_db
    from core.parser import parse_messages_for_search
    from core.locket_sync_state import last_success

    details = sync_locket_chats(dry_run=dry_run, return_details=True)
    if not details.get("ok"):
        console.print("[yellow]No conversations synced (unconfigured, unreachable, or empty).[/yellow]")
        console.print(f"[dim]Last successful Locket chat sync: {last_success('chats')}[/dim]")
        return
    n = details["conversations"]
    if n == 0:
        console.print("[dim]Locket chat sync reached the API; no conversations returned.[/dim]")
        return
    verb = "Would sync" if dry_run else "Synced"
    console.print(
        f"[green]{verb} {n} locket conversation(s) → {LOCKET_SYNC_ROOT}[/green]"
    )
    console.print(
        f"[dim]Chats: +{details['created']} new, {details['updated']} updated, "
        f"{details['deleted']} deleted, {details['unchanged']} unchanged[/dim]"
    )
    if dry_run:
        return
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
    console.print(f"[dim]Last successful Locket chat sync: {last_success('chats')}[/dim]")


@main.command(name="locket-status")
def locket_status():
    """Show last successful Locket memory/chat sync details."""
    from core.locket_sync_state import load_state

    state = load_state()
    for key, label in (("memory", "Memory"), ("chats", "Chats"), ("archive", "Recall archive")):
        entry = state.get(key) or {}
        counts = entry.get("counts") or {}
        console.print(f"[bold]{label}[/bold]: {entry.get('last_success_at') or 'never'}")
        if counts:
            console.print(
                "  "
                + ", ".join(f"{k}={v}" for k, v in counts.items() if k not in {"ok", "dry_run"})
            )


@main.command(name="archive-sync")
@click.option("--dry-run", is_flag=True, help="Show recall archive changes without uploading")
@click.option("--force", is_flag=True, help="Upload every chat and knowledge file again")
def archive_sync(dry_run, force):
    """Push Claude/Codex chats and knowledge to Locket for offline recall."""
    from core.locket_archive_sync import sync_locket_archive

    result = sync_locket_archive(dry_run=dry_run, force=force)
    if not result.get("ok") and not dry_run:
        console.print("[yellow]Recall archive sync is unconfigured or failed.[/yellow]")
    verb = "Would sync" if dry_run else "Synced"
    console.print(
        f"[green]{verb} {result.get('sessions_changed', 0)} chat(s), "
        f"{result.get('knowledge_changed', 0)} knowledge file(s)[/green]"
    )
    console.print(
        f"[dim]Deletes: {result.get('sessions_deleted', 0)} chats, "
        f"{result.get('knowledge_deleted', 0)} knowledge files; "
        f"requests={result.get('requests', 0)}[/dim]"
    )


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

    try:
        from core.locket_archive_sync import sync_locket_archive

        synced = sync_locket_archive(refresh_index=False)
        console.print(
            f"[green]Locket recall: {synced.get('sessions_synced', 0)} chats, "
            f"{synced.get('knowledge_synced', 0)} knowledge files[/green]"
        )
    except Exception as error:
        console.print(f"[yellow]Locket recall sync skipped: {error}[/yellow]")


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
    # codex 0.141+ saves the generated image into its CWD (not gen_dir). Run in an
    # isolated empty temp dir so we can reliably pick up THIS call's output and never
    # grab a stale prior render.
    import tempfile
    workdir = tempfile.mkdtemp(prefix="serena_genimg_")
    # Each `codex exec` writes a rollout that embeds the generated image as base64
    # (2-4MB a pop). These one-shot gen sessions are throwaway — snapshot the rollout
    # dir so we can delete the one this call creates, or they silently eat the disk.
    sess_dir = os.path.expanduser("~/.codex/sessions")
    pre_rollouts = set(glob.glob(os.path.join(sess_dir, "**", "*.jsonl"), recursive=True))

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
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=workdir)
    except subprocess.TimeoutExpired:
        console.print(f"[red]Timed out after {timeout}s.[/red]"); return
    if proc.returncode != 0:
        console.print(f"[red]codex exec failed (exit {proc.returncode}):[/red]")
        console.print(proc.stderr or proc.stdout); return

    IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
    # Try to extract the path from codex's stdout (we asked it to print SAVED:<path>)
    saved_path = None
    for line in proc.stdout.splitlines():
        m = re.match(r"^SAVED:(.+)$", line.strip())
        if m:
            cand = m.group(1).strip()
            if not os.path.isabs(cand):
                cand = os.path.join(workdir, cand)
            if os.path.isfile(cand):
                saved_path = cand
            break

    # Primary: newest image codex dropped in the isolated workdir (codex 0.141 saves to CWD)
    if not saved_path:
        imgs = [p for p in glob.glob(os.path.join(workdir, "**", "*"), recursive=True)
                if os.path.isfile(p) and p.lower().endswith(IMG_EXT)]
        if imgs:
            imgs.sort(key=os.path.getmtime)
            saved_path = imgs[-1]

    # NO shared-dir fallback. It used to diff ~/.codex/generated_images for new files,
    # but that dir is shared across every concurrent/orphaned gen: a straggler codex
    # process from an earlier topic can drop its image there mid-run, and the diff then
    # hands it back as *this* call's result (a Jozy Altidore render was once saved as a
    # Messi asset this way). A gen that leaves nothing in its own isolated workdir is a
    # failure; report it as one and let the caller retry.

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

    # Clean up after ourselves: the temp workdir and this call's throwaway rollout
    # (which holds the image inline as base64). Without this, a long batch run fills the disk.
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        for p in set(glob.glob(os.path.join(sess_dir, "**", "*.jsonl"), recursive=True)) - pre_rollouts:
            os.remove(p)
    except OSError:
        pass


@main.command(name="codex-exec")
@click.option("--model", default=None, help="Codex model to use. Omit to use the active default from ~/.codex/config.toml.")
@click.option("--effort", type=click.Choice(["none", "low", "medium", "high", "xhigh", "max"]), default=None,
              help="Reasoning effort (maps to model_reasoning_effort). Omit for the config default.")
@click.option("--cwd", "work_dir", default=None, type=click.Path(file_okay=False),
              help="Directory to run in (default: ~/.cache/serena-headless-codex/work, created if missing).")
@click.option(
    "--timeout",
    default=0,
    type=click.IntRange(min=0),
    help="Optional max seconds to wait for codex; 0 waits until the job finishes (default 0).",
)
@click.option("--danger-full-access/--workspace-write", default=True,
              help="Sandbox mode for the spawned Codex agent (default: danger-full-access).")
@click.option("--visible", "-V", is_flag=True, default=False,
              help="Keep the rollout on disk and surface the job as a real codex chat in Serena "
                   "(drops --ephemeral, marks it resident work, gives it a title).")
@click.option("--title", "custom_title", default=None,
              help="Chat title when visible (default: '<model> <effort>: <prompt>').")
@click.option("--link-to", "link_to", default=None, metavar="SID",
              help="Link this job into the same group as SID (joins SID's existing group if it has "
                   "one). Implies --visible.")
@click.option("--link-current", is_flag=True, default=False,
              help="Link to the launching Claude session detected from the process environment. "
                   "Intended for Codex agents running inside Claude workflows; implies --visible.")
@click.argument("prompt", nargs=-1)
def codex_exec(model, effort, work_dir, timeout, danger_full_access, visible,
               custom_title, link_to, link_current, prompt):
    """Run a headless one-shot codex job and print the result as JSON.

    Each call starts a fresh, real `codex exec` agent. The ordinary default is
    ephemeral, so ad-hoc headless jobs do not clutter Serena's chat list.

    With `--visible` (implied by `--link-to`) that flips: `--ephemeral` is
    dropped so codex writes a normal rollout, and once the session id is known
    the job is marked resident work + given a title, which is exactly what
    core/codex_scanner.py needs to surface an `exec` session in the chat list.
    `--link-to SID` additionally puts it in SID's group. A Codex agent inside
    a Claude workflow must use `--link-current`: it detects the launching
    Claude session, persists the real Codex rollout, and surfaces it as soon as
    Codex emits `thread.started`, not after the job ends.

    The prompt is the joined arguments; pass `-` (or nothing) to read it from
    stdin. Output is ALWAYS a single JSON object on stdout:

      {"ok", "result_text", "session_id", "exit_code", "error", "duration_s",
       "visible", "group"[, "warnings"]}

    Metadata/link/index side effects are best-effort: if one fails it lands in
    "warnings" and the codex result is returned anyway.

    The process exit code is 0 even when ok=false — the JSON carries the
    failure so callers only ever parse one thing.

    Examples:
      chats codex-exec "summarize the diff in /tmp/patch.txt"
      cat task.md | chats codex-exec --effort high --cwd /home/raghav/proj -
      chats codex-exec --link-current --model gpt-5.6-sol --effort high "..."
    """
    import json
    import os
    import queue
    import shutil
    import signal
    import subprocess
    import sys
    import threading
    import time
    from contextlib import suppress

    started = time.monotonic()
    warnings: list[str] = []
    group_id: str | None = None

    if link_current and link_to:
        emit_payload = {
            "ok": False,
            "result_text": "",
            "session_id": None,
            "exit_code": -1,
            "error": "use either --link-current or --link-to, not both",
            "duration_s": round(time.monotonic() - started, 3),
            "visible": True,
            "group": None,
        }
        print(json.dumps(emit_payload))
        return
    if link_current:
        link_to = _detect_claude_sid()
        if not link_to:
            warnings.append("could not detect launching claude session; rollout will be visible but unlinked")
    # Linking only works for a persisted, indexable rollout.
    visible = bool(visible or link_to or link_current)

    def emit(ok, result_text="", session_id="", exit_code=-1, error=None):
        # Plain print, no rich markup: this output is machine-parsed.
        payload = {
            "ok": bool(ok),
            "result_text": result_text or "",
            "session_id": session_id or None,
            "exit_code": int(exit_code),
            "error": error,
            "duration_s": round(time.monotonic() - started, 3),
            "visible": bool(visible),
            "group": group_id or None,
        }
        if warnings:
            payload["warnings"] = list(warnings)
        print(json.dumps(payload))

    if os.environ.get("SERENA_FLEET_WORKER", "").strip().lower() in {"1", "true", "on"}:
        emit(False, error="nested Codex jobs are disabled inside Fleet workers")
        return

    text = " ".join(prompt).strip()
    if not text or text == "-":
        text = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if not text:
        emit(False, error="prompt is required")
        return

    first_line = " ".join(text.split())
    title_text = (custom_title or "").strip()
    if not title_text:
        model_names = {
            "gpt-5.6-sol": "Sol 5.6",
            "gpt-5.6-terra": "Terra 5.6",
            "gpt-5.6-luna": "Luna 5.6",
        }
        model_name = model_names.get(model or "", model or "Codex")
        effort_name = f" {effort}" if effort else ""
        title_text = f"{model_name}{effort_name}: {first_line[:40] or 'workflow job'}"

    surfaced_sid = ""
    external_runtime_claimed = False

    def surface_session(sid: str) -> None:
        """Expose and group the rollout immediately after thread.started."""
        nonlocal external_runtime_claimed, group_id, surfaced_sid
        if not visible or not sid or surfaced_sid == sid:
            return
        surfaced_sid = sid
        try:
            from core import metadata as _meta

            _meta.set_resident_work(sid)
            _meta.set_custom_title(sid, title_text)
        except Exception as e:  # noqa: BLE001 - never fail the agent over metadata
            warnings.append(f"could not mark session visible: {e}")
        try:
            from core import metadata as _meta

            lease_seconds = float(timeout) + 60.0 if timeout else 24 * 60 * 60.0
            _meta.set_external_runtime(
                sid,
                kind="codex-exec",
                pid=proc.pid,
                lease_seconds=max(lease_seconds, 120.0),
            )
            external_runtime_claimed = True
        except Exception as e:  # noqa: BLE001
            warnings.append(f"could not claim external runtime: {e}")
        if link_to:
            try:
                from core import metadata as _meta

                group_id = _meta.link_sessions([sid, link_to])
            except Exception as e:  # noqa: BLE001
                warnings.append(f"could not link to {link_to}: {e}")
        try:
            from core.indexer import update_index

            update_index()
        except Exception as e:  # noqa: BLE001
            warnings.append(f"initial index refresh failed: {e}")

    work = Path(work_dir).expanduser() if work_dir else Path.home() / ".cache" / "serena-headless-codex" / "work"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        emit(False, error=f"could not create cwd {work}: {e}")
        return

    sandbox = "danger-full-access" if danger_full_access else "workspace-write"
    argv = [
        shutil.which("codex") or "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "-c", 'approval_policy="never"',
        "-c", f'sandbox_mode="{sandbox}"',
    ]
    if not visible:
        # Nothing persisted: no rollout file, so no cleanup dance and no
        # stale trust entries in ~/.codex/config.toml. Under --visible we
        # WANT the rollout — it's the only thing the scanner can index.
        argv.insert(3, "--ephemeral")
    if model:
        argv += ["-c", f'model="{model}"']
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += ["-C", str(work), "-"]

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(work),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Own process group so a timeout can kill codex AND anything it spawned.
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as e:
        emit(False, error=f"could not start codex: {e}")
        return

    session_id = ""
    result_text = ""
    error_detail = ""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def handle_stdout(line: str) -> None:
        nonlocal session_id, result_text, error_detail
        stdout_lines.append(line)
        try:
            event = json.loads(line.strip())
        except (json.JSONDecodeError, AttributeError):
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "thread.started":
            session_id = str(event.get("thread_id") or "") or session_id
            surface_session(session_id)
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if not isinstance(item, dict):
                return
            if item.get("type") == "agent_message":
                result_text = str(item.get("text") or "").strip()
            elif item.get("type") == "error":
                error_detail = str(item.get("message") or "").strip() or error_detail
        elif event_type == "turn.failed":
            turn_error = event.get("error")
            if isinstance(turn_error, dict):
                error_detail = str(turn_error.get("message") or "").strip() or error_detail
        elif event_type == "error":
            error_detail = str(event.get("message") or "").strip() or error_detail

    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def drain_pipe(name: str, pipe) -> None:
        try:
            for line in iter(pipe.readline, ""):
                output_queue.put((name, line))
        finally:
            output_queue.put((name, None))

    readers = [
        threading.Thread(target=drain_pipe, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=drain_pipe, args=("stderr", proc.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    finalized_session = False

    def finalize_session() -> None:
        nonlocal external_runtime_claimed, finalized_session
        if finalized_session:
            return
        finalized_session = True
        if external_runtime_claimed and session_id:
            try:
                from core import metadata as _meta

                _meta.clear_external_runtime(session_id, pid=proc.pid)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"could not release external runtime: {e}")
            external_runtime_claimed = False
        if visible and session_id:
            try:
                from core.indexer import update_index

                update_index()
            except Exception as e:  # noqa: BLE001
                warnings.append(f"final index refresh failed: {e}")

    try:
        try:
            proc.stdin.write(text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        deadline = time.monotonic() + timeout if timeout else None
        open_pipes = len(readers)
        while open_pipes:
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                queue_timeout = min(0.25, remaining) if remaining is not None else 0.25
                source, line = output_queue.get(timeout=queue_timeout)
            except queue.Empty:
                continue
            if line is None:
                open_pipes -= 1
            elif source == "stdout":
                handle_stdout(line)
            else:
                stderr_lines.append(line)
        if deadline is None:
            proc.wait()
        else:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with suppress(Exception):
            proc.wait(timeout=5)  # reap so it doesn't sit as a zombie
        emit(False, session_id=session_id, error=f"timeout after {timeout}s")
        return
    finally:
        finalize_session()

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    if visible:
        if not session_id:
            warnings.append("codex reported no thread id; session not made visible")

    code = proc.returncode if proc.returncode is not None else -1
    if code != 0:
        detail = error_detail or ((stderr or "").strip() or (stdout or "").strip())[-2000:]
        detail = detail[-2000:]
        emit(False, result_text, session_id, code,
             f"codex exec exited with status {code}: {detail}" if detail else f"codex exec exited with status {code}")
        return
    if not result_text:
        detail = f": {error_detail}" if error_detail else ""
        emit(False, "", session_id, code, f"empty output{detail}")
        return
    emit(True, result_text, session_id, code, None)


@main.group(name="fleet")
def fleet_group():
    """Run and inspect durable provider-routed workflows."""


def _fleet_print(payload: object, *, as_json: bool = False) -> None:
    """Keep Fleet CLI output useful to humans and stable for scripts."""
    import json

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    if isinstance(payload, dict):
        run_id = str(payload.get("run_id") or payload.get("id") or "")
        status = str(payload.get("status") or payload.get("state") or "")
        title = str(payload.get("title") or payload.get("task") or "").strip()
        if run_id:
            console.print(
                f"[bold cyan]{run_id}[/bold cyan]"
                + (f"  [bold]{status}[/bold]" if status else "")
                + (f"  {title}" if title else "")
            )
            return
    console.print(payload)


def _fleet_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@fleet_group.command(name="start")
@click.option(
    "--activity",
    type=click.Choice(["auto", "coding", "research"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--provider",
    "provider_mode",
    type=click.Choice(["auto", "balanced", "codex", "claude"]),
    default="auto",
    show_default=True,
    help="Provider routing. Auto may avoid a provider with a confirmed fresh usage limit.",
)
@click.option(
    "--workers",
    "worker_count",
    type=click.IntRange(min=1, max=4),
    default=None,
    help="Exact persistent worker count (1-4). Omit for task-based scaling.",
)
@click.option("--cwd", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--origin", "origin_session_id", default=None, help="Launching Serena session id.")
@click.option("--origin-agent", type=click.Choice(["claude", "codex"]), default=None)
@click.option("--dry-run", is_flag=True, help="Validate and materialize the workflow without model calls.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.argument("task", nargs=-1, required=True)
def fleet_start(
    activity,
    provider_mode,
    worker_count,
    work_dir,
    origin_session_id,
    origin_agent,
    dry_run,
    as_json,
    task,
):
    """Start a four-phase Fleet run and return immediately."""
    import os

    from core.fleet_supervisor import start_run

    prompt = " ".join(task).strip()
    if not origin_session_id:
        if os.environ.get("CODEX_THREAD_ID"):
            origin_session_id = _detect_codex_sid()
            origin_agent = origin_agent or "codex"
        elif os.environ.get("CLAUDE_CODE_SESSION_ID"):
            origin_session_id = _detect_claude_sid()
            origin_agent = origin_agent or "claude"
    run = _fleet_call(
        start_run,
        prompt,
        activity=activity,
        provider_mode=provider_mode,
        worker_count=worker_count,
        cwd=str(work_dir.expanduser()) if work_dir else None,
        origin_session_id=origin_session_id,
        origin_agent=origin_agent,
        dry_run=dry_run,
    )
    _fleet_print(run, as_json=as_json)


@fleet_group.command(name="list")
@click.option("--limit", default=20, type=click.IntRange(min=1, max=500), show_default=True)
@click.option("--json", "as_json", is_flag=True)
def fleet_list(limit, as_json):
    """List recent Fleet runs."""
    from core.fleet_supervisor import list_runs

    runs = _fleet_call(list_runs, limit=limit)
    if as_json:
        _fleet_print(runs, as_json=True)
        return
    if not runs:
        console.print("[dim]no fleet runs yet[/dim]")
        return
    from rich.table import Table

    table = Table(box=None, show_header=True, header_style="bold")
    table.add_column("run")
    table.add_column("status")
    table.add_column("activity")
    table.add_column("phase")
    table.add_column("task")
    for run in runs:
        table.add_row(
            str(run.get("run_id") or run.get("id") or "")[:18],
            str(run.get("status") or run.get("state") or ""),
            str(run.get("activity") or ""),
            str(run.get("current_phase") or ""),
            str(run.get("title") or run.get("task") or "")[:64],
        )
    console.print(table)


@fleet_group.command(name="status")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True)
def fleet_status(run_id, as_json):
    """Show one Fleet run."""
    from core.fleet_supervisor import get_run

    run = _fleet_call(get_run, run_id)
    if run is None:
        raise click.ClickException(f"unknown Fleet run: {run_id}")
    _fleet_print(run, as_json=as_json)


@fleet_group.command(name="inspect")
@click.argument("run_id")
@click.option(
    "--focus",
    default="",
    help="Work-unit id, worker key, or leg id to isolate.",
)
@click.option("--events", "event_limit", default=50, type=click.IntRange(min=1, max=100))
@click.option("--json", "as_json", is_flag=True)
def fleet_inspect(run_id, focus, event_limit, as_json):
    """Inspect durable DAG, context-budget, and event evidence."""
    import json

    from core.fleet_supervisor import inspect_run

    inspection = _fleet_call(
        inspect_run,
        run_id,
        focus,
        event_limit=event_limit,
    )
    if as_json:
        _fleet_print(inspection, as_json=True)
    else:
        click.echo(json.dumps(inspection, indent=2, sort_keys=True, default=str))


@fleet_group.command(name="wait")
@click.argument("run_id")
@click.option("--timeout", default=0.0, type=click.FloatRange(min=0), show_default=True)
@click.option("--json", "as_json", is_flag=True)
def fleet_wait(run_id, timeout, as_json):
    """Wait for a run to reach a terminal state."""
    from core.fleet_supervisor import wait_for_run

    run = _fleet_call(wait_for_run, run_id, timeout=None if timeout == 0 else timeout)
    _fleet_print(run, as_json=as_json)


@fleet_group.command(name="stop")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True)
def fleet_stop(run_id, as_json):
    """Cancel a queued or active run."""
    from core.fleet_supervisor import stop_run

    _fleet_print(_fleet_call(stop_run, run_id), as_json=as_json)


@fleet_group.command(name="delete")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True)
def fleet_delete(run_id, as_json):
    """Delete a terminal run and its Fleet-owned worker chats."""
    from core.fleet_supervisor import delete_run

    _fleet_print(_fleet_call(delete_run, run_id), as_json=as_json)


@fleet_group.command(name="retry")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True)
def fleet_retry(run_id, as_json):
    """Start a new attempt using an existing run's frozen policy."""
    from core.fleet_supervisor import retry_run

    _fleet_print(_fleet_call(retry_run, run_id), as_json=as_json)


@fleet_group.command(name="handoff")
@click.argument("run_id")
@click.argument("leg_id")
@click.argument("provider", type=click.Choice(["codex", "claude"]))
@click.option("--json", "as_json", is_flag=True)
def fleet_handoff(run_id, leg_id, provider, as_json):
    """Continue one unfinished worker on the other native provider."""
    from core.fleet_supervisor import handoff_leg

    _fleet_print(
        _fleet_call(handoff_leg, run_id, leg_id, provider),
        as_json=as_json,
    )


@fleet_group.command(name="result")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True)
def fleet_result(run_id, as_json):
    """Print a run's reconciled final result."""
    from core.fleet_supervisor import get_result

    result = _fleet_call(get_result, run_id)
    if as_json:
        _fleet_print(result, as_json=True)
    else:
        click.echo(str(result.get("result_text") or result.get("error") or ""))


@fleet_group.command(name="steer")
@click.argument("run_id")
@click.argument("message", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True)
def fleet_steer(run_id, message, as_json):
    """Add context that future legs in an active run must consume."""
    from core.fleet_supervisor import steer_run

    _fleet_print(
        _fleet_call(steer_run, run_id, " ".join(message).strip()),
        as_json=as_json,
    )


@fleet_group.command(name="doctor")
@click.option("--json", "as_json", is_flag=True)
def fleet_doctor(as_json):
    """Check worker binaries, policy, store, integrations, and service."""
    from core.fleet_supervisor import doctor

    report = _fleet_call(doctor)
    _fleet_print(report, as_json=as_json)
    if not report.get("ok"):
        raise click.exceptions.Exit(1)


@fleet_group.command(name="serve", hidden=True)
def fleet_serve():
    """Own the durable Fleet queue (normally run by systemd)."""
    from core.fleet_supervisor import serve_forever

    serve_forever()


@fleet_group.command(name="mcp", hidden=True)
def fleet_mcp():
    """Serve Fleet tools over MCP stdio."""
    from core.fleet_mcp import run_mcp_server

    run_mcp_server()


@fleet_group.command(name="read-mcp", hidden=True)
def fleet_read_mcp():
    """Serve Fleet's read-only account gateway over MCP stdio."""
    from core.fleet_read_mcp import run_gateway

    run_gateway()


@fleet_group.command(name="read-tools")
@click.option("--refresh", is_flag=True, help="Rebuild the catalog from the live servers.")
@click.option("--json", "as_json", is_flag=True)
def fleet_read_tools(refresh, as_json):
    """Show which read-only account tools Fleet's Research and Review legs get."""
    from core.fleet_read_mcp import (
        allowed_servers,
        catalog_tools,
        load_catalog,
        refresh_catalog,
    )

    summary = refresh_catalog(force=True) if refresh else None
    catalog = load_catalog()
    tools = catalog_tools(catalog)
    payload = {
        # NOT list(...): this module defines a `list` command that shadows the
        # builtin at module scope.
        "servers": [*allowed_servers()],
        "catalog": str((catalog or {}).get("generated_at") or ""),
        "tool_count": len(tools),
        "tools": [f"{tool['server']}.{tool['tool']}" for tool in tools],
        "server_status": (catalog or {}).get("servers", {}),
        "refresh": summary,
    }
    if as_json:
        _fleet_print(payload, as_json=True)
        return
    if summary and not summary["refreshed"]:
        console.print(f"[yellow]not refreshed:[/yellow] {summary['reason']}")
    if not tools:
        console.print("[dim]no read tools are exposed; workers run with no MCP at all[/dim]")
        return
    for name, status in sorted((payload["server_status"] or {}).items()):
        state = status.get("status")
        detail = f"{status.get('exposed', 0)} read, {status.get('denied', 0)} denied"
        if state != "ok":
            detail = str(status.get("error") or state)
        console.print(f"[bold]{name}[/bold]: {detail}")
    console.print(f"[dim]{len(tools)} tools exposed to read-only Fleet legs[/dim]")


# --- bounded extensibility and automation (plugins, schedules, notices) ---
#
# These exist so the approval gates are usable. An approval boundary nobody can
# approve through is theatre, so install, enable, schedule, and notification
# approval all have a real operator command here.


def _emit_json(payload: object) -> None:
    """cli.py imports json per-function by convention; keep that here."""
    import json

    click.echo(json.dumps(payload, indent=2, default=str))


def _operator_call(function, *args, **kwargs):
    """Turn a refused manifest or illegal transition into a readable message.

    These commands are the approval boundary an operator actually uses, so a
    rejection has to read like an answer, not a stack trace.
    """
    try:
        return function(*args, **kwargs)
    except (ValueError, RuntimeError, KeyError) as exc:
        raise click.ClickException(str(exc).strip("'") or exc.__class__.__name__) from exc


@main.group(name="plugin")
def plugin_group():
    """Inspect and approve Serena plugin manifests."""


def _plugin_registry():
    from core.serena_plugins import PluginRegistry

    return PluginRegistry()


@plugin_group.command(name="list")
@click.option("--state", default=None, help="Filter by lifecycle state.")
@click.option("--json", "as_json", is_flag=True)
def plugin_list(state, as_json):
    """List registered plugins and their lifecycle state."""
    records = _plugin_registry().list(state=state)
    if as_json:
        _emit_json(records)
        return
    if not records:
        click.echo("no plugins registered")
        return
    for record in records:
        click.echo(
            f"{record['plugin_id']:<28} {record['state']:<10} v{record['version']:<8} "
            f"health={record['health_state']}"
        )


@plugin_group.command(name="stage")
@click.argument("manifest_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", required=True, help="Who is proposing this change.")
@click.option("--reason", default="", help="Why this change is being proposed.")
def plugin_stage(manifest_path, actor, reason):
    """Stage a manifest as a pending, unapproved change."""
    from pathlib import Path as _Path

    staged = _operator_call(
        _plugin_registry().stage,
        _Path(manifest_path).read_text(encoding="utf-8"),
        actor=actor,
        reason=reason,
    )
    _emit_json(staged)


@plugin_group.command(name="pending")
@click.option("--json", "as_json", is_flag=True)
def plugin_pending(as_json):
    """Show staged plugin changes awaiting approval."""
    stages = _plugin_registry().pending_stages()
    if as_json:
        _emit_json(stages)
        return
    if not stages:
        click.echo("no staged plugin changes")
        return
    for stage in stages:
        diff = stage.get("diff") or {}
        flag = " ESCALATES PRIVILEGE" if diff.get("escalates_privilege") else ""
        click.echo(f"{stage['stage_id']}  {stage['plugin_id']}  by {stage['actor']}{flag}")
        if diff.get("added_scopes"):
            click.echo(f"    adds scopes: {', '.join(diff['added_scopes'])}")


@plugin_group.command(name="approve")
@click.argument("stage_id")
@click.option("--actor", required=True, help="Who is approving this change.")
def plugin_approve(stage_id, actor):
    """Approve a staged manifest, installing the plugin."""
    _emit_json(_operator_call(_plugin_registry().approve_stage, stage_id, actor=actor))


@plugin_group.command(name="reject")
@click.argument("stage_id")
@click.option("--actor", required=True)
@click.option("--reason", default="")
def plugin_reject(stage_id, actor, reason):
    """Reject a staged manifest."""
    _operator_call(_plugin_registry().reject_stage, stage_id, actor=actor, reason=reason)
    click.echo("rejected")


@plugin_group.command(name="set-state")
@click.argument("plugin_id")
@click.argument("state", type=click.Choice(["installed", "enabled", "disabled", "removed"]))
@click.option("--actor", required=True)
def plugin_set_state(plugin_id, state, actor):
    """Move a plugin through its lifecycle."""
    _emit_json(_operator_call(_plugin_registry().transition, plugin_id, state, actor=actor))


@main.group(name="schedule")
def schedule_group():
    """Inspect and approve Serena's bounded schedules."""


def _scheduler():
    from core.scheduler_actions import register_all
    from core.serena_scheduler import SerenaScheduler

    return register_all(SerenaScheduler())


@schedule_group.command(name="list")
@click.option("--state", default=None)
@click.option("--json", "as_json", is_flag=True)
def schedule_list(state, as_json):
    """List schedules and when they next run."""
    records = _scheduler().list(state=state)
    if as_json:
        _emit_json(records)
        return
    if not records:
        click.echo("no schedules registered")
        return
    for record in records:
        click.echo(
            f"{record['schedule_id'][:8]}  {record['action']:<24} {record['state']:<16} "
            f"every {record['interval_seconds']}s  failures={record['consecutive_failures']}"
        )


@schedule_group.command(name="approve")
@click.argument("schedule_id")
@click.option("--actor", required=True)
def schedule_approve(schedule_id, actor):
    """Approve a schedule so it is allowed to run."""
    _emit_json(_operator_call(_scheduler().approve, schedule_id, actor=actor))


@schedule_group.command(name="pause")
@click.argument("schedule_id")
def schedule_pause(schedule_id):
    """Pause a schedule without deleting it."""
    _emit_json(_operator_call(_scheduler().set_state, schedule_id, "paused"))


@schedule_group.command(name="resume")
@click.argument("schedule_id")
@click.option("--actor", required=True)
def schedule_resume(schedule_id, actor):
    """Reactivate a paused or disabled schedule."""
    _emit_json(_operator_call(_scheduler().resume, schedule_id, actor=actor))


@schedule_group.command(name="add")
@click.argument("action")
@click.option("--every", "interval_seconds", type=int, required=True,
              help="Interval in seconds between runs.")
@click.option("--actor", required=True)
@click.option("--payload", default="", help="JSON object handed to the handler.")
@click.option("--workdir", default="", help="Absolute project directory to run in.")
@click.option("--once", "one_shot", is_flag=True, help="Retire after one success.")
@click.option("--chain-to", multiple=True, help="Schedule id to wake on success.")
@click.option("--join-of", multiple=True, help="Schedule id that must succeed first.")
@click.option("--dedupe-key", default="")
@click.option("--dedupe-window", "dedupe_window_seconds", type=int, default=0)
@click.option("--no-approval", is_flag=True, help="Skip the approval gate.")
def schedule_add(
    action, interval_seconds, actor, payload, workdir, one_shot, chain_to,
    join_of, dedupe_key, dedupe_window_seconds, no_approval,
):
    """Add a schedule for one already-reviewed action."""
    import json

    try:
        parsed = json.loads(payload) if payload.strip() else {}
    except json.JSONDecodeError as error:
        raise click.ClickException(f"--payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise click.ClickException("--payload must be a JSON object")
    _emit_json(
        _operator_call(
            _scheduler().add_schedule,
            action=action,
            interval_seconds=interval_seconds,
            actor=actor,
            payload=parsed,
            workdir=workdir or None,
            one_shot=one_shot,
            # `list` is a click command at this module's scope, so the builtin
            # is not available here. Unpacking avoids calling that command.
            chain_to=[*chain_to],
            join_of=[*join_of],
            dedupe_key=dedupe_key,
            dedupe_window_seconds=dedupe_window_seconds,
            requires_approval=not no_approval,
        )
    )


@schedule_group.command(name="edit")
@click.argument("schedule_id")
@click.option("--actor", required=True)
@click.option("--every", "interval_seconds", type=int, default=None)
@click.option("--payload", default=None, help="Replacement JSON payload.")
@click.option("--workdir", default=None)
@click.option("--chain-to", multiple=True)
@click.option("--join-of", multiple=True)
def schedule_edit(schedule_id, actor, interval_seconds, payload, workdir, chain_to, join_of):
    """Change a schedule's shape. The action itself is never editable."""
    import json

    parsed = None
    if payload is not None:
        try:
            parsed = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError as error:
            raise click.ClickException(f"--payload must be JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise click.ClickException("--payload must be a JSON object")
    _emit_json(
        _operator_call(
            _scheduler().edit,
            schedule_id,
            actor=actor,
            interval_seconds=interval_seconds,
            payload=parsed,
            workdir=workdir,
            chain_to=[*chain_to] if chain_to else None,
            join_of=[*join_of] if join_of else None,
        )
    )


@schedule_group.command(name="remove")
@click.argument("schedule_id")
@click.option("--actor", required=True)
def schedule_remove(schedule_id, actor):
    """Retire a schedule for good, keeping its history."""
    _emit_json(_operator_call(_scheduler().remove, schedule_id, actor=actor))


@schedule_group.command(name="run-now")
@click.argument("schedule_id")
def schedule_run_now(schedule_id):
    """Run one active schedule immediately, without shifting its rotation."""
    run = _operator_call(_scheduler().run_now, schedule_id)
    click.echo(f"{'ok' if run.ok else 'FAILED'}  {run.action}  {run.detail[:100]}")


@schedule_group.command(name="actions")
def schedule_actions():
    """List the reviewed actions a schedule is allowed to name."""
    names = _scheduler().actions
    if not names:
        click.echo("no scheduler actions are registered")
        return
    for name in names:
        click.echo(name)


@schedule_group.command(name="history")
@click.argument("schedule_id", required=False)
@click.option("--limit", default=20)
def schedule_history(schedule_id, limit):
    """Show recent schedule runs."""
    for entry in _scheduler().history(schedule_id, limit=limit):
        status = "ok" if entry["ok"] else "FAILED"
        click.echo(f"{entry['action']:<24} {status:<7} {entry['detail'][:80]}")


@main.group(name="automation")
def automation_group():
    """Serena's resident bounded automation loop."""


@automation_group.command(name="serve")
@click.option("--interval", default=30.0, help="Seconds between passes.")
@click.option("--passes", default=0, help="Stop after N passes. 0 runs forever.")
def automation_serve(interval, passes):
    """Run the resident loop that ticks schedules and delivers due notices."""
    from core.automation_runtime import AutomationRuntime

    runtime = AutomationRuntime(poll_seconds=interval)
    ran = runtime.serve_forever(max_passes=passes)
    click.echo(f"automation loop finished after {ran} passes")


@automation_group.command(name="once")
@click.option("--json", "as_json", is_flag=True)
def automation_once(as_json):
    """Run exactly one pass of the loop and report what it did."""
    from core.automation_runtime import AutomationRuntime

    report = AutomationRuntime().run_pass()
    if as_json:
        _emit_json(report.to_dict())
        return
    if report.capacity_held:
        click.echo(f"held: {report.capacity_reason}")
    click.echo(
        f"ran={len(report.ran)} notices={report.notifications} "
        f"published={sum(report.published.values())} errors={len(report.errors)}"
    )
    for error in report.errors:
        click.echo(f"  error: {error}")


@main.command(name="owed")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", default=8)
def owed(as_json, limit):
    """What Serena still owes Raghav, and what she cannot confirm he got."""
    from core.control_recovery import outstanding_debts

    debts = outstanding_debts(limit=limit)
    if as_json:
        _emit_json(debts)
        return
    click.echo(debts["spoken"])
    for item in debts["items"]:
        click.echo(
            f"  [{item['surface']}] {item['owes']} - {item['summary'][:60]} "
            f"({item['waiting_for']}, attempts={item['attempts']})"
        )
    for item in debts["unconfirmed_items"]:
        click.echo(f"  [?] {item['owes']} - {item['reason'] or 'never confirmed'}")


@main.group(name="webhook")
def webhook_group():
    """Serena's authenticated signed webhook ingress."""


def _ingress():
    from core.webhook_ingress import default_ingress

    return default_ingress()


@webhook_group.command(name="routes")
def webhook_routes():
    """List the reviewed routes a signed request may reach."""
    for name in _ingress().routes:
        click.echo(name)


@webhook_group.command(name="pending")
def webhook_pending():
    """Show verified deliveries held for approval."""
    records = _ingress().pending()
    if not records:
        click.echo("no webhook deliveries awaiting approval")
        return
    for record in records:
        click.echo(f"{record['delivery_id']}  {record['route']}  {record['reason'][:60]}")


@webhook_group.command(name="approve")
@click.argument("delivery_id")
@click.option("--actor", required=True)
def webhook_approve(delivery_id, actor):
    """Release one held webhook delivery."""
    _emit_json(_operator_call(_ingress().approve, delivery_id, actor=actor).to_dict())


@webhook_group.command(name="history")
@click.option("--route", default=None)
@click.option("--decision", default=None)
@click.option("--limit", default=20)
def webhook_history(route, decision, limit):
    """Show the durable audit trail of inbound webhook deliveries."""
    records = _ingress().history(route=route, decision=decision, limit=limit)
    if not records:
        click.echo("no webhook deliveries recorded")
        return
    for record in records:
        click.echo(
            f"{record['decision']:<10} {record['status']:<4} {record['route']:<16} "
            f"{record['reason'][:60]}"
        )


@main.group(name="notify")
def notify_group():
    """Inspect Serena's one notification authority."""


def _authority():
    from core.notification_authority import NotificationAuthority

    return NotificationAuthority()


@notify_group.command(name="history")
@click.option("--channel", default=None)
@click.option("--decision", default=None)
@click.option("--limit", default=20)
@click.option("--json", "as_json", is_flag=True)
def notify_history(channel, decision, limit, as_json):
    """Show recent notification decisions and delivery outcomes."""
    records = _authority().history(channel=channel, decision=decision, limit=limit)
    if as_json:
        _emit_json(records)
        return
    if not records:
        click.echo("no notifications recorded")
        return
    for record in records:
        click.echo(
            f"{record['decision']:<16} {record['channel']:<9} {record['kind']:<28} "
            f"{record['summary'][:60]}"
        )


@notify_group.command(name="pending")
def notify_pending():
    """Show notices held for approval."""
    records = _authority().pending_approvals()
    if not records:
        click.echo("no notifications awaiting approval")
        return
    for record in records:
        click.echo(f"{record['notification_id']}  {record['kind']}  {record['summary'][:70]}")


@notify_group.command(name="approve")
@click.argument("notification_id")
def notify_approve(notification_id):
    """Release one held notice for delivery."""
    _emit_json(_operator_call(_authority().approve, notification_id).to_dict())


@notify_group.command(name="flush")
@click.option("--limit", default=20)
def notify_flush(limit):
    """Deliver notices whose quiet-hours or retry deadline has passed."""
    results = _authority().deliver_due(limit=limit)
    if not results:
        click.echo("nothing due")
        return
    for result in results:
        click.echo(f"{result.decision:<12} {result.notification_id}  {result.reason[:60]}")


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
    import json, os, urllib.request, urllib.error, socket
    if os.environ.get("SERENA_FLEET_WORKER", "").strip().lower() in {"1", "true", "on"}:
        raise click.ClickException("linked-agent bridges are disabled inside Fleet workers")
    text = " ".join(prompt).strip()
    if not text:
        console.print("[red]prompt is required[/red]"); return

    ports = [int(port)] if port else _detect_serena_ports()
    if not ports:
        console.print("[red]Could not find a running Serena instance on localhost.[/red]"); return

    # Two paths, mirroring ask-codex:
    #   --sid explicitly provided → straight to /api/claude-bridge
    #   --sid omitted → /api/ask-linked-claude which handles both the
    #       already-linked and auto-spawn cases atomically
    if sid:
        endpoint = "/api/claude-bridge"
        body = {"target_sid": sid, "prompt": text, "timeout": timeout}
    else:
        my_sid = from_sid or _detect_codex_sid()
        if not my_sid:
            console.print("[red]Could not detect current codex sid; pass --sid or --from-sid.[/red]"); return
        endpoint = "/api/ask-linked-claude"
        body = {"codex_sid": my_sid, "prompt": text, "timeout": timeout}
    errors: list[str] = []
    for p in ports:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{p}{endpoint}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except socket.timeout as e:
            console.print(f"[red]Bridge call timed out on port {p}: {e}[/red]"); return
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            errors.append(f"{p}: {e}")
            continue

        if payload.get("ok"):
            console.print(payload.get("response") or "(no response)")
            return
        msg = payload.get("message", "unknown error")
        errors.append(f"{p}: {msg}")
        if port or not _is_bridge_live_miss(msg):
            if payload.get("response"):
                console.print(payload["response"])
            break

    console.print(f"[red]{errors[-1] if errors else 'bridge failed'}[/red]")


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

    if os.environ.get("SERENA_FLEET_WORKER", "").strip().lower() in {"1", "true", "on"}:
        raise click.ClickException("linked-agent bridges are disabled inside Fleet workers")

    text = " ".join(prompt).strip()
    if not text:
        console.print("[red]prompt is required[/red]"); return

    # Find Serena's port (it picks one at random per launch)
    ports = [int(port)] if port else _detect_serena_ports()
    if not ports:
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

    errors: list[str] = []
    for p in ports:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{p}{endpoint}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except socket.timeout as e:
            console.print(f"[red]Bridge call timed out on port {p}: {e}[/red]"); return
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            errors.append(f"{p}: {e}")
            continue

        if payload.get("spawned"):
            console.print(f"[dim]Auto-spawned linked codex {payload.get('codex_sid', '')[:8]}.[/dim]")
        if payload.get("ok"):
            console.print(payload.get("response") or "(no response)")
            return
        msg = payload.get("message", "unknown error")
        errors.append(f"{p}: {msg}")
        if port or not _is_bridge_live_miss(msg):
            if payload.get("response"):
                console.print(payload["response"])
            break

    console.print(f"[red]{errors[-1] if errors else 'bridge failed'}[/red]")


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
        from core.linked_sessions import find_linked_session
    except ImportError:
        return None
    return find_linked_session(my_sid, target_agent)


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


def _is_bridge_live_miss(message: str) -> bool:
    msg = (message or "").lower()
    needles = (
        "no live terminal",
        "no live vte",
        "gtk window not initialized",
        "gtk shell not available",
        "pty_terminal unavailable",
    )
    return any(n in msg for n in needles)


def _detect_serena_ports() -> list[int]:
    """Find running Serena Flask instances on localhost, newest first.

    Cross-platform: uses `ss` on Linux + `netstat`/`Get-NetTCPConnection` on
    Windows. Falls back to a port-probe of common ranges if neither works.
    """
    import os, re, subprocess, sys, time
    candidates: list[tuple[float, int]] = []  # (start_time, port)

    if sys.platform == "linux":
        try:
            out = subprocess.check_output(["ss", "-tlnp"], text=True, timeout=2)
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return []
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
            return []
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
        return []
    ports: list[int] = []
    seen: set[int] = set()
    for _started, p in sorted(candidates, reverse=True):
        if p in seen:
            continue
        seen.add(p)
        ports.append(p)
    return ports


def _detect_serena_port() -> int | None:
    """Compatibility wrapper: newest running Serena port, if any."""
    ports = _detect_serena_ports()
    return ports[0] if ports else None


if __name__ == "__main__":
    main()
