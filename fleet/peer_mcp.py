"""Attempt-scoped worker tools; intentionally no run/retry/claim/model control tools."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from fleet.collaboration import PeerStore
from fleet.learning import FleetLearning
from fleet.store import FleetStore

mcp = FastMCP(
    "serena_peer",
    instructions="Durable Fleet peer advice. Never grants authority to edit another worker's files.",
)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def context():
    store = FleetStore()
    peer = PeerStore(store)
    token = os.environ.get("SERENA_FLEET_PEER_TOKEN", "")
    return store, peer, token


@mcp.tool(annotations=WRITE)
def read_messages(acknowledge: list[str] | None = None) -> dict:
    """Get your roster and unacknowledged inbox. Acknowledge ids only after processing them."""
    _store, peer, token = context()
    result = peer.inbox(token, acknowledge=acknowledge)
    who = peer.identity(token)
    result["lesson_candidates"] = FleetLearning(_store).projection(who["run_id"])["candidates"]
    return result


@mcp.tool(annotations=WRITE)
def send_message(recipient: str, body: str, dedupe: str, reply_to: str | None = None) -> dict:
    """Send scoped advice. Use reply_to to answer a help request and a stable dedupe key on retries."""
    _store, peer, token = context()
    return peer.send(
        token,
        recipient,
        body,
        dedupe=dedupe,
        reply_to=reply_to,
        kind="reply" if reply_to else "info",
    )


@mcp.tool(annotations=WRITE)
def request_help(recipient: str, body: str, dedupe: str) -> dict:
    """Ask a peer to diagnose a concrete blocker; Fleet services this unattended, read-only, within 300s."""
    _store, peer, token = context()
    return peer.request_help(token, recipient, body, dedupe=dedupe)


@mcp.tool(annotations=WRITE)
def propose_lesson(summary: str, evidence_paths: list[str]) -> dict:
    """Propose a reusable project fact, backed by unchanged files in the integrated base checkout."""
    store, peer, token = context()
    return FleetLearning(store).propose(peer.identity(token), summary, evidence_paths)


@mcp.tool(annotations=WRITE)
def review_lesson(lesson_id: str, approve: bool, reason: str) -> dict:
    """Review phase only: independently verify another worker's candidate against its evidence files."""
    store, peer, token = context()
    return FleetLearning(store).review(peer.identity(token), lesson_id, approve, reason)


if __name__ == "__main__":
    mcp.run()
