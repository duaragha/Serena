import pytest

from core.workspace_elicitation import validate_reply

FORM = {
    "mode": "form",
    "requestedSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "count": {"type": "integer", "minimum": 1},
            "enabled": {"type": "boolean"},
        },
        "required": ["name", "count"],
    },
}


def test_form_acceptance_and_constraints():
    validate_reply(
        FORM, {"action": "accept", "content": {"name": "Ada", "count": 2, "enabled": False}}
    )
    for content in (
        {"name": "x", "count": 2},
        {"name": "Ada", "count": True},
        {"name": "Ada", "count": 0},
        {"name": "Ada"},
        {"name": "Ada", "count": 2, "extra": "secret"},
    ):
        with pytest.raises(ValueError) as error:
            validate_reply(FORM, {"action": "accept", "content": content})
        assert "secret" not in str(error.value)


def test_decline_and_cancel_unknown_schema_need_no_form_data():
    for action in ("decline", "cancel"):
        validate_reply({"mode": "future"}, {"action": action, "content": None})
        with pytest.raises(ValueError):
            validate_reply(FORM, {"action": action, "content": {"name": "secret"}})
    validate_reply({"mode": "url"}, {"action": "accept", "content": None})


def test_external_schema_refs_are_not_fetched():
    form = {
        "mode": "form",
        "requestedSchema": {
            "type": "object",
            "properties": {"field": {"$ref": "http://127.0.0.1:1/private"}},
        },
    }
    with pytest.raises(ValueError, match="does not match"):
        validate_reply(form, {"action": "accept", "content": {"field": "secret"}})
