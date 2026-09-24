from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import extractor, mailbox  # noqa: E402
from .laserfiche import LaserficheClient, LaserficheError  # noqa: E402

INBOX = Path(os.environ.get("INBOX_DIR", "./inbox"))
WORK = Path(os.environ.get("WORK_DIR", "./work"))
DONE = INBOX / "_done"
FAILED = INBOX / "_failed"
for p in (INBOX, WORK, DONE, FAILED):
    p.mkdir(parents=True, exist_ok=True)

AUTO_SAVE = float(os.environ.get("AUTO_SAVE_CONFIDENCE", "0") or 0)

app = FastAPI(title="LF Capture")
lf = LaserficheClient()
_lock = threading.Lock()
_templates: list[dict] = []
_templates_at = 0.0
_inbox_id: int | None = None


# ---------- helpers --------------------------------------------------
def templates(refresh: bool = False) -> list[dict]:
    global _templates, _templates_at
    with _lock:
        if refresh or not _templates or time.time() - _templates_at > 3600:
            _templates = lf.templates()
            _templates_at = time.time()
        return _templates


def inbox_entry_id() -> int:
    global _inbox_id
    if _inbox_id is None:
        _inbox_id = lf.entry_id_by_path(os.environ["LF_INBOX_PATH"])
    return _inbox_id


def _job_dir(job_id: str) -> Path:
    d = WORK / job_id
    if not d.exists():
        raise HTTPException(404, "Unknown job")
    return d


def _new_job(pdf_bytes: bytes, source_name: str, context: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    d = WORK / job_id
    d.mkdir()
    (d / "doc.pdf").write_bytes(pdf_bytes)
    (d / "meta.json").write_text(json.dumps({"source": source_name, "created": time.time(), "context": context}))
    return job_id


def _run_extract(job_id: str, forced: str | None) -> dict:
    d = _job_dir(job_id)
    meta = json.loads((d / "meta.json").read_text())
    result = extractor.extract((d / "doc.pdf").read_bytes(), templates(), forced, meta.get("context", ""))
    meta.update(result)
    (d / "meta.json").write_text(json.dumps(meta))
    return {"id": job_id, **meta}


# ---------- API ------------------------------------------------------
@app.get("/")
def root():
    # served directly (no redirect) so the app works under a reverse-proxy prefix like /lfcapture/
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/templates")
def api_templates(refresh: bool = False):
    try:
        return templates(refresh)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


@app.get("/api/queue")
def api_queue():
    """PDFs waiting in the drop folder, plus jobs already extracted but not saved."""
    pending = sorted(p.name for p in INBOX.glob("*.pdf"))
    jobs = []
    for d in sorted(WORK.iterdir(), key=lambda p: p.stat().st_mtime):
        m = d / "meta.json"
        if m.exists():
            meta = json.loads(m.read_text())
            if "template" not in meta and "notes" not in meta:
                continue  # extraction still running (or crashed mid-way); not reviewable yet
            jobs.append({"id": d.name, "source": meta.get("source"), "template": meta.get("template"), "confidence": meta.get("confidence")})
    return {"inbox": pending, "jobs": jobs}


@app.post("/api/extract")
async def api_extract(file: UploadFile | None = File(None), inbox_name: str | None = Form(None), template: str | None = Form(None)):
    if file is not None:
        data = await file.read()
        name = file.filename or "upload.pdf"
    elif inbox_name:
        src = INBOX / Path(inbox_name).name
        if not src.exists():
            raise HTTPException(404, "File not in inbox")
        data = src.read_bytes()
        name = src.name
        shutil.move(src, WORK / f"{src.name}.claimed")  # keep out of queue while in review
    else:
        raise HTTPException(400, "Send a file or an inbox_name")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Not a PDF")
    job_id = _new_job(data, name)
    try:
        result = _run_extract(job_id, template or None)
    except Exception as e:  # surface the reason to the UI and drop the half-made job
        shutil.rmtree(WORK / job_id, ignore_errors=True)
        claimed = WORK / f"{name}.claimed"
        if claimed.exists():
            shutil.move(claimed, FAILED / name)
        raise HTTPException(502, f"Extraction failed: {e}")
    if template:
        return result
    return _maybe_auto_save(job_id, result, name)


@app.post("/api/reextract/{job_id}")
def api_reextract(job_id: str, template: str = Form(...)):
    try:
        return _run_extract(job_id, template)
    except Exception as e:
        raise HTTPException(502, f"Extraction failed: {e}")


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    return {"id": job_id, **json.loads((_job_dir(job_id) / "meta.json").read_text())}


@app.get("/api/pdf/{job_id}")
def api_pdf(job_id: str):
    return FileResponse(_job_dir(job_id) / "doc.pdf", media_type="application/pdf")


class SaveRequest(BaseModel):
    template: str
    fields: dict[str, list[str]]
    filename: str


def _save(job_id: str, template: str, fields: dict[str, list[str]], filename: str) -> dict:
    d = _job_dir(job_id)
    tdef = next((t for t in templates() if t["name"] == template), None)
    if not tdef:
        raise HTTPException(400, f"Unknown template {template}")
    missing = [f["name"] for f in tdef["fields"] if f["required"] and not [v for v in fields.get(f["name"], []) if v]]
    if missing:
        raise HTTPException(400, "Required fields missing: " + ", ".join(missing))
    entry_id = lf.import_pdf(inbox_entry_id(), filename, (d / "doc.pdf").read_bytes(), template, fields)
    meta = json.loads((d / "meta.json").read_text())
    src = meta.get("source", "")
    claimed = WORK / f"{src}.claimed"
    if claimed.exists():
        shutil.move(claimed, DONE / src)
    shutil.rmtree(d, ignore_errors=True)
    return {"entry_id": entry_id, "saved": True}


@app.post("/api/save/{job_id}")
def api_save(job_id: str, req: SaveRequest):
    try:
        return _save(job_id, req.template, req.fields, req.filename)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


@app.delete("/api/job/{job_id}")
def api_discard(job_id: str):
    d = _job_dir(job_id)
    meta = json.loads((d / "meta.json").read_text())
    claimed = WORK / f"{meta.get('source', '')}.claimed"
    if claimed.exists():
        shutil.move(claimed, FAILED / meta["source"])
    shutil.rmtree(d, ignore_errors=True)
    return {"discarded": True}


def _maybe_auto_save(job_id: str, result: dict, name: str) -> dict:
    if AUTO_SAVE and result["confidence"] >= AUTO_SAVE:
        try:
            saved = _save(job_id, result["template"], result["fields"], result["suggested_filename"] or name)
            return {**result, "auto_saved": True, **saved}
        except Exception as e:
            result["notes"] = f"Auto-save failed: {e}. " + result.get("notes", "")
            (WORK / job_id / "meta.json").write_text(json.dumps({**json.loads((WORK / job_id / "meta.json").read_text()), "notes": result["notes"]}))
    return result


def _from_mailbox(name: str, pdf: bytes, context: str) -> None:
    """Mail poller callback: extract now so the queue shows a finished job, auto-save if allowed."""
    job_id = _new_job(pdf, name, context)
    try:
        result = _run_extract(job_id, None)
        _maybe_auto_save(job_id, result, name)
    except Exception as e:  # leave the job in the queue for a human; note the error
        m = WORK / job_id / "meta.json"
        m.write_text(json.dumps({**json.loads(m.read_text()), "template": None, "fields": {}, "confidence": 0,
                                 "summary": "", "suggested_filename": "", "notes": f"Automatic read failed: {e}"}))


@app.on_event("startup")
def _start_mail():
    mailbox.start_if_configured(_from_mailbox)


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
