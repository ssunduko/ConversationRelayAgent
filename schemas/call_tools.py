"""Tool schemas for the call-control tools sent to Claude.

Account tools (contacts, messages, conversations, tags, reporting) are not
here — their schemas come from the Salesmsg MCP server at connect time, so the
agent always matches what the server actually offers. These are the two things
only the phone call can do.

`strict` guarantees the arguments Claude sends validate against the schema,
which matters here because a malformed argument turns into dead air.
"""

from __future__ import annotations

from typing import Any

TRANSFER_TO_HUMAN: dict[str, Any] = {
    "name": "transfer_to_human",
    "description": (
        "Transfer the caller to a human on the support line. Call this when "
        "they ask for a person, when they need something none of your tools "
        "cover, or when a lookup keeps failing. Tell the caller you are "
        "putting them through before calling this — the transfer happens "
        "immediately afterwards and you will not get another turn."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One sentence on why the call is being transferred, for the human agent.",
            }
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}

END_CALL: dict[str, Any] = {
    "name": "end_call",
    "description": (
        "Hang up. Call this only after the caller has said goodbye or "
        "confirmed they need nothing further, and after you have said "
        "goodbye back."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One sentence on why the call ended, for the call log.",
            }
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}

TOOLS: list[dict[str, Any]] = [TRANSFER_TO_HUMAN, END_CALL]
