from __future__ import annotations

import json
import logging
import os
import shutil
import queue
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import extractor, mailbox  # noqa: E402
from .laserfiche import LaserficheClient, LaserficheError  # noqa: E402
from . import users  # noqa: E402

INBOX = Path(os.environ.get("INBOX_DIR", "./inbox"))
WORK = Path(os.environ.get("WORK_DIR", "./work"))
DONE = INBOX / "_done"
FAILED = INBOX / "_failed"
for p in (INBOX, WORK, DONE, FAILED):
    p.mkdir(parents=True, exist_ok=True)

AUTO_SAVE = float(os.environ.get("AUTO_SAVE_CONFIDENCE", "0") or 0)

app = FastAPI(title="LF Capture")
_svc = users.service_client()          # optional; only the mailbox path uses it
_lock = threading.Lock()
_templates: list[dict] = []
_templates_at = 0.0
_inbox_id: int | None = None


# ---------- helpers --------------------------------------------------
def templates(lf: LaserficheClient | None = None, refresh: bool = False) -> list[dict]:
    """Template catalog (shared cache; any authenticated client may refresh it)."""
    global _templates, _templates_at
    with _lock:
        if refresh or not _templates or time.time() - _templates_at > 3600:
            if lf is None:
                if not _templates:
                    raise LaserficheError("Template catalog not loaded yet")
                return _templates
            _templates = lf.templates()
            _templates_at = time.time()
        return _templates


_folder_ids: dict[str, int] = {}


def folder_entry_id(lf: LaserficheClient, path: str | None = None) -> int:
    path = (path or os.environ["LF_INBOX_PATH"]).strip().rstrip("\\") or "\\"
    if path not in _folder_ids:
        _folder_ids[path] = lf.entry_id_by_path(path)
    return _folder_ids[path]


def _job_dir(job_id: str, user: str | None = None) -> Path:
    """Job folder; when a user is given, enforce that the job is theirs or shared (no owner)."""
    d = WORK / job_id
    if not d.exists():
        raise HTTPException(404, "Unknown job")
    if user is not None:
        m = d / "meta.json"
        owner = json.loads(m.read_text()).get("owner") if m.exists() else None
        if owner and owner != user:
            raise HTTPException(403, "That document is in another user's queue")
    return d


def _new_job(pdf_bytes: bytes, source_name: str, context: str = "", owner: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    d = WORK / job_id
    d.mkdir()
    (d / "doc.pdf").write_bytes(pdf_bytes)
    (d / "meta.json").write_text(json.dumps({"source": source_name, "created": time.time(), "context": context, "owner": owner}))
    return job_id


def _run_extract(job_id: str, forced: str | None, lf: LaserficheClient | None = None) -> dict:
    d = _job_dir(job_id)
    meta = json.loads((d / "meta.json").read_text())
    result = extractor.extract((d / "doc.pdf").read_bytes(), templates(lf), forced, meta.get("context", ""))
    meta.update(result)
    (d / "meta.json").write_text(json.dumps(meta))
    return {"id": job_id, **meta}


# ---------- API ------------------------------------------------------
@app.get("/")
def root():
    # served directly (no redirect) so the app works under a reverse-proxy prefix like /lfcapture/
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/me")
def api_me(request: Request):
    u = users.current_user(request)
    creds = users.get_creds(u) if u else None
    return {"lf_username": creds["username"] if creds else None, "has_lf_creds": bool(creds),
            "inbox_path": os.environ.get("LF_INBOX_PATH", ""),
            "mailbox_enabled": bool(os.environ.get("MAIL_MAILBOX")) and _svc is not None}


class LfCredsRequest(BaseModel):
    username: str
    password: str


@app.post("/api/settings/lf")
def api_set_lf(req: LfCredsRequest, response: Response):
    try:
        users.set_creds(req.username.strip(), req.password)
    except LaserficheError as e:
        raise HTTPException(400, f"Laserfiche rejected that login: {e}")
    users.set_session(response, req.username)
    return {"ok": True}


@app.delete("/api/settings/lf")
def api_clear_lf(request: Request, response: Response):
    u = users.current_user(request)
    if u:
        users.clear_creds(u)
    users.clear_session(response)
    return {"ok": True}


@app.get("/api/templates")
def api_templates(refresh: bool = False, lf: LaserficheClient = Depends(users.lf_for)):
    try:
        return templates(lf, refresh)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


@app.get("/api/queue")
def api_queue(request: Request):
    """Drop-folder PDFs (shared) plus extracted-but-unsaved jobs: the caller's own first, then shared ones."""
    user = users.current_user(request)
    pending = sorted(p.name for p in INBOX.glob("*.pdf"))
    mine, shared = [], []
    for d in sorted(WORK.iterdir(), key=lambda p: p.stat().st_mtime):
        m = d / "meta.json"
        if not d.is_dir() or not m.exists():
            continue
        meta = json.loads(m.read_text())
        if "template" not in meta and "notes" not in meta:
            continue  # extraction still running (or crashed mid-way); not reviewable yet
        row = {"id": d.name, "source": meta.get("source"), "template": meta.get("template"), "confidence": meta.get("confidence"),
               "lf_path": meta.get("lf_path"), "owner": meta.get("owner")}
        if not meta.get("owner"):
            shared.append(row)
        elif meta.get("owner") == user:
            mine.append(row)
    return {"inbox": pending, "jobs": mine + shared, "mine": len(mine), "shared": len(shared) + len(pending)}


@app.post("/api/extract")
async def api_extract(request: Request, file: UploadFile | None = File(None), inbox_name: str | None = Form(None), template: str | None = Form(None), lf: LaserficheClient = Depends(users.lf_for)):
    owner = users.current_user(request) if file is not None else None
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
    job_id = _new_job(data, name, owner=owner)
    try:
        result = _run_extract(job_id, template or None, lf)
    except Exception as e:  # surface the reason to the UI and drop the half-made job
        shutil.rmtree(WORK / job_id, ignore_errors=True)
        claimed = WORK / f"{name}.claimed"
        if claimed.exists():
            shutil.move(claimed, FAILED / name)
        raise HTTPException(502, f"Extraction failed: {e}")
    if template:
        return result
    return _maybe_auto_save(job_id, result, name, lf)


@app.post("/api/reextract/{job_id}")
def api_reextract(job_id: str, request: Request, template: str = Form(...), lf: LaserficheClient = Depends(users.lf_for)):
    _job_dir(job_id, users.current_user(request))
    try:
        return _run_extract(job_id, template, lf)
    except Exception as e:
        raise HTTPException(502, f"Extraction failed: {e}")


@app.get("/api/job/{job_id}")
def api_job(job_id: str, request: Request):
    return {"id": job_id, **json.loads((_job_dir(job_id, users.require_user(request)) / "meta.json").read_text())}


@app.get("/api/pdf/{job_id}")
def api_pdf(job_id: str, request: Request):
    return FileResponse(_job_dir(job_id, users.require_user(request)) / "doc.pdf", media_type="application/pdf")


class SaveRequest(BaseModel):
    template: str
    fields: dict[str, list[str]]
    filename: str
    folder: str | None = None   # Laserfiche folder path; defaults to LF_INBOX_PATH


def _save(job_id: str, template: str, fields: dict[str, list[str]], filename: str, lf: LaserficheClient, folder: str | None = None) -> dict:
    d = _job_dir(job_id)
    tdef = next((t for t in templates(lf) if t["name"] == template), None)
    if not tdef:
        raise HTTPException(400, f"Unknown template {template}")
    missing = [f["name"] for f in tdef["fields"] if f["required"] and not [v for v in fields.get(f["name"], []) if v]]
    if missing:
        raise HTTPException(400, "Required fields missing: " + ", ".join(missing))
    too_long = [f"{f['name']} ({max(len(v) for v in fields.get(f['name'], []))} chars, max {f['length']})"
                for f in tdef["fields"] if f.get("length") and fields.get(f["name"]) and any(len(v) > f["length"] for v in fields[f["name"]])]
    if too_long:
        raise HTTPException(400, "Too long for Laserfiche: " + "; ".join(too_long))
    meta = json.loads((d / "meta.json").read_text())
    if meta.get("lf_entry_id"):
        entry_id = int(meta["lf_entry_id"])
        lf.update_document(entry_id, template, fields, meta.get("lf_template"))
    else:
        try:
            parent = folder_entry_id(lf, folder)
        except LaserficheError as e:
            raise HTTPException(400, f"Destination folder not found: {folder or os.environ.get('LF_INBOX_PATH')} ({e})")
        entry_id = lf.import_pdf(parent, filename, (d / "doc.pdf").read_bytes(), template, fields)
    src = meta.get("source", "")
    claimed = WORK / f"{src}.claimed"
    if claimed.exists():
        shutil.move(claimed, DONE / src)
    shutil.rmtree(d, ignore_errors=True)
    return {"entry_id": entry_id, "saved": True}


@app.post("/api/save/{job_id}")
def api_save(job_id: str, req: SaveRequest, request: Request, lf: LaserficheClient = Depends(users.lf_for)):
    _job_dir(job_id, users.current_user(request))
    try:
        return _save(job_id, req.template, req.fields, req.filename, lf, req.folder)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


@app.delete("/api/job/{job_id}")
def api_discard(job_id: str, request: Request):
    d = _job_dir(job_id, users.require_user(request))
    meta = json.loads((d / "meta.json").read_text())
    claimed = WORK / f"{meta.get('source', '')}.claimed"
    if claimed.exists():
        shutil.move(claimed, FAILED / meta["source"])
    shutil.rmtree(d, ignore_errors=True)
    return {"discarded": True}


def _maybe_auto_save(job_id: str, result: dict, name: str, lf: LaserficheClient) -> dict:
    if AUTO_SAVE and result["confidence"] >= AUTO_SAVE and lf is not None:
        try:
            saved = _save(job_id, result["template"], result["fields"], result["suggested_filename"] or name, lf)
            return {**result, "auto_saved": True, **saved}
        except Exception as e:
            result["notes"] = f"Auto-save failed: {e}. " + result.get("notes", "")
            (WORK / job_id / "meta.json").write_text(json.dumps({**json.loads((WORK / job_id / "meta.json").read_text()), "notes": result["notes"]}))
    return result


def _from_mailbox(name: str, pdf: bytes, context: str) -> None:
    """Mail poller callback: extract now so the queue shows a finished job, auto-save if allowed."""
    job_id = _new_job(pdf, name, context)
    try:
        result = _run_extract(job_id, None, _svc)
        _maybe_auto_save(job_id, result, name, _svc)
    except Exception as e:  # leave the job in the queue for a human; note the error
        m = WORK / job_id / "meta.json"
        m.write_text(json.dumps({**json.loads(m.read_text()), "template": None, "fields": {}, "confidence": 0,
                                 "summary": "", "suggested_filename": "", "notes": f"Automatic read failed: {e}"}))


# ---------- backfill: documents already in Laserfiche ----------
_bf_queue: "queue.Queue[tuple[int, str]]" = queue.Queue()
_bf_state = {"queued": 0, "done": 0, "failed": 0, "errors": []}
_bf_seen: set[int] = set()


class ScanRequest(BaseModel):
    folder: str
    recursive: bool = False
    only_no_template: bool = True
    limit: int = 500


@app.post("/api/backfill/scan")
def api_backfill_scan(req: ScanRequest, lf: LaserficheClient = Depends(users.lf_for)):
    try:
        docs = lf.list_documents(req.folder, req.recursive, req.only_no_template, req.limit)
    except LaserficheError as e:
        raise HTTPException(502, str(e))
    return {"count": len(docs), "documents": [
        {"id": d["id"], "name": d.get("name"), "path": d.get("fullPath"), "template": d.get("templateName"),
         "pages": d.get("pageCount"), "queued": d["id"] in _bf_seen} for d in docs]}


@app.get("/api/lf/folders")
def api_lf_folders(path: str = "\\", lf: LaserficheClient = Depends(users.lf_for)):
    try:
        return lf.list_folders(path)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


class QueueRequest(BaseModel):
    entry_ids: list[int]


@app.post("/api/backfill/queue")
def api_backfill_queue(req: QueueRequest, request: Request):
    email = users.require_user(request)
    users.client_for(email)  # fail fast if no credentials
    n = 0
    for eid in req.entry_ids:
        if eid in _bf_seen:
            continue
        _bf_seen.add(eid)
        _bf_queue.put((eid, email))
        n += 1
    _bf_state["queued"] += n
    return {"queued": n}


@app.get("/api/backfill/status")
def api_backfill_status():
    return {**_bf_state, "pending": _bf_queue.qsize(), "errors": _bf_state["errors"][-10:]}


def _backfill_one(entry_id: int, email: str) -> None:
    lf = users.client_for(email)
    entry = lf.get_entry(entry_id)
    pdf = lf.export_pdf(entry_id, entry)
    existing = lf.entry_fields(entry_id)
    ctx = (f"This document is ALREADY in Laserfiche at: {entry.get('fullPath')}\n"
           f"Current template: {entry.get('templateName') or 'none'}\n"
           f"Existing field values (keep them unless the document clearly says otherwise): "
           f"{json.dumps(existing) if existing else 'none'}")
    job_id = _new_job(pdf, f"LF {entry_id}: {entry.get('name')}", ctx, owner=email)
    m = WORK / job_id / "meta.json"
    m.write_text(json.dumps({**json.loads(m.read_text()), "lf_entry_id": entry_id, "lf_path": entry.get("fullPath"),
                             "lf_template": entry.get("templateName"), "lf_fields": existing, "owner": email}))
    try:
        _run_extract(job_id, entry.get("templateName") or None, lf)
    except Exception as e:
        m.write_text(json.dumps({**json.loads(m.read_text()), "template": entry.get("templateName"), "fields": existing,
                                 "confidence": 0, "summary": "", "suggested_filename": "", "notes": f"Automatic read failed: {e}"}))


def _backfill_worker():
    while True:
        eid, email = _bf_queue.get()
        try:
            _backfill_one(eid, email)
            _bf_state["done"] += 1
        except Exception as e:
            _bf_state["failed"] += 1
            _bf_state["errors"].append(f"{eid}: {e}")
            _bf_seen.discard(eid)
        finally:
            _bf_queue.task_done()


@app.on_event("startup")
def _start_mail():
    if _svc is not None:
        mailbox.start_if_configured(_from_mailbox)
    elif os.environ.get("MAIL_MAILBOX"):
        logging.getLogger("lf-capture").warning("MAIL_MAILBOX is set but LF_USERNAME/LF_PASSWORD (service account) is not; mailbox capture disabled")
    for i in range(int(os.environ.get("BACKFILL_WORKERS", "2"))):
        threading.Thread(target=_backfill_worker, daemon=True, name=f"backfill-{i}").start()


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
