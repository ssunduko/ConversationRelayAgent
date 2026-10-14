"""Tool schemas for the outbound demo-booking call."""

from __future__ import annotations

from typing import Any

SEARCH_PRODUCT: dict[str, Any] = {
    "name": "search_product_knowledge",
    "description": (
        "Search Salesmsg product documentation and the website to answer a "
        "caller's question. Use it for anything about what the product does, "
        "how a feature works, integrations, pricing pages, or comparisons. "
        "Search before answering rather than relying on memory — and if it "
        "returns nothing useful, say the demo call will cover it instead of "
        "guessing."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What the caller wants to know, in your own words. A phrase works better than a keyword.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

SAVE_CALLER_DETAILS: dict[str, Any] = {
    "name": "save_caller_details",
    "description": (
        "Record what you have learned about the caller. Call this as soon as "
        "you have any of these — do not wait until the end of the call, and do "
        "not wait to have all of them. Call it again as you learn more. "
        "Confirm spellings back before saving a name or email; speech "
        "recognition mangles both."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "first_name": {"type": "string", "description": "Caller's first name, or empty if not given yet."},
            "last_name": {"type": "string", "description": "Caller's last name, or empty if not given yet."},
            "email": {"type": "string", "description": "Email address, or empty if not given yet."},
            "experience": {
                "type": "string",
                "description": (
                    "What they told you about their current texting and calling software: "
                    "which tools, how they use them, what frustrates them. Empty if not discussed."
                ),
            },
            "notes": {"type": "string", "description": "Anything else worth knowing next time. Empty if none."},
        },
        "required": ["first_name", "last_name", "email", "experience", "notes"],
        "additionalProperties": False,
    },
}

BOOK_DEMO: dict[str, Any] = {
    "name": "book_demo",
    "description": (
        "Record an agreed appointment for a Salesmsg product demo. Only call "
        "this once the caller has actually agreed and given a time. Read the "
        "time back to them before calling it. If they decline, do not call "
        "this — thank them and wrap up."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "requested_time": {
                "type": "string",
                "description": "The time as the caller said it, e.g. 'Tuesday the 18th at 2pm Eastern'.",
            },
            "notes": {
                "type": "string",
                "description": "What they want covered on the demo, or anything the rep should know. Empty if none.",
            },
        },
        "required": ["requested_time", "notes"],
        "additionalProperties": False,
    },
}

TOOLS: list[dict[str, Any]] = [SEARCH_PRODUCT, SAVE_CALLER_DETAILS, BOOK_DEMO]
