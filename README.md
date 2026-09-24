# LF Capture

Reads a PDF with Claude, picks the matching Laserfiche template (or lets a person pick),
fills the template fields, and imports the document into Laserfiche with the metadata set.
Templates and fields are pulled live from the repository, so adding a template in LF
adds it here with no code change.

## How it flows
1. PDF arrives — emailed to the capture mailbox (AS400 path), dropped in `inbox/`, or uploaded in the browser.
2. Claude reads the PDF (visually, so scans/faxes work) and returns `template + fields + confidence`
   through a JSON schema generated from your template definitions.
3. Review page: PDF on the left, fields on the right. Change the template and it re-reads.
4. **Save to Laserfiche** creates the document under `LF_INBOX_PATH` with the template and
   fields applied. Your existing Workflow routes it from there (Order Number is sent multi-value).
5. Source file moves to `inbox/_done` (or `_failed` on Discard).

Set `AUTO_SAVE_CONFIDENCE=0.9` to skip review for confident results with all required fields filled.

## Who talks to Laserfiche
Each person uses their **own** Laserfiche account: identity comes from the M365 sign-in at nginx
(`X-Auth-Request-Email`), and the first visit asks for a Laserfiche username/password in **Settings**.
The password is stored encrypted with `APP_SECRET` in the work volume (`users.json`) so the app can
re-login when Laserfiche's token expires. Reads, backfills and saves all run as that user, so
Laserfiche's own rights and audit trail apply. `LF_USERNAME`/`LF_PASSWORD` (service account) are only
needed for the unattended mailbox capture.

## Backfill — documents already in Laserfiche
Header → **Backfill**. Enter a folder path (`\Sales\Orders\80552-00`), optionally include subfolders,
choose "documents with no template" or all, **Scan**, tick the ones you want, **Queue for review**.
Each document is exported (edoc if it is a PDF, otherwise the LF pages rendered to PDF), read by Claude
with its current path/template/field values as context, and dropped into the normal review queue.
**Update metadata in Laserfiche** sets the template and fields on the existing entry in place — no
re-import, entry ID and history unchanged. Nothing is written until you press Update on each one.
Template descriptions and field descriptions in Laserfiche are passed to Claude; fill them in to steer it.

## Mailbox capture (AS400 path)
Point the AS400 (or any sender) at a capture mailbox and set the `MAIL_*` values in `.env`.
The tool polls it through Microsoft Graph, pulls every PDF attachment into the queue with the
subject/sender/date as extra context for Claude, and moves the message to `Processed`
(`Skipped` if it had no PDF). One-time setup, same pattern as the LF import profile:
1. Entra app registration → API permissions → Microsoft Graph, application: `Mail.Read`, `Mail.ReadWrite` → admin consent.
2. Scope it: `New-ApplicationAccessPolicy -AppId <client id> -PolicyScopeGroupId <mail-enabled group containing the capture mailbox> -AccessRight RestrictAccess`.
3. Client secret → `MAIL_CLIENT_SECRET`; set a calendar reminder for its expiry.

## Run
```bash
cp .env.example .env   # fill in LF service account + ANTHROPIC_API_KEY
docker compose up -d --build
# then add nginx-lfcapture.conf to the tools.northernfruit.com server block -> https://tools.northernfruit.com/lfcapture/
```
Dev without Docker: `pip install -r requirements.txt && uvicorn app.main:app --reload --port 8080`

## Laserfiche API
Written for the self-hosted **Repository API v2** (your server: build 2.0.2603.46).
- Auth: `POST /v2/Repositories/{repo}/Token` (form: grant_type=password) → bearer token; re-logs in on any 401.
- Import: `POST /v2/Repositories/{repo}/Entries/{parentId}/Folder/Import` with `pdfOptions.generatePages`
  (set `LF_GENERATE_PAGES=0` to store the PDF only, no LF pages).

Create `LF_INBOX_PATH` in the repo and give the service account create rights on it.

## Cost
Sonnet reads a typical 1–3 page PDF for roughly 1–3¢. At 200 pages/day that is a few dollars a month.
Change `CLAUDE_MODEL` in `.env` if you want a different model.

## Endpoints
`GET /api/templates` · `GET /api/queue` · `POST /api/extract` (file or inbox_name, optional template) ·
`POST /api/reextract/{id}` · `GET /api/job/{id}` · `GET /api/pdf/{id}` · `POST /api/save/{id}` · `DELETE /api/job/{id}`
