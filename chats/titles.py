"""Generate short titles from first messages."""

import re

# XML-style tags that wrap noise in first messages
_XML_NOISE_TAGS = [
    "local-command-caveat",
    "local-command-stdout",
    "local_command",
    "command-name",
    "command-message",
    "command-args",
]

# Patterns that indicate an error/stack trace
# Order matters: specific error types first, generic fallbacks last.
_ERROR_PATTERNS = [
    (r"(?i)\bTraceback\b.*\bcall last\b", "Python traceback"),
    (r"(?i)\bTypeError:\s*(.{5,60})", "TypeError"),
    (r"(?i)\bReferenceError:\s*(.{5,60})", "ReferenceError"),
    (r"(?i)\bSyntaxError:\s*(.{5,60})", "SyntaxError"),
    (r"(?i)\bModuleNotFoundError:\s*(.{5,60})", "ModuleNotFoundError"),
    (r"(?i)\bImportError:\s*(.{5,60})", "ImportError"),
    (r"(?i)\bKeyError:\s*(.{5,60})", "KeyError"),
    (r"(?i)\bAttributeError:\s*(.{5,60})", "AttributeError"),
    (r"(?i)\bValueError:\s*(.{5,60})", "ValueError"),
    (r"(?i)\bRuntimeError:\s*(.{5,60})", "RuntimeError"),
    (r"(?i)\bNameError:\s*(.{5,60})", "NameError"),
    (r"(?i)\bConnectionError\b", "ConnectionError"),
    (r"(?i)\bECONNREFUSED\b", "connection refused"),
    (r"(?i)\bENOENT\b", "file not found"),
    (r"(?i)\bPermission denied\b", "permission denied"),
    (r"(?i)\bSegmentation fault\b", "segfault"),
    (r"(?i)\bpanic:", "Go panic"),
    (r"(?i)\bFATAL\s+ERROR\b", "fatal error"),
    (r"(?i)\bUnhandledPromiseRejection\b", "unhandled promise rejection"),
    (r"(?i)\bError:\s*(.{10,60})", None),  # generic Error: <msg> — must be after specific ones
    (r"(?i)at\s+\S+\s+\(.*:\d+:\d+\)", None),  # JS stack frame
]

# Patterns that look like API keys, tokens, hex dumps, UUIDs
_NOISE_LINE_RE = re.compile(
    r"^("
    r"[0-9a-fA-F]{24,}"  # long hex strings
    r"|pk_[0-9a-f]+"  # API keys
    r"|sk_[0-9a-f]+"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID
    r"|0x[0-9a-fA-F]+"  # hex literal
    r")(/\S*)?$"  # optional trailing path like /mcp
)

# Very short non-meaningful responses
_TRIVIAL_WORDS = {
    "yes", "no", "ok", "okay", "sure", "thanks", "ty", "thx", "nah",
    "yep", "yup", "nope", "hmm", "hm", "ah", "oh", "lol", "k", "kk",
    "y", "n", "yeah", "nah", "cool", "nice", "great", "done", "good",
    "true", "false", "idk", "idc", "maybe",
}


def generate_title(first_message: str) -> str:
    """Generate a concise title from a first message.

    Never returns "Untitled chat" if there is any text at all.
    """
    if not first_message:
        return "Untitled chat"

    text = first_message.strip()
    if not text:
        return "Untitled chat"

    # ── Phase 1: Extract slash command or strip XML noise ──────────
    # If the message is a slash command, extract the command name
    cmd_match = re.search(r"<command-name>\s*(/\S+)\s*</command-name>", text)
    cmd_msg_match = re.search(r"<command-message>\s*(\S+)\s*</command-message>", text)
    slash_cmd = None
    if cmd_match:
        slash_cmd = cmd_match.group(1)
    elif cmd_msg_match:
        slash_cmd = "/" + cmd_msg_match.group(1)

    text = _strip_xml_noise(text)
    if not text:
        if slash_cmd:
            return f"Ran {slash_cmd}"
        return "Chat session"

    # ── Phase 2: Handle special content types ──────────────────────

    # [Request interrupted by user...]
    if re.match(r"^\[Request interrupted by user", text):
        return "Interrupted request"

    # [tool result] prefix from parser
    if text.startswith("[tool result]"):
        text = text[len("[tool result]"):].strip()
        if not text:
            return "Tool result followup"

    # [Pasted text #N +X lines] — strip all occurrences, look for context
    had_pasted = bool(re.search(r"\[Pasted text(?:\s+#\d+)?(?:\s+\+\d+\s+lines)?\]", text))
    if had_pasted:
        text = re.sub(
            r"\[Pasted text(?:\s+#\d+)?(?:\s+\+\d+\s+lines)?\]\s*",
            "", text
        ).strip()
        if not text:
            return "Pasted content discussion"

    # ── Phase 3: Handle file paths ─────────────────────────────────
    # Quoted path like '/home/user/file.tar.gz'
    path_match = re.match(r"^['\"](.+?)['\"](.*)$", text, re.DOTALL)
    if path_match:
        path_str = path_match.group(1)
        rest = path_match.group(2).strip()
        if "/" in path_str or "\\" in path_str:
            filename = _extract_filename(path_str)
            if rest:
                # There's a question after the path
                rest_clean = _first_meaningful_line(rest)
                if rest_clean:
                    return _capitalize(_truncate(rest_clean, 50))
            return f"Working with {filename}"

    # Bare file path at start (not quoted but looks like /path/to/file or C:\...)
    bare_path_match = re.match(
        r"^([/~][\w./-]+|[a-zA-Z]:\\[\w.\\ /-]+)(.*)$", text, re.DOTALL
    )
    if bare_path_match:
        path_str = bare_path_match.group(1)
        rest = bare_path_match.group(2).strip()
        # Only treat as path if it has multiple segments
        if path_str.count("/") >= 2 or path_str.count("\\") >= 2:
            filename = _extract_filename(path_str)
            if rest:
                rest_clean = _first_meaningful_line(rest)
                if rest_clean:
                    return _capitalize(_truncate(rest_clean, 50))
            return f"Working with {filename}"

    # ── Phase 4: Detect error dumps / stack traces ─────────────────
    error_title = _detect_error(text)
    if error_title:
        return error_title

    # ── Phase 5: Skip noise lines, find real text ──────────────────
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    meaningful_lines = []
    for ln in lines:
        if _is_noise_line(ln):
            continue
        meaningful_lines.append(ln)

    if not meaningful_lines:
        # Everything was noise — use best effort
        if lines:
            return "Code session"
        return "Chat session"

    text = meaningful_lines[0]

    # ── Phase 6: Strip remaining markup ────────────────────────────
    # Markdown headers, bullets, bold markers
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"^\*{1,2}\s*", "", text)
    text = re.sub(r"^-\s*", "", text)
    # Remove leading > (quote marker)
    text = re.sub(r"^>\s*", "", text)
    text = text.strip()

    if not text:
        return "Chat session"

    # ── Phase 7: Slash commands (before short-word check) ───────────
    if text.startswith("/"):
        cmd = text.split()[0]
        rest = text[len(cmd):].strip()
        if rest:
            return _capitalize(_truncate(rest, 50))
        return f"Ran {cmd}"

    # ── Phase 8: URLs (pass remaining lines for context) ──────────
    if text.startswith("http://") or text.startswith("https://"):
        # Rejoin meaningful_lines so URL handler can see text on later lines
        return _handle_url("\n".join(meaningful_lines))

    # ── Phase 9: Handle numbered lists ─────────────────────────────
    # "1. Do X 2. Do Y" or "1) Do X"
    numbered_match = re.match(r"^\d+[.\)]\s*(.+)", text)
    if numbered_match:
        text = numbered_match.group(1)
        # If there's a "2." or "2)" later, truncate before it
        next_item = re.search(r"\s+\d+[.\)]\s", text)
        if next_item:
            text = text[:next_item.start()].strip()

    # ── Phase 10: Handle trivial/short messages ────────────────────
    words = text.split()
    if len(words) <= 2:
        lower = text.lower().rstrip("?!.,")
        if lower in _TRIVIAL_WORDS:
            return "Quick response"
        # Pure numbers (port numbers, line numbers, etc.)
        if re.match(r"^\d+$", text):
            return "Quick response"
        # Short but meaningful (like "anything uncommited?")
        if text:
            return _capitalize(text)

    # ── Phase 11: Console output with a question ───────────────────
    # If there are multiple lines and a later one looks like a question,
    # prefer the question
    if len(meaningful_lines) > 1:
        for ln in meaningful_lines:
            ln_clean = re.sub(r"^[\d.\)\-\*#>\s]+", "", ln).strip()
            if ln_clean and ln_clean.endswith("?"):
                return _capitalize(_truncate(ln_clean, 55))

    # ── Phase 12: Clean up inline URLs and final truncation ────────
    # Strip inline URLs that clutter the title
    text = re.sub(r"https?://\S+", "", text).strip()
    # Collapse whitespace after URL removal
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text:
        return "Link discussion"

    return _capitalize(_truncate(text, 55))


# ── Internal helpers ───────────────────────────────────────────────


def _strip_xml_noise(text: str) -> str:
    """Remove all XML-style noise tags and their content, returning real text."""
    # Remove known noise tags and their content
    for tag in _XML_NOISE_TAGS:
        # Remove full tag pairs: <tag>...</tag>
        text = re.sub(
            rf"<{re.escape(tag)}>.*?</{re.escape(tag)}>",
            "", text, flags=re.DOTALL
        )
        # Remove self-closing: <tag ... />
        text = re.sub(rf"<{re.escape(tag)}\s*[^>]*/>\s*", "", text)

    # Also strip any remaining angle-bracket tags that look like XML wrappers
    # but keep content outside of them
    text = text.strip()
    return text


def _first_meaningful_line(text: str) -> str:
    """Find the first non-empty, non-noise line in text."""
    for ln in text.split("\n"):
        ln = ln.strip()
        if ln and not _is_noise_line(ln):
            # Strip leading punctuation/whitespace cruft
            ln = re.sub(r"^[\s\-\*#>]+", "", ln).strip()
            if ln:
                return ln
    return ""


def _is_noise_line(line: str) -> bool:
    """Check if a line is noise (hex dump, UUID, API key, stack frame, etc.)."""
    line = line.strip()
    if not line:
        return True
    # API keys, hex dumps, UUIDs
    if _NOISE_LINE_RE.match(line):
        return True
    # Lines that are just file:line:col references (stack traces)
    if re.match(r"^\s*at\s+\S+\s+\(.*:\d+:\d+\)\s*$", line):
        return True
    # Lines that are just "^" caret pointers in error output
    if re.match(r"^\s*\^~*\s*$", line):
        return True
    # Pasted text markers
    if re.match(r"^\[Pasted text(?:\s+#\d+)?(?:\s+\+\d+\s+lines)?\]\s*$", line):
        return True
    return False


def _extract_filename(path_str: str) -> str:
    """Extract a meaningful filename from a file path."""
    path_str = path_str.strip().rstrip("/\\")
    parts = path_str.replace("\\", "/").split("/")
    filename = parts[-1] if parts else path_str
    # If filename is empty (trailing slash was stripped), go up
    if not filename and len(parts) > 1:
        filename = parts[-2]
    return filename or "file"


def _detect_error(text: str) -> str | None:
    """If the text looks like an error dump, return a title like 'Fixing ...'."""
    for pattern, label in _ERROR_PATTERNS:
        m = re.search(pattern, text)
        if m:
            if label:
                return f"Fixing {label}"
            # Try to extract the error message from a capture group
            if m.lastindex and m.lastindex >= 1:
                err_msg = m.group(1).strip().rstrip(".")
                # Clean up: if it starts with a quote or bracket, keep it short
                err_msg = _truncate(err_msg, 40)
                return f"Fixing {err_msg}"
            # Generic "error" line detected (like JS stack frame)
            return "Debugging error"
    return None


def _handle_url(text: str) -> str:
    """Generate title from a URL-prefixed message.

    text may be multiline (meaningful_lines joined with newlines).
    """
    url_match = re.match(r"https?://(?:www\.)?([^/\s]+)(?:/([^\s]*))?", text)
    if not url_match:
        return "Link discussion"

    domain = url_match.group(1)
    path = url_match.group(2) or ""

    # Collect all non-URL lines from the entire text as context
    all_lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    context_lines = [
        ln for ln in all_lines
        if not re.match(r"^https?://", ln)
    ]
    if context_lines:
        # Use the first non-URL line as the title
        first_ctx = re.sub(r"^[\s\-\*#>]+", "", context_lines[0]).strip()
        if first_ctx:
            return _capitalize(_truncate(first_ctx, 50))

    if "github.com" in domain and "/" in path:
        repo = "/".join(path.split("/")[:2])
        return f"Working with {repo}"

    return f"Link from {domain}"


def _truncate(text: str, max_len: int) -> str:
    """Truncate text at a natural boundary."""
    if len(text) <= max_len:
        return text

    # Try to break at sentence end
    for sep in [". ", "? ", "! ", "; ", ", "]:
        idx = text.rfind(sep, 0, max_len)
        if idx > max_len // 3:  # Don't truncate too aggressively
            result = text[:idx + 1].rstrip()
            # Strip trailing commas/semicolons (looks awkward as a title)
            result = result.rstrip(",;")
            # Add ellipsis if we truncated mid-thought (comma/semicolon)
            if sep in (", ", "; "):
                result += "..."
            return result

    # Break at word boundary
    idx = text.rfind(" ", 0, max_len)
    if idx > max_len // 3:
        return text[:idx] + "..."

    return text[:max_len] + "..."


def _capitalize(text: str) -> str:
    """Capitalize first letter without destroying the rest."""
    if not text:
        return text
    return text[0].upper() + text[1:]
