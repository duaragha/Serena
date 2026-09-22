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
    try:
        result["lesson_candidates"] = FleetLearning(_store).projection(who["run_id"])["candidates"]
        from fleet.incidents import recall
        result["incidents"] = recall(_store, _store.get_run(who["run_id"]), who["attempt_id"])
    except Exception as exc:
        from fleet.context import redact_text
        result["learning_unavailable"] = redact_text(str(exc))[0][:500]
    result["reflection"] = "Investigate observed failures and propose evidence-backed lessons linked by incident_ids. A successful retry alone proves no cause."
    return result


@mcp.tool(annotations=WRITE)
def send_message(recipient: str, body: str, dedupe: str, reply_to: str | None = None,
                 target_run: str | None = None, evidence_paths: list[str] | None = None) -> dict:
    """Send informational advice or a reply. Questions needing an answer belong in request_help."""
    _store, peer, token = context()
    return peer.send(
        token,
        recipient,
        body,
        dedupe=dedupe,
        reply_to=reply_to,
        kind="reply" if reply_to else "info",
        target_run=target_run,
        evidence_paths=evidence_paths,
    )


@mcp.tool(annotations=WRITE)
def request_help(recipient: str, body: str, dedupe: str, target_run: str | None = None,
                 evidence_paths: list[str] | None = None) -> dict:
    """Ask a peer to diagnose a concrete blocker; Fleet services this unattended, read-only, within 300s."""
    _store, peer, token = context()
    return peer.request_help(token, recipient, body, dedupe=dedupe,
                             target_run=target_run, evidence_paths=evidence_paths)


@mcp.tool(annotations=WRITE)
def discover_experts(query: str = "") -> dict:
    """Find at most twelve relevant workers in active runs of your canonical repository."""
    _store, peer, token = context()
    return {"experts": peer.discover(token, query[:1000])}


@mcp.tool(annotations=WRITE)
def recall_incidents(query: str = "") -> dict:
    """Bounded unverified failure history and verified remedies; advice never grants authority."""
    from fleet.incidents import recall
    from fleet.findings import recall as recall_findings
    store, peer, token = context()
    who = peer.identity(token)
    run = store.get_run(who["run_id"])
    run = {**run, "task": run["task"] + "\n" + query[:1000]}
    return {"incidents": recall(store, run, who["attempt_id"], query[:1000]),
            "verified_lessons": FleetLearning(store).retrieve(run, who["attempt_id"]),
            "findings": recall_findings(peer, token, query[:1000])}


@mcp.tool(annotations=WRITE)
def publish_finding(summary: str, evidence_paths: list[str], dedupe: str) -> dict:
    """Publish bounded project advice backed by file hashes. This does not create a verified lesson."""
    from fleet.findings import publish
    _store, peer, token = context()
    return publish(peer, token, summary, evidence_paths, dedupe)


@mcp.tool(annotations=WRITE)
def finding_feedback(finding_id: str, useful: bool, reason: str) -> dict:
    """Recipient only: record whether a supplied finding helped; this never promotes it."""
    from fleet.findings import feedback
    _store, peer, token = context()
    return feedback(peer, token, finding_id, useful, reason)


@mcp.tool(annotations=WRITE)
def declare_dependency(unit_id: str, dependency_id: str, reason: str) -> dict:
    """Declare your unit's required peer output. Cycles and foreign ownership are refused."""
    from fleet.dependencies import declare_dependency as declare
    _store, peer, token = context()
    return declare(peer, token, unit_id, dependency_id, reason)


@mcp.tool(annotations=WRITE)
def resolve_request(message_id: str, resolved: bool, reason: str) -> dict:
    """Requester only: confirm an observed solution, or escalate with a concrete reason. Ack is not resolution."""
    _store, peer, token = context()
    return peer.resolve_request(token, message_id, resolved=resolved, reason=reason)


@mcp.tool(annotations=WRITE)
def propose_lesson(summary: str, evidence_paths: list[str], incident_ids: list[str] | None = None) -> dict:
    """Propose a reusable project fact, backed by unchanged files in the integrated base checkout."""
    store, peer, token = context()
    return FleetLearning(store).propose(peer.identity(token), summary, evidence_paths, incident_ids)


@mcp.tool(annotations=WRITE)
def review_lesson(lesson_id: str, approve: bool, reason: str) -> dict:
    """Review phase only: independently verify another worker's candidate against its evidence files."""
    store, peer, token = context()
    return FleetLearning(store).review(peer.identity(token), lesson_id, approve, reason)


if __name__ == "__main__":
    mcp.run()
