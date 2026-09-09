"""Validate native MCP replies without fetching schemas or echoing user input."""

from copy import deepcopy


def validate_reply(params, answer):
    if not isinstance(answer, dict) or set(answer) != {"action", "content"}:
        raise ValueError("An MCP action and content are required")
    action = answer["action"]
    if action not in ("accept", "decline", "cancel"):
        raise ValueError("Invalid MCP action")
    if action != "accept":
        if answer["content"] is not None:
            raise ValueError("Declined MCP requests cannot include content")
        return
    if params.get("mode") == "url":
        if answer["content"] is not None:
            raise ValueError("URL confirmation cannot include form content")
        return
    if params.get("mode") != "form":
        raise ValueError("This MCP form mode is not implemented")
    from jsonschema import FormatChecker
    from jsonschema.validators import validator_for
    from referencing import Registry

    schema = deepcopy(params.get("requestedSchema"))
    content = answer["content"]
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or not isinstance(content, dict)
    ):
        raise ValueError("MCP form requires an object")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or content.keys() - properties.keys():
        raise ValueError("MCP answer contains unrequested fields")

    # Native optional schema keywords may be serialized as null.
    def normalize(value):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    schema = normalize(schema)
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        validator(schema, registry=Registry(), format_checker=FormatChecker()).validate(content)
    except Exception:
        raise ValueError("Answer does not match the requested MCP form") from None
