"""Bounded, value-free feedback for Pydantic tool argument errors."""

import re

_MAX_ERRORS = 16
_MAX_PATH = 8
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validation_feedback(exc, schema):
    """Return safe error locations and codes; never return messages or inputs.

    Only names declared in the schema (plus common mistaken argument names) are
    exposed. Unknown names are replaced because they may contain user data.
    """
    names = {"requirement", "scope_id"}

    def collect(node):
        if isinstance(node, dict):
            names.update(node.get("properties", {}).keys())
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(schema.model_json_schema())
    errors = []
    for item in exc.errors(include_url=False, include_context=False, include_input=False)[:_MAX_ERRORS]:
        path = []
        for part in item["loc"][:_MAX_PATH]:
            if isinstance(part, int):
                path.append(min(max(part, 0), 9999))
            elif isinstance(part, str) and part in names:
                path.append(part)
            else:
                path.append("<unknown>")
        code = item["type"]
        errors.append({"path": path, "code": code if _CODE.fullmatch(code) else "validation_error"})
    return errors
