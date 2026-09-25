"""A small check of an executor's answer against the JSON schema it was given.

Only the subset dream's own schemas use: object/array/string/integer/boolean,
properties, required, additionalProperties false, enum and items. Lengths are
checked by each phase, not here.
"""

from __future__ import annotations

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}


def matches(value: object, schema: dict) -> bool:
    expected = _TYPES[schema["type"]]
    # bool is an int subclass: JSON true is never an integer here
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if expected is dict:
        properties = schema.get("properties", {})
        if set(schema.get("required", ())) - set(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(matches(item, properties[key]) for key, item in value.items()
                   if key in properties)
    if expected is list:
        return all(matches(item, schema["items"]) for item in value)
    return True
