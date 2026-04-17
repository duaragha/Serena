"""Interactive TUI for browsing Claude Code conversations."""

from datetime import datetime, timezone
from pathlib import Path

from rich.markup import escape

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown as MarkdownWidget,
    Static,
)

from core.indexer import (
    add_tag,
    build_fts,
    delete_session,
    get_session,
    list_projects,
    list_sessions,
    remove_tag,
    search_fts,
    set_title,
    toggle_star,
    update_index,
)
from knowledge.reader import (
    delete_topic,
    format_size,
    get_topic_content,
    list_topics,
)
from memory.store import (
    add_memory,
    delete_memory,
    get_memory,
    list_memories,
    update_memory,
)
from core.config import ensure_session_visible, resolve_session_cwd
from core.parser import parse_full
from chats.watcher import ProjectsWatcher


def _time_group(timestamp_str: str | None) -> str:
    if not timestamp_str:
        return "Unknown"
    try:
        ts = datetime.fromisoformat(timestamp_str)
        now = datetime.now(timezone.utc) if ts.tzinfo else datetime.now()
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



def _shorten_cwd(cwd: str) -> str:
    if cwd.startswith("/home/raghav/Documents/Projects/"):
        return cwd[31:]
    elif cwd.startswith("/home/raghav"):
        return "~[lin]" + cwd[12:]
    elif cwd.startswith("C:\\Users\\ragha\\Projects\\"):
        return cwd[23:]
    elif cwd.startswith("C:\\Users\\ragha"):
        return "~[win]" + cwd[14:]
    return cwd


def _shorten_project(project: str) -> str:
    if project.startswith("C--Users-ragha-Projects-"):
        return project[24:]
    elif project.startswith("-home-raghav-Documents-Projects-"):
        return project[32:]
    elif project.startswith("-home-raghav"):
        return "~" + project[12:]
    elif project.startswith("C--Users-ragha"):
        return "~" + project[14:]
    return project


def _format_tokens(n: int) -> str:
    """Format token count as compact string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


# ── Modal screens ──────────────────────────────────────────────


class InputModal(ModalScreen[str]):
    """Modal that asks for text input."""

    DEFAULT_CSS = """
    InputModal {
        align: center middle;
    }
    #input-dialog {
        width: 60;
        height: auto;
        max-height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #input-dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, prompt: str, default: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="input-dialog"):
            yield Label(self.prompt)
            yield Input(value=self.default, id="modal-input")

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    @on(Input.Submitted, "#modal-input")
    def on_submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss("")


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        max-height: 8;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message)
            yield Label("[b]y[/b] = yes, [b]n[/b] / [b]Esc[/b] = cancel")

    def key_y(self) -> None:
        self.dismiss(True)

    def key_n(self) -> None:
        self.dismiss(False)

    def key_escape(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen):
    """Modal that displays all available keybindings."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 60;
        height: auto;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #help-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .help-row {
        margin: 0;
    }
    #help-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        bindings_info: list[tuple[str, str]] = []
        bindings_info.append(("Enter", "Open conversation"))
        for b in ChatsApp.BINDINGS:
            display = b.key_display if b.key_display else b.key.capitalize()
            bindings_info.append((display, b.description))

        with Vertical(id="help-dialog"):
            yield Static("Keybindings", id="help-title")
            for key, desc in bindings_info:
                yield Static(f"  {key:<12} {desc}", classes="help-row")
            yield Static("Press Escape or q to close", id="help-hint")

    def key_escape(self) -> None:
        self.dismiss()

    def key_q(self) -> None:
        self.dismiss()


# ── Memory screen ─────────────────────────────────────────────


class MemoryTypeModal(ModalScreen[str]):
    """Modal to pick a memory type."""

    DEFAULT_CSS = """
    MemoryTypeModal {
        align: center middle;
    }
    #type-dialog {
        width: 50;
        height: auto;
        max-height: 12;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #type-dialog Label {
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="type-dialog"):
            yield Label("Memory type:")
            yield Label(
                "[b]1[/b] general  [b]2[/b] user  [b]3[/b] feedback  "
                "[b]4[/b] project  [b]5[/b] reference"
            )

    _types = {"1": "general", "2": "user", "3": "feedback", "4": "project", "5": "reference"}

    def key_1(self) -> None:
        self.dismiss("general")

    def key_2(self) -> None:
        self.dismiss("user")

    def key_3(self) -> None:
        self.dismiss("feedback")

    def key_4(self) -> None:
        self.dismiss("project")

    def key_5(self) -> None:
        self.dismiss("reference")

    def key_escape(self) -> None:
        self.dismiss("")


class MemoryScreen(ModalScreen):
    """Full-screen overlay for managing memories."""

    DEFAULT_CSS = """
    MemoryScreen {
        background: $surface;
    }
    #memory-container {
        width: 100%;
        height: 100%;
    }
    #memory-title {
        dock: top;
        height: 3;
        background: $accent;
        color: $text;
        padding: 0 2;
        text-style: bold;
        content-align: left middle;
    }
    #memory-table {
        height: 1fr;
    }
    #memory-detail {
        height: 40%;
        border-top: thick $accent;
        padding: 1 2;
    }
    #memory-status {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("a", "add_memory", "Add"),
        Binding("e", "edit_memory", "Edit"),
        Binding("t", "change_type", "Type"),
        Binding("delete", "delete_memory", "Delete"),
        Binding("q", "go_back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.memories: list[dict] = []
        self.memory_ids: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-container"):
            yield Static("  Memories  (a)dd  (e)dit  (t)ype  (del)ete  (esc) back", id="memory-title")
            yield DataTable(id="memory-table", cursor_type="row", zebra_stripes=True)
            yield VerticalScroll(id="memory-detail")
            yield Static("", id="memory-status")

    def on_mount(self) -> None:
        self._refresh_table()

    def _refresh_table(self) -> None:
        self.memories = list_memories()
        self.memory_ids = []

        table = self.query_one("#memory-table", DataTable)
        table.clear(columns=True)
        table.add_columns("ID", "Type", "Content", "Updated")

        for m in self.memories:
            content_preview = m["content"][:80]
            if len(m["content"]) > 80:
                content_preview += "..."
            table.add_row(
                str(m["id"]),
                m["type"],
                content_preview,
                (m["updated_at"] or "")[:16],
                key=str(m["id"]),
            )
            self.memory_ids.append(m["id"])

        status = self.query_one("#memory-status", Static)
        status.update(f"  {len(self.memories)} memories")

        # Show first memory detail
        if self.memories:
            self._show_detail(self.memories[0])
        else:
            detail = self.query_one("#memory-detail", VerticalScroll)
            detail.remove_children()
            detail.mount(Static("No memories yet. Press (a) to add one."))

    def _get_selected_memory(self) -> dict | None:
        table = self.query_one("#memory-table", DataTable)
        if table.cursor_row is None or not self.memory_ids:
            return None
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self.memory_ids):
            return None
        mid = self.memory_ids[row_idx]
        return get_memory(mid)

    def _show_detail(self, memory: dict) -> None:
        detail = self.query_one("#memory-detail", VerticalScroll)
        detail.remove_children()
        detail.mount(Static(
            f"[bold]#{memory['id']}[/bold]  [dim]{memory['type']}[/dim]  "
            f"[dim]created {(memory['created_at'] or '')[:16]}  "
            f"updated {(memory['updated_at'] or '')[:16]}[/dim]\n\n"
            f"{escape(memory['content'])}"
        ))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        mem = self._get_selected_memory()
        if mem:
            self._show_detail(mem)

    def action_add_memory(self) -> None:
        def on_content(content: str) -> None:
            if not content.strip():
                return

            def on_type(mem_type: str) -> None:
                if not mem_type:
                    mem_type = "general"
                add_memory(content.strip(), mem_type)
                self.app.notify(f"Memory saved ({mem_type})")
                self._refresh_table()

            self.app.push_screen(MemoryTypeModal(), on_type)

        self.app.push_screen(InputModal("Memory content:"), on_content)

    def action_edit_memory(self) -> None:
        mem = self._get_selected_memory()
        if not mem:
            return

        def on_edit(new_content: str) -> None:
            if new_content.strip() and new_content.strip() != mem["content"]:
                update_memory(mem["id"], content=new_content.strip())
                self.app.notify(f"Memory #{mem['id']} updated")
                self._refresh_table()

        self.app.push_screen(InputModal("Edit memory:", mem["content"]), on_edit)

    def action_change_type(self) -> None:
        mem = self._get_selected_memory()
        if not mem:
            return

        def on_type(new_type: str) -> None:
            if new_type and new_type != mem["type"]:
                update_memory(mem["id"], mem_type=new_type)
                self.app.notify(f"Memory #{mem['id']} type → {new_type}")
                self._refresh_table()

        self.app.push_screen(MemoryTypeModal(), on_type)

    def action_delete_memory(self) -> None:
        mem = self._get_selected_memory()
        if not mem:
            return

        preview = mem["content"][:40] + ("..." if len(mem["content"]) > 40 else "")

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                delete_memory(mem["id"])
                self.app.notify(f"Memory #{mem['id']} deleted")
                self._refresh_table()

        self.app.push_screen(
            ConfirmModal(f"Delete memory #{mem['id']}?\n\"{preview}\""),
            on_confirm,
        )

    def action_go_back(self) -> None:
        self.dismiss()


# ── Knowledge screen ──────────────────────────────────────────


class KnowledgeScreen(ModalScreen):
    """Full-screen overlay for browsing the knowledge base."""

    DEFAULT_CSS = """
    KnowledgeScreen {
        background: $surface;
    }
    #knowledge-container {
        width: 100%;
        height: 100%;
    }
    #knowledge-title {
        dock: top;
        height: 3;
        background: $accent;
        color: $text;
        padding: 0 2;
        text-style: bold;
        content-align: left middle;
    }
    #knowledge-search {
        dock: top;
        height: 3;
        display: none;
        padding: 0 1;
    }
    #knowledge-search.visible {
        display: block;
    }
    #knowledge-panes {
        height: 1fr;
    }
    #knowledge-list {
        width: 38%;
        height: 100%;
        border-right: thick $accent;
    }
    #knowledge-detail {
        width: 1fr;
        height: 100%;
        padding: 1 2;
    }
    #knowledge-status {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("delete", "delete_topic", "Delete"),
        Binding("slash", "search_knowledge", "Search", key_display="/"),
        Binding("l", "link_topic", "Link to chat"),
        Binding("q", "go_back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.topics: list[dict] = []
        self.topic_slugs: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="knowledge-container"):
            yield Static("  Knowledge Base  (/) search  (l)ink  (del)ete  (esc) back", id="knowledge-title")
            yield Input(placeholder="Search knowledge...", id="knowledge-search")
            with Horizontal(id="knowledge-panes"):
                yield DataTable(id="knowledge-list", cursor_type="row", zebra_stripes=True)
                yield VerticalScroll(id="knowledge-detail")
            yield Static("", id="knowledge-status")

    def on_mount(self) -> None:
        from core.indexer import update_knowledge_index
        update_knowledge_index()
        self._refresh_table()

    def _refresh_table(self) -> None:
        from core.indexer import list_knowledge_topics
        self.topics = list_knowledge_topics()
        self.topic_slugs = []

        table = self.query_one("#knowledge-list", DataTable)
        table.clear(columns=True)
        table.add_columns("Topic", "Files", "Size", "Modified", "Links")

        for t in self.topics:
            from datetime import datetime
            mod = ""
            if t.get("modified"):
                mod = datetime.fromtimestamp(t["modified"]).strftime("%Y-%m-%d")
            table.add_row(
                t["title"][:40],
                str(t["file_count"]),
                format_size(t["total_size"]),
                mod,
                str(t.get("linked_sessions", 0)) if t.get("linked_sessions") else "",
                key=t["slug"],
            )
            self.topic_slugs.append(t["slug"])

        status = self.query_one("#knowledge-status", Static)
        status.update(f"  {len(self.topics)} topics")

        if self.topics:
            self._show_detail(self.topics[0])
        else:
            detail = self.query_one("#knowledge-detail", VerticalScroll)
            detail.remove_children()
            detail.mount(Static("No knowledge topics found."))

    def _get_selected_topic(self) -> dict | None:
        table = self.query_one("#knowledge-list", DataTable)
        if table.cursor_row is None or not self.topic_slugs:
            return None
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self.topic_slugs):
            return None
        slug = self.topic_slugs[row_idx]
        for t in self.topics:
            if t["slug"] == slug:
                return t
        return None

    def _show_detail(self, topic: dict) -> None:
        from core.indexer import get_topic_sessions
        detail = self.query_one("#knowledge-detail", VerticalScroll)
        detail.remove_children()

        desc = topic.get("description", "")
        if len(desc) > 120:
            desc = desc[:120] + "..."

        header = (
            f"[bold]{escape(topic['title'])}[/bold]  "
            f"[dim]{topic['slug']}  |  {topic['file_count']} files  |  "
            f"{format_size(topic['total_size'])}[/dim]\n"
        )
        if desc:
            header += f"[dim]{escape(desc)}[/dim]\n"

        detail.mount(Static(header))

        # Show linked sessions
        linked = get_topic_sessions(topic["slug"])
        if linked:
            detail.mount(Static(f"\n[bold]Linked Chats ({len(linked)}):[/bold]"))
            for s in linked[:5]:
                detail.mount(Static(
                    f"  [dim]{s['session_id'][:8]}[/dim]  {escape(s['display_title'])}  "
                    f"[dim]{(s.get('first_timestamp') or '')[:10]}[/dim]"
                ))

        detail.mount(Static("\n" + "─" * 60))

        content = get_topic_content(topic["slug"])
        detail.mount(Static(content, markup=False))
        detail.scroll_home(animate=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        topic = self._get_selected_topic()
        if topic:
            self._show_detail(topic)

    def action_search_knowledge(self) -> None:
        search_bar = self.query_one("#knowledge-search", Input)
        search_bar.toggle_class("visible")
        if search_bar.has_class("visible"):
            search_bar.focus()
            search_bar.value = ""
        else:
            self._refresh_table()

    @on(Input.Submitted, "#knowledge-search")
    def on_knowledge_search(self, event: Input.Submitted) -> None:
        from core.indexer import search_knowledge_fts
        query = event.value.strip()
        if not query:
            self._refresh_table()
            return

        results = search_knowledge_fts(query, limit=30)
        # Group by topic
        seen_slugs = set()
        filtered_topics = []
        for r in results:
            if r["topic_slug"] not in seen_slugs:
                seen_slugs.add(r["topic_slug"])
                for t in self.topics:
                    if t["slug"] == r["topic_slug"]:
                        filtered_topics.append(t)
                        break

        table = self.query_one("#knowledge-list", DataTable)
        table.clear(columns=True)
        table.add_columns("Topic", "Files", "Size", "Modified", "Links")
        self.topic_slugs = []

        for t in filtered_topics:
            from datetime import datetime
            mod = ""
            if t.get("modified"):
                mod = datetime.fromtimestamp(t["modified"]).strftime("%Y-%m-%d")
            table.add_row(
                t["title"][:40],
                str(t["file_count"]),
                format_size(t["total_size"]),
                mod,
                str(t.get("linked_sessions", 0)) if t.get("linked_sessions") else "",
                key=t["slug"],
            )
            self.topic_slugs.append(t["slug"])

        status = self.query_one("#knowledge-status", Static)
        status.update(f"  Search: '{query}' — {len(filtered_topics)} topics")
        self.query_one("#knowledge-list", DataTable).focus()

    def action_link_topic(self) -> None:
        """Link the selected topic to a recent chat session."""
        from core.indexer import link_session_topic, list_sessions
        topic = self._get_selected_topic()
        if not topic:
            return

        # Show input for session ID
        def on_input(sid_input: str) -> None:
            sid = sid_input.strip()
            if not sid:
                return
            try:
                link_session_topic(sid, topic["slug"], "manual")
                self.app.notify(f"Linked '{topic['title']}' to {sid[:8]}")
                self._show_detail(topic)
            except Exception as e:
                self.app.notify(str(e), severity="error")

        self.app.push_screen(InputModal("Session ID to link:"), on_input)

    def action_delete_topic(self) -> None:
        topic = self._get_selected_topic()
        if not topic:
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                delete_topic(topic["slug"])
                self.app.notify(f"Deleted '{topic['title']}'")
                self._refresh_table()

        self.app.push_screen(
            ConfirmModal(
                f"Delete '{topic['title']}'?\n"
                f"This removes all {topic['file_count']} files in {topic['slug']}/"
            ),
            on_confirm,
        )

    def action_go_back(self) -> None:
        self.dismiss()


# ── Main app ───────────────────────────────────────────────────


class ChatsApp(App):
    """Interactive browser for Claude Code conversations."""

    TITLE = "Serena"

    CSS = """
    Screen {
        background: $surface;
    }

    #status {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 2;
        margin-bottom: 1;
    }

    #search-bar {
        dock: top;
        height: 3;
        display: none;
        padding: 0 1;
    }
    #search-bar.visible {
        display: block;
    }

    #main-panes {
        height: 1fr;
    }

    #left-pane {
        width: 50%;
        height: 100%;
        border-right: thick $accent;
    }

    #right-pane {
        width: 1fr;
        height: 100%;
    }

    #sessions-table {
        height: 1fr;
    }

    #conv-header {
        dock: top;
        height: 3;
        background: $accent;
        color: $text;
        padding: 0 2;
        text-style: bold;
    }

    #conv-body {
        height: 1fr;
        padding: 1 2;
    }

    .msg-user {
        color: $success;
        text-style: bold;
        margin-top: 1;
    }
    .msg-assistant {
        color: $primary;
        text-style: bold;
        margin-top: 1;
    }
    .msg-text {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "star", "Star"),
        Binding("r", "rename", "Rename"),
        Binding("t", "tag", "Tag"),
        Binding("slash", "search", "Search", key_display="/"),
        Binding("p", "filter_project", "Project"),
        Binding("d", "filter_device", "Device"),
        Binding("a", "show_all", "All"),
        Binding("x", "refresh_index", "Refresh"),
        Binding("space", "toggle_select", "Select", show=False),
        Binding("delete", "delete", "Delete"),
        Binding("o", "resume", "Resume"),
        Binding("n", "new_chat", "New"),
        Binding("w", "remote_control", "Web/RC"),
        Binding("m", "memory", "Memory"),
        Binding("k", "knowledge", "Knowledge"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("open_bracket", "resize_left", "Shrink", show=False),
        Binding("close_bracket", "resize_right", "Grow", show=False),
        Binding("escape", "focus_table", "Back", show=False),
        Binding("tab", "focus_conversation", "View chat", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[dict] = []
        self.session_ids: list[str] = []  # parallel to table rows
        self.current_filter_project: str | None = None
        self.current_filter_device: str | None = None
        self.search_mode = False
        self._last_loaded_sid: str | None = None
        self._rebuilding = False
        self._selected: set[str] = set()  # multi-selected session IDs
        self._left_pane_width = 38  # percentage
        self._watcher: ProjectsWatcher | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search conversations...", id="search-bar")
        with Horizontal(id="main-panes"):
            with Vertical(id="left-pane"):
                yield DataTable(id="sessions-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right-pane"):
                yield Static("", id="conv-header")
                yield VerticalScroll(id="conv-body")
        yield Static("Loading...", id="status")
        yield Footer()

    @work(thread=True)
    def on_mount(self) -> None:
        self.app.call_from_thread(self._set_status, "Indexing...")
        update_index()
        from core.indexer import update_knowledge_index
        update_knowledge_index()
        self.app.call_from_thread(self._load_sessions)
        self.app.call_from_thread(self._focus_table)
        self._start_watcher()

    def _start_watcher(self) -> None:
        if self._watcher is not None:
            return
        watcher = ProjectsWatcher(on_change=self._on_projects_changed)
        try:
            watcher.start()
        except Exception:
            return
        self._watcher = watcher

    def _on_projects_changed(self) -> None:
        """Fired (debounced) by the fs watcher when a jsonl file changes."""
        self._refresh_from_disk()

    @work(thread=True, exclusive=True, group="fs-refresh")
    def _refresh_from_disk(self) -> None:
        update_index()
        self.app.call_from_thread(
            self._load_sessions,
            self.current_filter_project,
            self.current_filter_device,
        )

    def on_unmount(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _load_sessions(
        self,
        project: str | None = None,
        device: str | None = None,
    ) -> None:
        self.current_filter_project = project
        self.current_filter_device = device
        self.sessions = list_sessions(
            project=project, device=device, limit=200,
        )
        self._rebuild_table()

    def _add_session_row(
        self, table: DataTable, s: dict, group_key: str,
    ) -> None:
        """Add a single session row to the table."""
        selected = s["session_id"] in self._selected
        star = "[x]" if selected else ("*" if s.get("starred") else "")
        sid = s["session_id"][:8]
        title = s.get("display_title", "Untitled")[:50]
        display_cwd = s.get("last_cwd") or s.get("cwd") or ""
        project = _shorten_cwd(display_cwd) if display_cwd else _shorten_project(s.get("project_dir", ""))
        mdate = (s.get("last_timestamp") or "")[:10]
        cdate = (s.get("first_timestamp") or "")[:10]
        total_tokens = (s.get("input_tokens") or 0) + (s.get("output_tokens") or 0) + (s.get("cache_read_tokens") or 0) + (s.get("cache_create_tokens") or 0)
        tokens = _format_tokens(total_tokens) if total_tokens else ""

        table.add_row(star, sid, title, project, mdate, cdate, tokens, key=s["session_id"])
        self.session_ids.append(s["session_id"])

    def _rebuild_table(self) -> None:
        self._rebuilding = True
        table = self.query_one("#sessions-table", DataTable)
        table.clear(columns=True)

        table.add_columns("", "ID", "Title", "Project", "M.Date", "C.Date", "Tokens")
        self.session_ids = []

        # Split starred from the rest
        starred = [s for s in self.sessions if s.get("starred")]
        unstarred = [s for s in self.sessions if not s.get("starred")]

        group_counter = 0

        # ── Starred section (always at top) ──
        if starred:
            group_counter += 1
            table.add_row("", "", "── * Starred ──", "", "", "", "", key=f"group-{group_counter}-starred")
            self.session_ids.append(None)

            for s in starred:
                self._add_session_row(table, s, "starred")

        # ── Time-grouped sections with project sub-groups ──
        # Group unstarred by time
        time_groups: dict[str, list[dict]] = {}
        for s in unstarred:
            group = _time_group(s.get("last_timestamp") or s.get("first_timestamp"))
            time_groups.setdefault(group, []).append(s)

        for time_label, sessions_in_group in time_groups.items():
            group_counter += 1
            table.add_row("", "", f"── {time_label} ──", "", "", "", "", key=f"group-{group_counter}-{time_label}")
            self.session_ids.append(None)

            # Sub-group by project within this time group
            project_groups: dict[str, list[dict]] = {}
            for s in sessions_in_group:
                proj = _shorten_project(s.get("project_dir", "")) or "~"
                project_groups.setdefault(proj, []).append(s)

            if len(project_groups) > 1:
                # Multiple projects — show sub-headers
                for proj_name, proj_sessions in project_groups.items():
                    group_counter += 1
                    table.add_row("", "", f"   {proj_name}", "", "", "", "", key=f"group-{group_counter}-{time_label}-{proj_name}")
                    self.session_ids.append(None)
                    for s in proj_sessions:
                        self._add_session_row(table, s, f"{time_label}-{proj_name}")
            else:
                # Single project — no sub-header needed
                for s in sessions_in_group:
                    self._add_session_row(table, s, time_label)

        # Update status
        filters = []
        if self.current_filter_project:
            filters.append(f"project: {self.current_filter_project}")
        if self.current_filter_device:
            filters.append(f"device: {self.current_filter_device}")
        filter_str = f"  [{', '.join(filters)}]" if filters else ""
        sel_str = f"  [{len(self._selected)} selected]" if self._selected else ""
        self._set_status(f"{len(self.sessions)} conversations{filter_str}{sel_str}  |  Press ? for help")

        # Reset conversation pane
        self._last_loaded_sid = None
        self._rebuilding = False
        if self.session_ids:
            # Move cursor to first non-group row but don't auto-load
            for i, sid in enumerate(self.session_ids):
                if sid is not None:
                    table.move_cursor(row=i)
                    break
        self._clear_conversation()

    def _get_selected_session(self) -> dict | None:
        table = self.query_one("#sessions-table", DataTable)
        if table.cursor_row is None:
            return None
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self.session_ids):
            return None
        sid = self.session_ids[row_idx]
        if sid is None:
            return None
        return get_session(sid)

    def _clear_conversation(self) -> None:
        """Clear the right pane conversation display."""
        header = self.query_one("#conv-header", Static)
        header.update("")
        body = self.query_one("#conv-body", VerticalScroll)
        body.remove_children()
        body.mount(Static("Select a conversation", classes="msg-text"))

    def _load_conversation_for_row(self) -> None:
        """Load conversation for the currently highlighted row."""
        table = self.query_one("#sessions-table", DataTable)
        if table.cursor_row is None:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self.session_ids):
            return
        sid = self.session_ids[row_idx]
        if sid is None:
            # Group header row - don't change the conversation
            return
        if sid == self._last_loaded_sid:
            # Already showing this conversation
            return
        self._last_loaded_sid = sid

        session = get_session(sid)
        if not session:
            return

        # Update the header
        title = session.get("display_title", "Untitled")
        star = "* " if session.get("starred") else ""
        short_id = session["session_id"][:8]
        date = (session.get("first_timestamp") or "")[:16].replace("T", " ")
        inp = session.get("input_tokens") or 0
        out = session.get("output_tokens") or 0
        cache_r = session.get("cache_read_tokens") or 0
        cache_w = session.get("cache_create_tokens") or 0
        total = inp + out + cache_r + cache_w
        token_str = f"  |  {_format_tokens(total)} tokens (in:{_format_tokens(inp)} out:{_format_tokens(out)} cache:{_format_tokens(cache_r + cache_w)})" if total else ""
        header = self.query_one("#conv-header", Static)
        header.update(escape(f"{star}{title}  ({short_id})  {date}{token_str}"))

        # Clear the body and show loading indicator
        body = self.query_one("#conv-body", VerticalScroll)
        body.remove_children()
        body.mount(Static("Loading...", classes="msg-text"))

        self._do_load_conversation(session)

    @work(thread=True, exclusive=True, group="conv-loader")
    def _do_load_conversation(self, session: dict) -> None:
        """Load conversation messages in a background thread."""
        target_sid = session["session_id"]
        file_path = Path(session["file_path"])

        if not file_path.exists():
            self.app.call_from_thread(self._render_conversation, target_sid, [("error", "File not found.")])
            return

        messages = parse_full(file_path)
        rendered: list[tuple[str, str]] = []
        for msg in messages:
            if msg.role == "user":
                rendered.append(("user", msg.text))
            elif msg.role == "assistant" and msg.text and not msg.tool_name:
                rendered.append(("assistant", msg.text))

        self.app.call_from_thread(self._render_conversation, target_sid, rendered)

    def _render_conversation(self, target_sid: str, messages: list[tuple[str, str]]) -> None:
        """Render parsed messages into the conversation body."""
        if self._last_loaded_sid != target_sid:
            # User has moved on to a different row, discard
            return

        body = self.query_one("#conv-body", VerticalScroll)
        body.remove_children()

        if not messages:
            body.mount(Static("No messages found.", classes="msg-text"))
            return

        for role, text in messages:
            if role == "user":
                body.mount(Static("You", classes="msg-user"))
                body.mount(Static(text, markup=False, classes="msg-text"))
            elif role == "assistant":
                body.mount(Static("Claude", classes="msg-assistant"))
                body.mount(Static(text, markup=False, classes="msg-text"))
            elif role == "error":
                body.mount(Static(text, markup=False, classes="msg-text"))

        body.scroll_home(animate=False)

    # ── Cursor movement triggers conversation load ──

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """When the cursor moves to a new row, load that conversation."""
        if self._rebuilding:
            return
        self._load_conversation_for_row()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key - same behavior, ensures conversation is loaded."""
        self._load_conversation_for_row()

    # ── Actions ──

    def action_star(self) -> None:
        session = self._get_selected_session()
        if not session:
            return
        is_starred = toggle_star(session["session_id"][:8])
        self.notify("Starred" if is_starred else "Unstarred")
        self._last_loaded_sid = None
        self._load_sessions(self.current_filter_project, self.current_filter_device)

    def action_rename(self) -> None:
        session = self._get_selected_session()
        if not session:
            return

        current = session.get("display_title", "")

        def on_rename(new_title: str) -> None:
            if new_title.strip():
                set_title(session["session_id"], new_title.strip())
                self.notify(f"Renamed to '{new_title.strip()}'")
                self._last_loaded_sid = None
                self._load_sessions(self.current_filter_project, self.current_filter_device)

        self.push_screen(InputModal("Rename conversation:", current), on_rename)

    def action_tag(self) -> None:
        session = self._get_selected_session()
        if not session:
            return

        existing = ", ".join(session.get("tags", []))

        def on_tag(tag_input: str) -> None:
            if not tag_input.strip():
                return
            tag_name = tag_input.strip()
            if tag_name.startswith("-"):
                tag_name = tag_name[1:]
                try:
                    remove_tag(session["session_id"], tag_name)
                    self.notify(f"Removed tag '{tag_name}'")
                except ValueError:
                    self.notify(f"Tag '{tag_name}' not found", severity="error")
            else:
                add_tag(session["session_id"], tag_name)
                self.notify(f"Tagged with '{tag_name}'")
            self._last_loaded_sid = None
            self._load_sessions(self.current_filter_project, self.current_filter_device)

        prompt = "Add tag (prefix with - to remove):"
        if existing:
            prompt += f"\nCurrent: {existing}"
        self.push_screen(InputModal(prompt), on_tag)

    def action_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.toggle_class("visible")
        if search_bar.has_class("visible"):
            search_bar.focus()
            search_bar.value = ""
            self.search_mode = True
        else:
            self.search_mode = False
            self._load_sessions(self.current_filter_project, self.current_filter_device)

    @on(Input.Submitted, "#search-bar")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            self.search_mode = False
            search_bar = self.query_one("#search-bar", Input)
            search_bar.remove_class("visible")
            self._load_sessions(self.current_filter_project, self.current_filter_device)
            return

        self._run_search(query)

    @work(thread=True)
    def _run_search(self, query: str) -> None:
        self.app.call_from_thread(self._set_status, "Searching...")
        results = search_fts(query, limit=50)
        if not results:
            self.app.call_from_thread(self._set_status, "Building search index...")
            build_fts()
            results = search_fts(query, limit=50)

        seen = set()
        sessions = []
        for r in results:
            sid = r["session_id"]
            if sid in seen:
                continue
            seen.add(sid)
            session = get_session(sid)
            if session:
                sessions.append(session)

        self.sessions = sessions
        self.app.call_from_thread(self._rebuild_table)
        self.app.call_from_thread(
            self._set_status,
            f"Search: '{query}' — {len(sessions)} conversations found",
        )
        self.app.call_from_thread(self._focus_table)

    def _focus_table(self) -> None:
        self.query_one("#sessions-table", DataTable).focus()

    def action_filter_project(self) -> None:
        projects = list_projects()
        project_names = [_shorten_project(p["project_dir"]) for p in projects]
        prompt = "Filter by project:\n" + ", ".join(project_names)

        def on_project(value: str) -> None:
            if value.strip():
                self._load_sessions(project=value.strip(), device=self.current_filter_device)
            else:
                self._load_sessions(device=self.current_filter_device)

        self.push_screen(InputModal(prompt), on_project)

    def action_filter_device(self) -> None:
        if self.current_filter_device == "linux":
            self._load_sessions(self.current_filter_project, device="windows")
        elif self.current_filter_device == "windows":
            self._load_sessions(self.current_filter_project, device=None)
        else:
            self._load_sessions(self.current_filter_project, device="linux")

    def action_show_all(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.remove_class("visible")
        self.search_mode = False
        self._selected.clear()
        self._load_sessions()

    def action_toggle_select(self) -> None:
        """Toggle selection on current row."""
        session = self._get_selected_session()
        if not session:
            return
        sid = session["session_id"]
        if sid in self._selected:
            self._selected.discard(sid)
        else:
            self._selected.add(sid)
        # Refresh table to show selection markers
        self._rebuild_table()

    def action_delete(self) -> None:
        # If multi-selected, delete all selected
        if self._selected:
            count = len(self._selected)

            def on_confirm(confirmed: bool | None) -> None:
                if confirmed:
                    deleted = 0
                    for sid in list(self._selected):
                        try:
                            delete_session(sid)
                            deleted += 1
                        except ValueError:
                            pass
                    self._selected.clear()
                    self.notify(f"Deleted {deleted} conversations")
                    self._last_loaded_sid = None
                    self._load_sessions(self.current_filter_project, self.current_filter_device)

            self.push_screen(
                ConfirmModal(f"Delete {count} selected conversations?\nThis will permanently remove the chat files."),
                on_confirm,
            )
        else:
            # Single delete
            session = self._get_selected_session()
            if not session:
                return
            title = session.get("display_title", "Untitled")
            sid = session["session_id"][:8]

            def on_confirm_single(confirmed: bool | None) -> None:
                if confirmed:
                    try:
                        delete_session(session["session_id"])
                        self.notify(f"Deleted '{title}'")
                        self._last_loaded_sid = None
                        self._load_sessions(self.current_filter_project, self.current_filter_device)
                    except ValueError as e:
                        self.notify(str(e), severity="error")

            self.push_screen(
                ConfirmModal(f"Delete '{title}' ({sid})?\nThis will permanently remove the chat file."),
                on_confirm_single,
            )

    def _is_project_chat(self, session: dict) -> bool:
        """Check if a chat is from a project directory (not just ~)."""
        project = session.get("project_dir", "")
        # Home-only chats have project_dir like "-home-raghav" or "C--Users-ragha"
        # Project chats have deeper paths
        return project not in ("-home-raghav", "C--Users-ragha") and project != ""

    def action_resume(self) -> None:
        """Resume chat — exits TUI and launches claude in same terminal."""
        session = self._get_selected_session()
        if not session:
            return

        sid = session["session_id"]
        cwd = resolve_session_cwd(session.get("last_cwd") or session.get("cwd"))
        ensure_session_visible(sid, session.get("project_dir", ""), cwd)
        title = session.get("display_title", "Chat")

        if self._is_project_chat(session):
            self.exit(result=("exec", cwd, ["claude", "--dangerously-skip-permissions", "--remote-control", title, "-r", sid]))
        else:
            self.exit(result=("exec", cwd, ["claude", "--dangerously-skip-permissions", "-r", sid]))

    def action_new_chat(self) -> None:
        """Exit TUI and open a new claude chat in the directory chats was launched from."""
        import os
        cwd = os.environ.get("CHATS_LAUNCH_DIR", str(Path.home()))
        self.exit(result=("exec", cwd, ["claude", "--dangerously-skip-permissions"]))

    def action_remote_control(self) -> None:
        """Force Remote Control for any chat."""
        session = self._get_selected_session()
        if not session:
            return

        sid = session["session_id"]
        cwd = resolve_session_cwd(session.get("last_cwd") or session.get("cwd"))
        ensure_session_visible(sid, session.get("project_dir", ""), cwd)
        title = session.get("display_title", "Chat")

        self.exit(result=("exec", cwd, ["claude", "--dangerously-skip-permissions", "--remote-control", title, "-r", sid]))

    def action_memory(self) -> None:
        """Open the memory manager screen."""
        self.push_screen(MemoryScreen())

    def action_knowledge(self) -> None:
        """Open the knowledge base browser."""
        self.push_screen(KnowledgeScreen())

    def _apply_pane_width(self) -> None:
        self.query_one("#left-pane").styles.width = f"{self._left_pane_width}%"

    def action_resize_left(self) -> None:
        """Shrink left pane."""
        self._left_pane_width = max(20, self._left_pane_width - 5)
        self._apply_pane_width()

    def action_resize_right(self) -> None:
        """Grow left pane."""
        self._left_pane_width = min(70, self._left_pane_width + 5)
        self._apply_pane_width()

    def action_focus_table(self) -> None:
        """Return focus to the sessions table."""
        self.query_one("#sessions-table", DataTable).focus()

    def action_focus_conversation(self) -> None:
        """Jump focus to the conversation pane for scrolling."""
        self.query_one("#conv-body", VerticalScroll).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh_index(self) -> None:
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        self.app.call_from_thread(self._set_status, "Refreshing index...")
        new, updated = update_index()
        self.app.call_from_thread(self._set_status, f"Refreshed: {new} new, {updated} updated")
        self.app.call_from_thread(
            self._load_sessions, self.current_filter_project, self.current_filter_device
        )


def run():
    import os
    import subprocess
    import sys
    os.environ["CHATS_LAUNCH_DIR"] = os.getcwd()
    app = ChatsApp()
    result = app.run()
    if result and isinstance(result, tuple) and result[0] == "exec":
        _, cwd, args = result
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        if sys.platform == "win32":
            # cmd.exe: quote cwd to prevent forward-slash paths from being
            # parsed as flags, then run claude and drop into a fresh prompt.
            args_str = subprocess.list2cmdline(args)
            os.execvp("cmd", ["cmd", "/k", f'cd /d "{cwd}" && {args_str}'])
        else:
            import shlex
            args_str = " ".join(shlex.quote(a) for a in args)
            # cd to dir, run claude, then stay in that directory with a new shell
            os.execvp("bash", ["bash", "-c", f"cd {shlex.quote(cwd)} && {args_str}; exec bash"])
