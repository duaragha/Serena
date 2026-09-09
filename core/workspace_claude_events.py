"""Translate Claude SDK records into the shared pane schema, retaining originals."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass


def plain(value):
    return asdict(value) if is_dataclass(value) else deepcopy(value)


class ClaudeEvents:
    def __init__(self, sid):
        self.sid = sid
        self.turn = None
        self.message_ids = {}
        self.tools = {}

    def event(self, method, params):
        return {"method": method, "params": {"threadId": self.sid, **params}}

    def blocks(self, content, message_id, *, user=False):
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if user and not any(b.get("type") == "tool_result" for b in content):
            return [{"id": message_id, "type": "userMessage", "content": content}]
        items = []
        for index, block in enumerate(content):
            item = {"id": f"{message_id}:{index}", "providerOriginal": deepcopy(block)}
            # asdict(SDK content blocks) does not carry a wire 'type'.
            kind = block.get("type") or (
                "text"
                if "text" in block
                else "tool_result"
                if "tool_use_id" in block
                else "tool_use"
                if "name" in block and "input" in block
                else "unknown"
            )
            if kind == "text":
                item.update(type="agentMessage", text=block.get("text", ""))
            elif kind == "tool_use":
                item.update(
                    id=block["id"],
                    type="claudeToolCall",
                    tool=block["name"],
                    input=block.get("input"),
                    status="inProgress",
                )
                self.tools[item["id"]] = deepcopy(item)
            elif kind == "tool_result":
                item.update(deepcopy(self.tools.get(block["tool_use_id"], {})))
                item.update(
                    id=block["tool_use_id"],
                    type="claudeToolCall",
                    output=block.get("content"),
                    status="failed" if block.get("is_error") else "completed",
                )
                self.tools[item["id"]] = deepcopy(item)
            else:
                item.update(type="claudeContent", content=deepcopy(block))
            items.append(item)
        return items

    def history(self, records):
        turns = []
        for record in records:
            source = plain(record)
            if source.get("session_id") != self.sid:
                raise ValueError("Claude history belongs to another session")
            message = source["message"]
            content = message.get("content", [])
            user = source["type"] == "user"
            tool_result = isinstance(content, list) and any(
                b.get("type") == "tool_result" for b in content
            )
            if not turns or (user and not tool_result):
                turns.append({"id": source["uuid"], "status": "completed", "items": []})
            items = self.blocks(content, message.get("id") or source["uuid"], user=user)
            for item in items:
                prior = next((i for i in turns[-1]["items"] if i["id"] == item["id"]), None)
                if prior:
                    prior.update(item)
                else:
                    turns[-1]["items"].append(item)
        return {
            "method": "workspace/history",
            "params": {"thread": {"id": self.sid, "turns": turns}, "provider": "claude"},
        }

    def receive(self, message):
        data = plain(message)
        kind = type(message).__name__
        sid = data.get("session_id") or (data.get("data") or {}).get("session_id")
        if sid and sid != self.sid:
            raise ValueError("Claude returned a different session identity")
        raw = self.event("workspace/claude", {"recordType": kind, "record": data})
        events = [raw]
        parent = data.get("parent_tool_use_id") or "root"
        if kind == "SystemMessage" and data.get("subtype") == "init":
            events.append(
                self.event(
                    "workspace/settings",
                    {"model": data["data"].get("model"), "claudeCapabilities": data["data"]},
                )
            )
        elif kind == "StreamEvent" and self.turn:
            event = data["event"]
            if event["type"] == "message_start":
                self.message_ids[parent] = event["message"]["id"]
            message_id = self.message_ids.get(parent)
            if (
                message_id
                and event["type"] == "content_block_delta"
                and event["delta"].get("type") == "text_delta"
            ):
                events.append(
                    self.event(
                        "item/agentMessage/delta",
                        {
                            "turnId": self.turn,
                            "itemId": f"{message_id}:{event['index']}",
                            "delta": event["delta"]["text"],
                        },
                    )
                )
        elif kind in {"AssistantMessage", "UserMessage"} and self.turn:
            message_id = data.get("message_id") or data.get("uuid") or self.message_ids.get(parent)
            if message_id:
                for item in self.blocks(data["content"], message_id, user=kind == "UserMessage"):
                    events.append(self.event("item/completed", {"turnId": self.turn, "item": item}))
            if data.get("model"):
                events.append(self.event("workspace/settings", {"model": data["model"]}))
        elif kind == "ResultMessage" and self.turn:
            events.append(
                self.event(
                    "turn/completed",
                    {
                        "turn": {
                            "id": self.turn,
                            "status": "failed" if data["is_error"] else "completed",
                            "providerOriginal": data,
                            "durationMs": data.get("duration_ms"),
                        }
                    },
                )
            )
            if isinstance(data.get("usage"), dict):
                events.append(self.event("workspace/claudeUsage", {"usage": data["usage"]}))
            self.turn = None
        return events
