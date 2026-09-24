"""Watch an M365 mailbox through Microsoft Graph and turn PDF attachments into capture jobs.

Uses client-credentials (application permissions Mail.Read + Mail.ReadWrite, scoped to the
capture mailbox with New-ApplicationAccessPolicy). Processed messages move to MAIL_PROCESSED_FOLDER
so nothing is picked up twice; messages with no PDF go to MAIL_SKIPPED_FOLDER.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Callable

import httpx

log = logging.getLogger("lf-capture.mail")
GRAPH = "https://graph.microsoft.com/v1.0"


class Mailbox:
    def __init__(self) -> None:
        self.tenant = os.environ["MAIL_TENANT_ID"]
        self.client_id = os.environ["MAIL_CLIENT_ID"]
        self.secret = os.environ["MAIL_CLIENT_SECRET"]
        self.mailbox = os.environ["MAIL_MAILBOX"]
        self.processed = os.environ.get("MAIL_PROCESSED_FOLDER", "Processed")
        self.skipped = os.environ.get("MAIL_SKIPPED_FOLDER", "Skipped")
        self._http = httpx.Client(timeout=60)
        self._token: str | None = None
        self._expires = 0.0
        self._folder_ids: dict[str, str] = {}

    # ---- auth ----
    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() >= self._expires:
            r = self._http.post(
                f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                data={"client_id": self.client_id, "client_secret": self.secret, "grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"},
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._expires = time.time() + int(body.get("expires_in", 3600)) - 120
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, **params) -> dict:
        r = self._http.get(f"{GRAPH}/users/{self.mailbox}{path}", headers=self._headers(), params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict | None:
        r = self._http.post(f"{GRAPH}/users/{self.mailbox}{path}", headers=self._headers(), json=body)
        r.raise_for_status()
        return r.json() if r.content else None

    # ---- folders ----
    def _folder_id(self, name: str) -> str:
        if name not in self._folder_ids:
            found = self._get("/mailFolders", **{"$filter": f"displayName eq '{name}'"}).get("value", [])
            if found:
                self._folder_ids[name] = found[0]["id"]
            else:
                self._folder_ids[name] = self._post("/mailFolders", {"displayName": name})["id"]
        return self._folder_ids[name]

    def _move(self, msg_id: str, folder: str) -> None:
        self._post(f"/messages/{msg_id}/move", {"destinationId": self._folder_id(folder)})

    # ---- main loop ----
    def fetch_new(self) -> list[dict]:
        """Inbox messages, oldest first, with attachment metadata."""
        data = self._get(
            "/mailFolders/inbox/messages",
            **{"$orderby": "receivedDateTime asc", "$top": "20", "$select": "id,subject,from,receivedDateTime,hasAttachments,bodyPreview"},
        )
        return data.get("value", [])

    def pdf_attachments(self, msg_id: str) -> list[tuple[str, bytes]]:
        out = []
        for a in self._get(f"/messages/{msg_id}/attachments").get("value", []):
            if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            name = a.get("name") or "attachment.pdf"
            if not (name.lower().endswith(".pdf") or a.get("contentType") == "application/pdf"):
                continue
            content = a.get("contentBytes")
            if content is None:  # large attachments omit contentBytes in the list call
                r = self._http.get(f"{GRAPH}/users/{self.mailbox}/messages/{msg_id}/attachments/{a['id']}/$value", headers=self._headers())
                r.raise_for_status()
                data = r.content
            else:
                data = base64.b64decode(content)
            if data.startswith(b"%PDF"):
                out.append((name, data))
        return out

    def run_forever(self, handle: Callable[[str, bytes, str], None], interval: int) -> None:
        """handle(file_name, pdf_bytes, context_text) is called once per PDF attachment."""
        while True:
            try:
                for m in self.fetch_new():
                    sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
                    ctx = f"Email subject: {m.get('subject','')}\nFrom: {sender}\nReceived: {m.get('receivedDateTime','')}"
                    pdfs = self.pdf_attachments(m["id"]) if m.get("hasAttachments") else []
                    if not pdfs:
                        self._move(m["id"], self.skipped)
                        continue
                    for name, data in pdfs:
                        handle(name, data, ctx)
                    self._move(m["id"], self.processed)
            except Exception:
                log.exception("mailbox poll failed")
            time.sleep(interval)


def start_if_configured(handle: Callable[[str, bytes, str], None]) -> bool:
    if not os.environ.get("MAIL_MAILBOX"):
        return False
    mb = Mailbox()
    interval = int(os.environ.get("MAIL_POLL_SECONDS", "30"))
    threading.Thread(target=mb.run_forever, args=(handle, interval), daemon=True, name="mail-poller").start()
    log.info("watching mailbox %s every %ss", mb.mailbox, interval)
    return True
