"""Translate session-bound ACP updates without deciding permissions or running tools."""

from copy import deepcopy


class AcpEvents:
    def __init__(self, session_id):
        self.sid = session_id
        self.turn = None
        self.items = {}
        self.messages = {}
        self.questions = {}
        self.last_message = None

    def event(self, method, params):
        return {"method": method, "params": {**deepcopy(params), "threadId": self.sid}}

    def begin(self, turn_id):
        if self.turn is not None or not isinstance(turn_id, str) or not turn_id:
            raise ValueError("ACP turn is already active or invalid")
        self.turn = turn_id
        self.items, self.messages = {}, {}
        self.last_message = None
        return self.event("turn/started", {"turn": {"id": turn_id, "status": "inProgress", "items": []}})

    def update(self, params):
        if not isinstance(params, dict) or params.get("sessionId") != self.sid:
            raise ValueError("ACP update belongs to another session")
        update = params.get("update")
        if not isinstance(update, dict) or not isinstance(update.get("sessionUpdate"), str):
            raise ValueError("Invalid ACP update")
        kind = update["sessionUpdate"]
        if kind in {"available_commands_update", "config_option_update", "current_mode_update", "usage_update", "session_info_update"}:
            return self.event("workspace/acpMetadata", {"update": update})
        if self.turn is None:
            raise ValueError("ACP update has no active turn")
        if kind in {"agent_message_chunk", "user_message_chunk"} and not isinstance(update.get("content"), dict):
            raise ValueError("Invalid ACP content")
        if kind in {"agent_message_chunk", "user_message_chunk"} and update["content"].get("type") == "text":
            text = update["content"].get("text")
            if not isinstance(text, str):
                raise ValueError("Invalid ACP text")
            native_id = update.get("messageId")
            if native_id is not None and not isinstance(native_id, str):
                raise ValueError("Invalid ACP message identity")
            key = (kind, native_id) if native_id is not None else (
                self.last_message if self.last_message and self.last_message[0] == kind
                else (kind, len(self.messages)))
            self.last_message = key
            if key not in self.messages:
                self.messages[key] = f"{self.turn}:message:{len(self.messages)}"
            item_id = self.messages[key]
            item = deepcopy(self.items.get(item_id, {"id": item_id, "type": "agentMessage", "text": ""}))
            item["text"] += text
            if kind == "user_message_chunk":
                item.update(type="userMessage", content=[{"type": "text", "text": item["text"]}])
        elif kind in {"tool_call", "tool_call_update"}:
            self.last_message = None
            native_id = update.get("toolCallId")
            if not isinstance(native_id, str) or not native_id:
                raise ValueError("Invalid ACP tool identity")
            item_id = f"{self.turn}:tool:{native_id}"
            item = deepcopy(self.items.get(item_id, {"id": item_id, "type": "acpToolCall"}))
            original = {**item.get("providerOriginal", {}), **deepcopy(update)}
            item["providerOriginal"] = original
            item["tool"] = original.get("title", native_id)
            item["input"] = original.get("rawInput")
            item["output"] = original.get("rawOutput", original.get("content"))
            item["status"] = {"in_progress": "inProgress"}.get(original.get("status"), original.get("status", "pending"))
        else:
            self.last_message = None
            item_id = f"{self.turn}:event:{len(self.items)}"
            item = {"id": item_id, "type": "acpContent", "providerOriginal": deepcopy(update)}
        self.items[item_id] = item
        return self.event("item/completed" if item.get("status") in {"completed", "failed"} else "item/started",
                          {"turnId": self.turn, "item": item})

    def permission(self, request_id, params):
        if type(request_id) not in (str, int) or request_id in self.questions:
            raise ValueError("Invalid or duplicate ACP permission identity")
        if not isinstance(params, dict) or params.get("sessionId") != self.sid or self.turn is None:
            raise ValueError("ACP permission belongs to another session or no active turn")
        options = params.get("options")
        if not isinstance(options, list) or not options or any(
            not isinstance(option, dict) or not isinstance(option.get("optionId"), str)
            or not option["optionId"] or not isinstance(option.get("name"), str)
            or option.get("kind") not in {"allow_once", "allow_always", "reject_once", "reject_always"}
            for option in options
        ) or len({option["optionId"] for option in options}) != len(options):
            raise ValueError("Invalid ACP permission options")
        self.questions[request_id] = deepcopy(params)
        return {"id": request_id, **self.event("session/request_permission", params)}

    def answer(self, request_id, option_id=None):
        question = self.questions.get(request_id)
        if question is None:
            raise ValueError("ACP permission is no longer pending")
        if option_id is not None and option_id not in {o["optionId"] for o in question["options"]}:
            raise ValueError("ACP permission option was not offered")
        return {"outcome": {"outcome": "cancelled"} if option_id is None else {"outcome": "selected", "optionId": option_id}}

    def resolved(self, request_id):
        self.questions.pop(request_id, None)
        return self.event("serverRequest/resolved", {"requestId": request_id})

    def complete(self, stop_reason):
        if self.turn is None or stop_reason not in {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}:
            raise ValueError("Invalid ACP turn completion")
        if self.questions:
            raise ValueError("ACP permissions remain unresolved")
        result = self.event("turn/completed", {"turn": {"id": self.turn,
            "status": "interrupted" if stop_reason == "cancelled" else "completed",
            "stopReason": stop_reason, "items": list(self.items.values())}})
        self.turn = None
        return result
