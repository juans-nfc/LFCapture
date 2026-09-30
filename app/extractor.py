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
    if f.get("length"):
        desc += f". Max {f['length']} characters — shorten or abbreviate if needed."
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


def extract(pdf_bytes: bytes, templates: list[dict], forced_template: str | None = None, context: str = "", lessons: str = "") -> dict:
    candidates = [t for t in templates if not forced_template or t["name"] == forced_template]
    if not candidates:
        raise ValueError(f"Template not found: {forced_template}")

    catalog = "\n".join(
        f"- {t['name']}: {t['description'] or 'no description'} — fields: " + ", ".join(f["name"] for f in t["fields"])
        for t in candidates
    )
    # Static part: identical for every document read against this catalog -> cached across calls.
    static_instruction = (
        "You are a document-capture clerk for Northern Fruit, an apple packer and shipper in Wenatchee, WA. "
        "Read the attached PDF (it may be a scan, a fax, or a system-generated form). "
        + ("The template is fixed by the operator; do not change it. " if forced_template else "Choose the single best-matching template. ")
        + "Fill only fields you can actually read from the document; leave unknown fields out rather than guessing. "
        "Never pick a list value just because the field is required — omit it if the document does not state it. "
        "When the document shows a longer or slightly different form of an allowed list value (e.g. 'Northern Fruit Company' where the allowed value is 'Northern Fruit'), use the allowed value. "
        "Order numbers, PO numbers and invoice numbers must be copied exactly as printed. "
        "If a multi-value field applies to several values (e.g. several order numbers on one packing list), include all of them.\n\n"
        f"Available templates:\n{catalog}"
    )
    tool = _tool(candidates)
    tool["cache_control"] = {"type": "ephemeral"}          # cache prefix ends after tools + system (tools -> system -> messages)
    system = [{"type": "text", "text": static_instruction, "cache_control": {"type": "ephemeral"}}]
    if lessons:
        # Its own breakpoint: it changes whenever someone corrects a document, but the big catalog block above stays cached.
        system.append({"type": "text", "text": lessons, "cache_control": {"type": "ephemeral"}})

    # Dynamic part: the document itself and anything specific to this read.
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf_bytes).decode()}},
        {"type": "text", "text": "Read this document and record it." + (f"\n\nHow this document arrived (may contain useful identifiers):\n{context}" if context else "")},
    ]

    msg = _client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_document"},
        messages=[{"role": "user", "content": content}],
    )
    use = next(b for b in msg.content if b.type == "tool_use")
    data = use.input
    cls = data.get("classification", {})
    fields = cls.get("fields", {})
    # normalise: every field -> list[str]
    norm = {k: (v if isinstance(v, list) else [v]) for k, v in fields.items() if v not in (None, "", [])}
    u = msg.usage
    return {
        "template": cls.get("template") or forced_template,
        "fields": norm,
        "confidence": float(data.get("confidence", 0)),
        "summary": data.get("summary", ""),
        "suggested_filename": data.get("suggested_filename", ""),
        "notes": data.get("notes", ""),
        "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                  "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0, "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0},
    }


_SPLIT_TOOL = {
    "name": "report_segments",
    "description": "Report where each separate document begins and ends inside this scanned PDF.",
    "input_schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "first_page": {"type": "integer", "description": "1-based first page of this document"},
                        "last_page": {"type": "integer", "description": "1-based last page of this document (inclusive)"},
                        "what": {"type": "string", "description": "A few words: what this document is (e.g. 'Sales Invoice 77824-00')"},
                    },
                    "required": ["first_page", "last_page", "what"],
                },
            }
        },
        "required": ["segments"],
    },
}


def detect_segments(pdf_bytes: bytes, page_count: int, templates: list[dict]) -> list[dict]:
    """Find separate documents inside one scanned PDF. Returns [{first_page,last_page,what}] covering all pages, in order."""
    kinds = ", ".join(t["name"] for t in templates)
    instruction = (
        f"This PDF has {page_count} pages and may contain SEVERAL separate documents scanned together "
        f"(for example an invoice, then a packing list, then a bill of lading for the same order). "
        f"Document types in this business include: {kinds}. "
        "Report the page range of each separate document. A new document usually starts with its own header/title block, "
        "a new document number, or a different form layout; continuation pages (page 2 of an invoice) belong to the previous document. "
        "If the whole PDF is one document, report a single segment. Every page must belong to exactly one segment."
    )
    msg = _client.messages.create(
        model=MODEL, max_tokens=1024, tools=[_SPLIT_TOOL], tool_choice={"type": "tool", "name": "report_segments"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf_bytes).decode()}},
            {"type": "text", "text": instruction}]}],
    )
    use = next(b for b in msg.content if b.type == "tool_use")
    segs = sorted(use.input.get("segments", []), key=lambda x: x["first_page"])
    # sanity: clamp to page range, drop overlaps/gaps by re-walking
    out, nxt = [], 1
    for sg in segs:
        a, b = max(nxt, int(sg["first_page"])), min(page_count, int(sg["last_page"]))
        if a > page_count or b < a:
            continue
        if a > nxt:  # gap: fold missing pages into this segment
            a = nxt
        out.append({"first_page": a, "last_page": b, "what": sg.get("what", "")})
        nxt = b + 1
    if not out:
        return [{"first_page": 1, "last_page": page_count, "what": ""}]
    if out[-1]["last_page"] < page_count:
        out[-1]["last_page"] = page_count
    return out
