"""Tool schema for recalling what the answering service remembers (Zep)."""

from __future__ import annotations

from typing import Any

RECALL_MEMORY: dict[str, Any] = {
    "name": "recall_memory",
    "description": (
        "Search your memory before reaching for the account tools: facts from "
        "this caller's earlier calls, and the Salesmessage product catalogue. "
        "Use it when the caller asks something they may have asked before, "
        "refers to a contact, campaign or figure you discussed on an earlier "
        "call, or asks how a feature works. Remembered account figures are a "
        "snapshot from the date shown, not live data. If this finds nothing "
        "useful, move on to the account tools."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to remember, as a phrase, e.g. 'which inbox they use for support'.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

TOOLS: list[dict[str, Any]] = [RECALL_MEMORY]
