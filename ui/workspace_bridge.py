"""Prefer an existing structured owner without creating or focusing a pane."""

import hashlib
import time
from uuid import UUID, uuid4

from flask import current_app, request

from ui.workspace_web import local_workspace_request


def structured_bridge(provider, sid, prompt, timeout):
    host = current_app.extensions.get("workspace_host")
    if host is None:
        return None
    if local_workspace_request() is not None:
        return {"ok": False, "response": "", "message": "Structured bridge is local-only"}
    data = request.get_json(silent=True) or {}
    request_id = data.get("request_id") or str(uuid4())
    try:
        result = host.bridge(sid, provider, prompt, request_id, timeout=timeout)
        return None if result is None else {**result, "request_id": request_id}
    except (ValueError, RuntimeError) as error:
        # Once a structured owner is involved, never retry through a terminal.
        return {"ok": False, "response": "", "message": str(error)}


def _work_host(sid):
    host = current_app.extensions.get("workspace_host")
    if host is not None and (host.journal.has_pending_work(sid) or any(
        row.get("sid") == sid for row in host.runtime_context_snapshot()["runtimes"]
    )):
        return host
    return None


def structured_work_bridge(sid, prompt, item_id, dispatch_id, timeout):
    """Keep accepted-job dispatch on its native owner and original rollout slice."""
    from core import codex_bridge as bridge

    committed, reserved, start = False, False, None
    try:
        host = _work_host(sid)
        if host is None:
            return None
        if not isinstance(dispatch_id, str) or str(UUID(dispatch_id)) != dispatch_id:
            raise ValueError("Native work requires a stable dispatch UUID")
        unsafe = bridge._unsafe_work_metadata(sid)
        if unsafe:
            raise ValueError(unsafe)
        path = bridge.find_codex_jsonl(sid)
        if path is None:
            raise ValueError("Native work transcript was not found")
        reserved = True  # A reservation timeout does not prove acquisition failed.
        result = host.reserve_work(sid, item_id)
        if not result.get("ok"):
            reserved = False
            return bridge._work_result(ok=False, message=result.get("message", "Native reservation refused"))
        unsafe = bridge._unsafe_work_metadata(sid)
        if unsafe:
            reserved = not host.release_work(sid, item_id)
            return bridge._work_result(ok=False, message=unsafe, reserved=reserved)
        start = bridge._line_count(path)
        committed = True  # Preserve uncertainty if acknowledgement is lost.
        submission = host.submit_work(sid, item_id, prompt, dispatch_id, start_offset=start)
        committed = bool(submission.get("committed", submission.get("pending", False)))
        start = submission.get("start_offset", start)
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        if not submission.get("ok"):
            if committed:
                bridge._mark_route_dispatch(item_id, "uncertain", start_offset=start,
                                            end_offset=bridge._line_count(path), prompt_sha256=digest)
            else:
                reserved = not host.release_work(sid, item_id)
            return bridge._work_result(ok=False, committed=committed, reserved=reserved,
                message=submission.get("message") or submission.get("error") or "Native dispatch is unconfirmed",
                start_offset=start, end_offset=bridge._line_count(path))
        if type(start) is not int or start < 0:
            raise ValueError("Native dispatch has no confirmed original transcript boundary")
        bridge._mark_route_dispatch(item_id, "committed", start_offset=start, end_offset=None, prompt_sha256=digest)
        result = bridge._collect_work_response(path, start, digest, timeout,
                                              {"kind": "workspace", "host": host, "sid": sid})
        if result.get("finished"):
            deadline = time.monotonic() + 2
            completion = host.journal.turn_completion(sid, submission["turn_id"])
            while completion is None and time.monotonic() < deadline:
                time.sleep(0.05)
                completion = host.journal.turn_completion(sid, submission["turn_id"])
            if completion is None:
                result = {**result, "ok": False, "finished": False,
                          "message": "Exact native completion is not yet confirmed"}
            elif completion.get("status") != "completed":
                error = completion.get("error") or {}
                result = {**result, "ok": False,
                          "message": error.get("message") or f"Native turn {completion.get('status', 'unconfirmed')}"}
        end = bridge._line_count(path)
        bridge._mark_route_dispatch(item_id, "completed" if result.get("ok") else "uncertain",
                                    start_offset=start, end_offset=end, prompt_sha256=digest)
        if result.get("finished"):
            reserved = not host.release_work(sid, item_id)
        return bridge._work_result(ok=bool(result.get("ok")), committed=True, reserved=reserved,
            response=result.get("response", ""), message=result.get("message", "Native work finished"),
            start_offset=start, end_offset=end)
    except Exception as error:
        # Never route around a native reservation or an uncertain native submit.
        return bridge._work_result(ok=False, message=str(error), committed=committed,
                                    reserved=reserved, start_offset=start)


def structured_work_interrupt(sid, item_id):
    try:
        host = _work_host(sid)
        return None if host is None else host.interrupt_work(sid, item_id)
    except Exception as error:
        return {"ok": False, "message": str(error)}
