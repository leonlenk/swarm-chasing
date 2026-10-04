"""JSON Schema (draft 2020-12) for a declarative dataset mapping. ``python -m swarm_mcp.setup schema`` prints it.

A *path* is a dotted field path into a row (``speaker.id``); ``[]`` marks an array
of objects (``mentions[].id``). A key that literally contains dots (a CSV header
``user.name``) is matched first. A *field spec* is either a path string or an
object; every role that takes one also accepts the shorthand string.
"""

from __future__ import annotations

from typing import Any

SLUG = r"^[a-z][a-z0-9_-]*$"

_path = {"type": "string", "minLength": 1, "description": "dotted field path, e.g. 'speaker.id' or 'mentions[].id'"}
_paths = {"anyOf": [_path, {"type": "array", "items": _path, "minItems": 1}]}
_meta = {
    "description": "fields kept in meta: a list of paths (key = path) or {out_name: path}",
    "anyOf": [
        {"type": "array", "items": _path},
        {"type": "object", "additionalProperties": _path},
    ],
}
_where = {
    "type": "array",
    "description": "row filters; a row is kept only when every condition holds",
    "items": {
        "type": "object",
        "required": ["field"],
        "additionalProperties": False,
        "properties": {
            "field": _path,
            "op": {"enum": ["==", "!=", "in", "not_in", "exists", "missing", "contains"], "default": "=="},
            "value": {},
        },
    },
}
_time = {
    "anyOf": [
        _path,
        {
            "type": "object",
            "required": ["field"],
            "additionalProperties": False,
            "properties": {
                "field": _path,
                "format": {
                    "enum": ["auto", "iso", "epoch_s", "epoch_ms", "epoch_us", "rfc2822", "strptime"],
                    "default": "auto",
                },
                "pattern": {"type": "string", "description": "strptime pattern when format is 'strptime'"},
            },
        },
    ]
}
_value_field = {
    "description": "a path, {field: path}, or a constant {value: ...}",
    "anyOf": [
        _path,
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "field": _path,
                "value": {"type": ["string", "null"]},
                "lookup": {"type": "string", "description": "name of an entry in 'lookups' to translate the value"},
                "values": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "null"]},
                    "description": "translate raw values, e.g. {'user': 'human', 'bot': 'agent'}",
                },
                "default": {"type": ["string", "null"]},
            },
        },
    ],
}
_match = {
    "enum": ["id", "name", "any"],
    "default": "any",
    "description": "how values resolve to agents: by agent id, by display name/alias (case-insensitive), or either",
}

MAPPING_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://swarm-mcp.local/schemas/mapping-v1.json",
    "title": "swarm-mcp dataset mapping (v1)",
    "description": "Declarative mapping of a dataset's files/tables onto standard event records. Purely data: nothing in it is executed.",
    "type": "object",
    "required": ["source", "records"],
    "additionalProperties": False,
    "properties": {
        "mapping_version": {"const": 1},
        "source": {"type": "string", "pattern": SLUG, "description": "event-id source slug, e.g. 'forum'"},
        "description": {"type": "string"},
        "root": {"type": "string", "description": "default dataset path (used by check when no path is given)"},
        "email_allowlist": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}, "description": "free-form notes (e.g. TODOs); ignored"},
        "agents": {
            "type": "object",
            "additionalProperties": False,
            "description": "where agents come from. Omit 'from' (or set derive_from_actors) to make every distinct actor an agent.",
            "properties": {
                "from": {
                    "type": ["string", "null"],
                    "description": "table key or glob, e.g. 'agents.jsonl.gz', 'forum.db#users'",
                },
                "id": _path,
                "display_name": _path,
                "aliases": {
                    "type": "array",
                    "items": _path,
                    "description": "fields holding alternative names (strings or lists)",
                },
                "meta": _meta,
                "where": _where,
                "derive_from_actors": {"type": "boolean", "default": False},
            },
        },
        "lookups": {
            "type": "object",
            "description": "named key -> value tables for joins, e.g. room id -> room name",
            "additionalProperties": {
                "type": "object",
                "required": ["from", "key", "value"],
                "additionalProperties": False,
                "properties": {"from": {"type": "string"}, "key": _path, "value": _path},
            },
        },
        "records": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["from", "kind", "local_id"],
                "additionalProperties": False,
                "properties": {
                    "from": {"type": "string", "description": "table key or glob"},
                    "kind": {"type": "string", "pattern": SLUG},
                    "category": {"enum": ["message", "action", "other"], "default": "message"},
                    "description": {"type": "string"},
                    "where": _where,
                    "local_id": {
                        "description": "path, list of paths (joined with ':'), or '@row' for '<table>:<row number>'",
                        **_paths,
                    },
                    "time": _time,
                    "ts_quality": {"enum": ["exact", "approx", "derived", "missing"]},
                    "actor": {
                        "anyOf": [
                            _path,
                            {
                                "type": "object",
                                "required": ["field"],
                                "additionalProperties": False,
                                "properties": {
                                    "field": _path,
                                    "match": _match,
                                    "fallback_field": {
                                        **_path,
                                        "description": "used when 'field' is empty, e.g. a human user id",
                                    },
                                    "fallback_prefix": {"type": "string", "default": "human:"},
                                    "unmatched_prefix": {"type": "string", "default": "external:"},
                                },
                            },
                        ]
                    },
                    "actor_type": _value_field,
                    "location": _value_field,
                    "type": _value_field,
                    "text": {
                        "anyOf": [
                            _path,
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "field": _path,
                                    "fields": {"type": "array", "items": _path, "minItems": 1},
                                    "sep": {"type": "string", "default": "\n\n"},
                                },
                            },
                        ]
                    },
                    "reply_to": {
                        "anyOf": [
                            _path,
                            {
                                "type": "object",
                                "required": ["field"],
                                "additionalProperties": False,
                                "properties": {
                                    "field": _path,
                                    "kind": {
                                        "type": "string",
                                        "pattern": SLUG,
                                        "description": "kind of the target (default: this kind)",
                                    },
                                },
                            },
                        ]
                    },
                    "recipients": {
                        "anyOf": [
                            _path,
                            {
                                "type": "object",
                                "required": ["field"],
                                "additionalProperties": False,
                                "properties": {
                                    "field": _path,
                                    "match": _match,
                                    "split": {
                                        "type": "string",
                                        "description": "separator when the field is one string",
                                    },
                                },
                            },
                        ]
                    },
                    "text_mentions": {
                        "enum": ["at", "names", None],
                        "description": "also add agents mentioned in the text as recipients: '@handle' tokens, or any agent name/alias",
                    },
                    "meta": _meta,
                },
            },
        },
        "periods": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["from", "kind", "local_id"],
                "additionalProperties": False,
                "properties": {
                    "from": {"type": "string"},
                    "kind": {"type": "string", "pattern": SLUG},
                    "description": {"type": "string"},
                    "where": _where,
                    "local_id": _paths,
                    "label": _path,
                    "start": _time,
                    "end": _time,
                    "meta": _meta,
                },
            },
        },
    },
}
