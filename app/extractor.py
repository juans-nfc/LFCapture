"""Classify a PDF against the Laserfiche template catalog and extract its fields.

One Claude call, PDF attached as a base64 document block, JSON forced through a
tool schema built from the live template definitions so field names always match
what Laserfiche expects.
"""
from __future__ import annotations

import base64
import os

import anthropic

_client = anthropic.Anthropic()
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

_TYPE_HINT = {
    "Date": "ISO date YYYY-MM-DD",
    "DateTime": "ISO datetime YYYY-MM-DDTHH:MM:SS",
    "Time": "HH:MM:SS",
    "Number": "decimal number as a string, no currency symbols or thousands separators",
    "LongInteger": "integer as a string",
    "ShortInteger": "integer as a string",
    "List": "one of the allowed values exactly",
}


def _field_schema(f: dict) -> dict:
    hint = _TYPE_HINT.get(f["type"], "text")
    desc = f"{f['name']} ({hint})"
    if f.get("description"):
        desc += f": {f['description']}"
    if f["list"]:
        desc += ". Allowed: " + ", ".join(f["list"]) + ". Leave OUT unless one of these values is actually printed on the document."
    if f["required"]:
        desc += ". Required."
    item = {"type": "string"}
    if f["list"]:
        item["enum"] = f["list"]
    if f["multi"]:
        return {"type": "array", "items": item, "description": desc + " Multi-value: list every applicable value."}
    return {**item, "description": desc}


def _tool(templates: list[dict]) -> dict:
    # oneOf per template: the chosen template dictates which field object is filled.
    variants = []
    for t in templates:
        props = {f["name"]: _field_schema(f) for f in t["fields"]}
        variants.append(
            {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "enum": [t["name"]]},
                    "fields": {"type": "object", "properties": props, "additionalProperties": False},
                },
                "required": ["template", "fields"],
            }
        )
    return {
        "name": "record_document",
        "description": "Record the document's type and its metadata fields.",
        "input_schema": {
            "type": "object",
            "properties": {
                "classification": {"anyOf": variants},
                "confidence": {"type": "number", "description": "0-1, how sure you are of the template choice AND the key fields."},
                "summary": {"type": "string", "description": "One sentence: what this document is."},
                "suggested_filename": {"type": "string", "description": "Short descriptive filename without extension, e.g. '80670-0 Invoice ACME 2026-09-23'."},
                "notes": {"type": "string", "description": "Anything ambiguous, unreadable, or worth a human's attention. Empty if none."},
            },
            "required": ["classification", "confidence", "summary", "suggested_filename", "notes"],
        },
    }


def extract(pdf_bytes: bytes, templates: list[dict], forced_template: str | None = None, context: str = "") -> dict:
    candidates = [t for t in templates if not forced_template or t["name"] == forced_template]
    if not candidates:
        raise ValueError(f"Template not found: {forced_template}")

    catalog = "\n".join(
        f"- {t['name']}: {t['description'] or 'no description'} — fields: " + ", ".join(f["name"] for f in t["fields"])
        for t in candidates
    )
    instruction = (
        "You are a document-capture clerk for Northern Fruit, an apple packer and shipper in Wenatchee, WA. "
        "Read the attached PDF (it may be a scan, a fax, or a system-generated form). "
        + ("The template is fixed by the operator; do not change it. " if forced_template else "Choose the single best-matching template. ")
        + "Fill only fields you can actually read from the document; leave unknown fields out rather than guessing. "
        "Order numbers, PO numbers and invoice numbers must be copied exactly as printed. "
        "If a multi-value field applies to several values (e.g. several order numbers on one packing list), include all of them.\n\n"
        f"Available templates:\n{catalog}"
        + (f"\n\nHow this document arrived (may contain useful identifiers):\n{context}" if context else "")
    )

    msg = _client.messages.create(
        model=MODEL,
        max_tokens=2048,
        tools=[_tool(candidates)],
        tool_choice={"type": "tool", "name": "record_document"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf_bytes).decode()}},
                    {"type": "text", "text": instruction},
                ],
            }
        ],
    )
    use = next(b for b in msg.content if b.type == "tool_use")
    data = use.input
    cls = data.get("classification", {})
    fields = cls.get("fields", {})
    # normalise: every field -> list[str]
    norm = {k: (v if isinstance(v, list) else [v]) for k, v in fields.items() if v not in (None, "", [])}
    return {
        "template": cls.get("template") or forced_template,
        "fields": norm,
        "confidence": float(data.get("confidence", 0)),
        "summary": data.get("summary", ""),
        "suggested_filename": data.get("suggested_filename", ""),
        "notes": data.get("notes", ""),
        "usage": {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens},
    }
