"""Translate Claude SDK records into the shared pane schema, retaining originals."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from xml.etree import ElementTree


def plain(value):
    return asdict(value) if is_dataclass(value) else deepcopy(value)


def history_command(content):
    """Recognize only the complete native slash-command envelope."""
    if isinstance(content, list):
        if len(content) != 1 or content[0].get("type") != "text":
            return None
        content = content[0].get("text")
    if not isinstance(content, str) or len(content) > 65536 or "<!" in content or "<?" in content:
        return None
    if not content.lstrip().startswith("<command-name>"):
        return None
    try:
        root = ElementTree.fromstring(f"<command>{content}</command>")
    except ElementTree.ParseError:
        return None
    if [child.tag for child in root] != ["command-name", "command-message", "command-args"]:
        return None
    if (root.text or "").strip() or any(child.attrib or len(child) or (child.tail or "").strip() for child in root):
        return None
    name, label, args = ((child.text or "").strip() for child in root)
    if not name.startswith("/") or len(name) < 2 or any(char.isspace() for char in name) or label != name[1:]:
        return None
    return name + (f" {args}" if args else "")


class ClaudeEvents:
    def __init__(self, sid):
        self.sid = sid
        self.turn = None
        self.message_ids = {}
        self.tools = {}
        self.streaming_tools = {}
        self.capabilities = {}
        self.tasks = {}
        self.last_root_text = None

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
        turn_items = {}
        model = None
        for record in records:
            source = plain(record)
            if source.get("session_id") != self.sid:
                raise ValueError("Claude history belongs to another session")
            message = source["message"]
            content = message.get("content", [])
            user = source["type"] == "user"
            candidate = message.get("model")
            if not user and isinstance(candidate, str) and candidate and candidate != "<synthetic>":
                model = candidate
            tool_result = isinstance(content, list) and any(
                b.get("type") == "tool_result" for b in content
            )
            if not turns or (user and not tool_result):
                turns.append({"id": source["uuid"], "status": "completed", "items": []})
                turn_items = {}
            command = history_command(content) if user else None
            items = self.blocks(command if command is not None else content, message.get("id") or source["uuid"], user=user)
            if command is not None:
                items[0]["providerOriginal"] = deepcopy(source)
            for item in items:
                prior = turn_items.get(item["id"])
                if prior:
                    prior.update(item)
                else:
                    turns[-1]["items"].append(item)
                    turn_items[item["id"]] = item
        return {
            "method": "workspace/history",
            "params": {"thread": {"id": self.sid, "turns": turns, **({"model": model} if model else {})}, "provider": "claude"},
        }

    def receive(self, message):
        data = plain(message)
        kind = type(message).__name__
        original = deepcopy(data)
        if isinstance(message, dict):
            wire_type = data.get("type")
            kind = {
                "assistant": "AssistantMessage", "user": "UserMessage",
                "result": "ResultMessage", "stream_event": "StreamEvent",
                "system": "SystemMessage",
            }.get(wire_type, f"Native:{wire_type}")
            if wire_type in {"assistant", "user"}:
                body = data.get("message")
                if not isinstance(body, dict):
                    raise ValueError("Claude native message has no message body")
                data.update(content=deepcopy(body.get("content", [])),
                            model=body.get("model"), message_id=body.get("id") or data.get("uuid"))
            elif wire_type == "system":
                kind = {
                    "task_started": "TaskStartedMessage", "task_progress": "TaskProgressMessage",
                    "task_notification": "TaskNotificationMessage", "task_updated": "TaskUpdatedMessage",
                }.get(data.get("subtype"), "SystemMessage")
                if kind == "SystemMessage":
                    data["data"] = deepcopy(original)
        sid = data.get("session_id") or (data.get("data") or {}).get("session_id")
        if sid and sid != self.sid:
            raise ValueError("Claude returned a different session identity")
        raw = self.event("workspace/claude", {"recordType": kind, "record": original})
        events = [raw]
        parent = data.get("parent_tool_use_id") or "root"
        origin = {"parentToolUseId": parent} if parent != "root" else {}
        if origin and data.get("model") and data["model"] != "<synthetic>":
            origin["sourceModel"] = data["model"]
        if kind in {"TaskStartedMessage", "TaskProgressMessage", "TaskNotificationMessage", "TaskUpdatedMessage"}:
            task_id = data.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("Claude task event has no task identity")
            task = deepcopy(self.tasks.get(task_id, {"id": task_id}))
            terminal = task.get("status") in {"completed", "failed", "stopped", "killed"}
            previous_status = task.get("status")
            if kind == "TaskUpdatedMessage":
                patch = data.get("patch") or {}
                task.update({key: deepcopy(patch[key]) for key in ("status", "description", "usage") if key in patch})
                if data.get("status"):
                    task["status"] = data["status"]
            else:
                task.update({key: deepcopy(data[key]) for key in ("description", "usage", "summary") if data.get(key) is not None})
                task["status"] = data.get("status") or "running"
            if terminal:
                task["status"] = previous_status
            self.tasks[task_id] = task
            events.append(self.event("workspace/backgroundTask", {"task": deepcopy(task)}))
        elif kind == "SystemMessage" and data.get("subtype") == "init":
            self.capabilities = deepcopy(data["data"])
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
                self.streaming_tools = {key: value for key, value in self.streaming_tools.items() if key[0] != parent}
            message_id = self.message_ids.get(parent)
            key = (parent, event.get("index"))
            if message_id and event["type"] == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
                block = event["content_block"]
                item = self.blocks([block], message_id)[0]
                item.update(inputStreaming=True, inputJson="", **origin)
                self.tools[item["id"]] = deepcopy(item)
                self.streaming_tools[key] = item["id"]
                events.append(self.event("item/started", {"turnId": self.turn, "item": item}))
            elif key in self.streaming_tools:
                tool_id = self.streaming_tools[key]
                item = self.tools[tool_id]
                if event["type"] == "content_block_delta" and event.get("delta", {}).get("type") == "input_json_delta":
                    item["inputJson"] += event["delta"]["partial_json"]
                    events.append(self.event("item/started", {"turnId": self.turn, "item": deepcopy(item)}))
                elif event["type"] == "content_block_stop":
                    self.streaming_tools.pop(key)
                    item["inputStreaming"] = False
                    if item["inputJson"]:
                        try:
                            parsed = json.loads(item["inputJson"])
                            if not isinstance(parsed, dict):
                                raise ValueError("Tool arguments are not an object")
                            item["input"] = parsed
                            item.pop("inputJson")
                        except ValueError:
                            item["inputUnavailable"] = True
                    else:
                        item.pop("inputJson")
                    events.append(self.event("item/started", {"turnId": self.turn, "item": deepcopy(item)}))
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
                            **origin,
                        },
                    )
                )
        elif kind in {"AssistantMessage", "UserMessage"} and self.turn:
            message_id = data.get("message_id") or data.get("uuid") or self.message_ids.get(parent)
            if message_id:
                items = self.blocks(data["content"], message_id, user=kind == "UserMessage")
                if kind == "AssistantMessage" and parent == "root":
                    self.last_root_text = (self.turn, "".join(
                        item["text"] for item in items if item["type"] == "agentMessage"
                    ))
                for item in items:
                    item.update(origin)
                    if item["type"] == "claudeToolCall":
                        self.tools[item["id"]] = deepcopy(item)
                        # The SDK may emit the authoritative tool before its
                        # trailing stream stop. Late fragments must not replace it.
                        self.streaming_tools = {key: value for key, value in self.streaming_tools.items() if value != item["id"]}
                    events.append(self.event("item/completed", {"turnId": self.turn, "item": item}))
            if parent == "root" and data.get("model") and data["model"] != "<synthetic>":
                events.append(self.event("workspace/settings", {"model": data["model"]}))
        elif kind == "ResultMessage" and self.turn:
            # A delayed result must not complete a newer input. Older SDK
            # records without acknowledgement fields keep their legacy path.
            if "user_message_uuid" in data or "user_message_uuids" in data:
                acknowledged = data.get("user_message_uuids", [])
                if not isinstance(acknowledged, list) or any(not isinstance(value, str) for value in acknowledged):
                    raise ValueError("Invalid Claude input acknowledgement")
                acknowledged = [*acknowledged, data.get("user_message_uuid")]
                if self.turn not in acknowledged:
                    raise ValueError("Claude result does not acknowledge the active input")
            if (
                data.get("num_turns") == 0
                and isinstance(data.get("result"), str)
                and data["result"]
                # Some local commands emit both an assistant message and an
                # identical final result. Keep the raw result, not a second row.
                and (data.get("is_error") or self.last_root_text != (self.turn, data["result"]))
            ):
                events.append(
                    self.event(
                        "item/completed",
                        {
                            "turnId": self.turn,
                            "item": {
                                "id": f"{self.turn}:command-result",
                                "type": "commandOutput",
                                "text": data["result"],
                                "status": "failed" if data["is_error"] else "completed",
                            },
                        },
                    )
                )
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
            self.last_root_text = None
            self.streaming_tools.clear()
        return events
