# "Read with LF Capture" from inside Laserfiche — setup

Right-click a folder or selected documents in the Laserfiche client → **Start Business Process → Read with LF Capture**.
The tool reads each document, writes the template and fields straight onto the entry, and tags it
`AI-Proposed` (plus `AI-Unsure` when confidence is below the bar, or `AI-Failed` if it could not read it).
The user checks the metadata pane, fixes anything, and removes the tag. Everything stays in Laserfiche.

## 1. Tool side (once)
1. `.env` on the tools server — add:
   ```
   LF_WORKFLOW_TOKEN=<output of: openssl rand -hex 24>
   ```
   (keep `LF_USERNAME`/`LF_PASSWORD` set — the service account is the fallback when the person who
   started the process has never signed in to LF Capture.)
2. `git pull && docker compose up -d --build`
3. nginx — add `nginx-lfcapture-workflow.conf` inside the 443 server block, above the `/lfcapture/` block,
   set the `allow` line to the Laserfiche server's IP or subnet, then `nginx -t && systemctl reload nginx`.
   Test from the LF server (PowerShell):
   ```powershell
   Invoke-RestMethod -Method POST -Uri https://tools.northernfruit.com/lfcapture/api/lf/read `
     -Headers @{ Authorization = "Bearer <token>" } -ContentType "application/json" `
     -Body '{"entry_id": 538920, "user": "northernfruit\\juans"}'
   ```
   Expect `{"queued":1,"acting_as":"northernfruit\\juans","mode":"keep"}` and, a few seconds later, the tag on that entry.

## 2. Laserfiche side
### Tags (Repository Administration → Tags)
Create three **informational** tags: `AI-Proposed`, `AI-Unsure`, `AI-Failed`.
Give the group that will use this the right to apply/remove them.

### Workflow (Workflow Designer)
Create a workflow **Read with LF Capture**:

1. **Starting rule**: *Business Process* — allow it to be started on **documents and folders**, from the client.
   Under the business process settings, allow multiple starting entries (the client starts one instance per entry).
2. Activity **HTTP Web Request**:
   - Method: `POST`
   - URL: `https://tools.northernfruit.com/lfcapture/api/lf/read`
   - Headers:
     `Authorization: Bearer <the LF_WORKFLOW_TOKEN>`
     `Content-Type: application/json`
   - Body (JSON):
     ```json
     {"entry_id": %(Entry ID), "user": "%(Initiator)", "mode": "keep"}
     ```
     `%(Entry ID)` and `%(Initiator)` are the starting-entry and business-process-initiator tokens — pick them
     from the token dialog in your Workflow version if the names differ slightly.
     Use `"recursive": true` if you want a folder read to include subfolders.
3. (Optional) Activity **Track Tokens** / **Write to Log** with the response, so failures show up in the Workflow log.
4. Publish. It appears in the client under *Start Business Process*.

Make a second copy named **Re-read with LF Capture** with `"mode": "overwrite"` for documents whose
template or values are wrong — it re-classifies and replaces values (the human still reviews via the tag).

### Optional: clean-up automation
- A **saved search** `Tag = AI-Proposed` is the review queue inside Laserfiche.
- A tiny workflow with starting rule *Field changed / metadata modified on entries tagged AI-Proposed* that
  removes the tag when someone edits the fields, so tags clear themselves after review.

## What the person sees
Right-click → Start Business Process → Read with LF Capture. Refresh after a few seconds: template and
fields are filled, the entry shows the `AI-Proposed` tag (and `AI-Unsure` if it wasn't confident).
Check, adjust, remove the tag. The Laserfiche audit trail shows the AI write under that person's own
account (if they've signed in to LF Capture before) and the human edit after it.
