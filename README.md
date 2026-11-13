# NGO Excel Enricher

Production-oriented Python CLI for enriching NGO and organisation rows from an Excel workbook.

The app is configured to use the workbook in this folder by default:

```bash
python -m app.main
```

Equivalent explicit command:

```bash
python -m app.main --input "Xperimentor NGO Master — LIVE .xlsx" --output outputs/ngo_enriched.xlsx
```

## What it does

- Detects the header row, including workbooks with title/preamble rows.
- Preserves existing columns and adds missing required output/internal columns.
- Crawls each official website with rate limiting, retries, robots.txt checks, URL deduplication, and a per-domain page cap.
- Prioritizes contact, about, team, leadership, program, CSR, partnership, impact, education, STEM, teacher, school, and annual-report pages.
- Extracts public emails and phone numbers without guessing patterns.
- Ranks likely decision-makers from public website text.
- Scores education/STEM relevance and confidence.
- Writes a formatted Excel output with filters, frozen header row, hyperlinks, wrapped notes/evidence, and review highlighting.
- Saves checkpoints every configured number of rows and supports resume.
- Logs run details to `logs/run.log` and row-level errors to `logs/errors.csv`.

## Configuration

Edit `config.yaml` for crawl limits, timeout, delay, checkpoint frequency, country defaults, and keyword lists.

## Useful commands

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run a small smoke test on two unprocessed rows:

```bash
python -m app.main --no-resume --limit 2 --output outputs/smoke.xlsx
```

Run tests:

```bash
python -m pytest
```

Create a sales-ready workbook from an already enriched file without crawling again:

```bash
python -m app.main --input outputs/ngo_enriched_live_fixed.xlsx --output outputs/ngo_sales_pipeline.xlsx --skip-crawl --sales-automation
```

This adds lead fit score, priority, segment, recommended channel, outreach readiness, pitch angle, first-email draft, LinkedIn note, objection risk, and next action. It also creates `Priority Outreach` and `Manual Review` sheets.

## DIDAC WhatsApp lead app

Visitors and staff talk to a WhatsApp number on Bird. Bird posts each message to the Flask app, which saves leads in Postgres and photos/voice notes in a private storage bucket.

```text
WhatsApp -> Bird -> /bird/webhook (Flask on Vercel) -> Supabase Postgres (leads, media, feedback, events)
                                                    -> Supabase Storage bucket (photos, voice notes)
                                                    -> OpenAI (card OCR, voice transcription)
                                    <- reply sent back through the Bird API
```

| Piece | Where it runs |
| --- | --- |
| App | Vercel (Python 3.12, region `bom1`) |
| Database | Supabase Postgres, transaction pooler (port 6543) |
| Media | Supabase Storage, private bucket `didac-media` |
| WhatsApp | Bird (Channels API) |
| OCR / transcription | OpenAI |

### Supabase setup

1. Create a project (region Mumbai to match Vercel). The Data API can stay off, the app talks to Postgres directly.
2. Project settings > Database > Connection string > **Transaction pooler**. Use it as `DATABASE_URL` (port 6543). Create the tables once with `venv/bin/python -m didac_app.cli init-db` (also after any schema change); the app does not create them on every start.
3. Storage > New bucket named `didac-media`, **private**.
4. Project settings > API keys: copy the project URL into `SUPABASE_URL` and the secret key into `SUPABASE_SECRET_KEY`. Set `SUPABASE_BUCKET=didac-media`. The secret key bypasses all access rules, so keep it server-side only.

Media rows store the bucket key (`<date>/<sender>/<file>`) in `media.local_path`. `/media/<id>` redirects to a signed link that expires after 1 hour.

### Run the tests (local Postgres in Docker)

The didac tests need a throwaway Postgres. Start one once:

```bash
docker run -d --name didac-test-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=didac_test -p 54329:5432 postgres:16-alpine
```

Then run the tests:

```bash
venv/bin/python -m pytest tests/test_didac_*.py
```

The tests ignore `DATABASE_URL` from `.env` and use `postgresql://postgres:postgres@localhost:54329/didac_test`, so they never touch Supabase. Each test starts with empty tables. To use another database, set `TEST_DATABASE_URL`; it is wiped by every test run, so never point it at real data.

Later runs: `docker start didac-test-pg` (stop with `docker stop didac-test-pg`, remove with `docker rm -f didac-test-pg`).

### Run the app

Run the Bird WhatsApp webhook locally:

```bash
cp .env.example .env
venv/bin/python -m didac_app.server
```

Fill in `BIRD_WORKSPACE_ID`, `BIRD_CHANNEL_ID`, `BIRD_ACCESS_KEY` and a long random `BIRD_WEBHOOK_TOKEN` in `.env`. Point the Bird webhook subscription (event `whatsapp.inbound`) at:

```text
https://<your-public-url>/bird/webhook?token=<BIRD_WEBHOOK_TOKEN>
```

Requests without the right token get 401. Bird retries are ignored by message id, and replies are sent through the Bird API.

### Deploy on Vercel

`index.py` in the repo root exposes the Flask app (`app`), which Vercel detects and serves for every URL. `vercel.json` sets region `bom1` (next to Supabase in Mumbai) and a 60s function limit. Do not add a rewrite to `/api/index`, it breaks the paths.

First deploy from this folder with the Vercel CLI:

```bash
npm i -g vercel
vercel login
vercel link          # creates the project; it may add .vercel and .env.local, both are gitignored
```

Add each variable (same names as `.env.example`) to the **Production** environment, with `vercel env add NAME production` or in the dashboard under Settings > Environment Variables:

`SECRET_KEY` (a long random string that signs login sessions), `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_BUCKET`, `SUPABASE_SECRET_KEY`, `BIRD_WORKSPACE_ID`, `BIRD_CHANNEL_ID`, `BIRD_ACCESS_KEY`, `BIRD_WEBHOOK_TOKEN`, `OPENAI_API_KEY`, `PUBLIC_BASE_URL` (the production URL, no trailing slash), plus `DIDAC_EXPORT_DIR=/tmp/didac` and `DIDAC_MEDIA_DIR=/tmp/didac_media` (Vercel's disk is read-only except `/tmp`).

Then deploy and check:

```bash
vercel deploy --prod
curl https://<your-app>.vercel.app/health
```

Env var changes only apply to new deployments, so redeploy after changing one. Finally set the Bird webhook subscription (event `whatsapp.inbound`) to `https://<your-app>.vercel.app/bird/webhook?token=<BIRD_WEBHOOK_TOKEN>` and send "hi" to the WhatsApp number.

Auth: everything except `/health` and `/bird/webhook` needs a login (phone number or email + password). Accounts live in the `users` table; admins add, deactivate and reset passwords on the Team page. `/bird/webhook` is protected by its `?token=`. If `SECRET_KEY` is unset the dashboard answers 503 and the webhook keeps working.

Create the first admin once, against the production database (`DATABASE_URL` in your `.env`):

```bash
venv/bin/python -m didac_app.cli create-admin --name "Your Name" --phone "+91XXXXXXXXXX" --email you@example.com
```

Debugging a deploy:

```bash
vercel logs --since 30m --query 'level:error' --limit 20
```

Also check the Inbound Events sheet in the report export: each message has a status (`received`, `processed`, or an error) and the error text.

Known limits on Vercel:

- Hobby plan is non-commercial use only; use Pro or a team account for real commercial use.
- The 60s limit covers the whole webhook call (download, OCR, reply). QR decoding is capped (1600px, 8s) to stay inside it.
- A cold start re-runs the table-creation SQL (about a second extra).
- Free-form WhatsApp replies only work within 24 hours of the person's last message (Bird/WhatsApp rule).
- To change the app URL (project rename or custom domain), then also update the Bird webhook, `PUBLIC_BASE_URL`, and redeploy.

Staff can send:

```text
New lead
Name: Ramesh
Org: XYZ Foundation
Role: CSR Head
City: Delhi
Interest: STEM lab for govt schools
Budget: 20 schools
Timeline: Q1
```

Staff commands (staff only; visitors typing a slash command just get the menu):

```text
/help
/summary
/export excel
```

Visitor flow (tap list rows or type the label; numbers are not accepted):

```text
Visitor: hi
Bot: list - Download brochure, Share my requirement, Feedback / voice note
Visitor: Download brochure -> TiQer
Bot: sends the TiQer brochure link
Visitor: Share my requirement
Bot: asks name, email (can be skipped), organisation, type, city and state, then the requirement, and saves one lead
```

Staff flow (a phone that is active on the Team page): the menu is Contact form, Other docs (cards, etc.), Event badge / QR, Add a note. Send photos (a contact form may include the visiting card stapled to it), reply DONE, then Save, Edit or Retake. Edit takes `Field: value` lines, including `Priority: High`. Tapping Save asks for a priority (High, Medium, Low) unless it was set in Edit; the lead is saved only after that. The "I read this as" summary is never shortened: it goes out as one message, or as one plain message plus the buttons when it is too long for a button message (about 1000 characters). Type BACK to stop editing, cancel to stop a flow.

Notes always go to the sender's own most recent lead, and only if it was saved or edited in the last 30 minutes. With no such lead, a note with a name, phone or email starts a new "note only" lead.

Visitor leads have no priority (shown as Unclassified); staff set it on the lead page.

WhatsApp lists: Bird turns each list item into a section, so every menu is sent as ONE item holding all rows (otherwise every title shows twice). WhatsApp shows only a row's title, cut at 24 characters, and never its description.

### Follow-up message to captured leads

Admins open **Follow-up** in the dashboard bar. **Send next batch** sends the approved WhatsApp template `didac_thank_you` (Marketing; body "thank you for visiting Xperimentor at DIDAC", footer "Reply STOP...", quick-reply buttons `Share my requirement` and `Download brochure`, whose labels must match the visitor menu rows) to every distinct mobile number among the leads. A lead page also has **Send follow-up** for one lead. Nothing sends automatically.

- One message per number, ever (`followups` table, primary key = E.164 number). Landlines, Team numbers and numbers that replied STOP or unsubscribe are skipped. A failed send gives the number back.
- 09:00 to 21:00 IST only; at most 40 per click and `FOLLOWUP_DAILY_CAP` (default 100) per 24 hours; the batch stops at the first error.
- Bird accepting a send only means "accepted". The Follow-up page asks Bird for each message's final status (delivered, read, failed with Meta's reason) when it loads. Meta may refuse a marketing message to someone who never messaged us (error 131049); that is Meta's decision per recipient.
- Env vars: `BIRD_FOLLOWUP_TEMPLATE_ID` (the template's Bird project id), `BIRD_FOLLOWUP_TEMPLATE_VERSION` (default `latest`), `FOLLOWUP_DAILY_CAP`. Blank template id disables the feature. The Bird access key can only send and read messages; it cannot create or manage templates (403), so templates are made in the Bird dashboard.

### Dashboard

Leads page: tabs All (default), Contact forms, Event badges, Other documents; one **Filter** control (captured by Everyone/Team/Visitors, priority High/Medium/Low/Unclassified); a coloured Priority chip in the table and the preview panel. Lead page: edit every field including Priority, photo **Rotate left/right** (the angle is saved on the photo row, the stored file is never changed), Send follow-up and Delete lead (admins), in-page confirm dialogs. Every password box has a Show/Hide button.

Brochure links come from the env vars `BROCHURE_URL`, `SIA_BROCHURE_URL`, `TIQER_BROCHURE_URL`, `PLAY_LAB_BROCHURE_URL`, `STREAM_BROCHURE_URL`, `LAB_IN_CLASSROOM_BROCHURE_URL`. Until real PDFs exist they default to sample PDFs (see `config.py`). Change one with `vercel env rm/add <NAME> production`, then redeploy.

Deploys: commit to `prod` on the fork and push; Vercel deploys on push.

Dashboard (locally `http://127.0.0.1:5001`, deployed `https://<your-app>.vercel.app`):

```text
/dashboard
/brochures
/export/report
/export/report.xlsx
```

The CLI uses `DATABASE_URL` from `.env`, so these commands act on whatever database it points to (Supabase for real use).

Export manually:

```bash
venv/bin/python -m didac_app.cli export --output outputs/didac/didac_leads.xlsx
```

Clear all test leads, media rows, feedback and events before the live event (this wipes the whole database, the bucket files stay):

```bash
venv/bin/python -m didac_app.cli reset-sample
```

Optional OpenAI intelligence:

```bash
OPENAI_API_KEY=...
OPENAI_VISION_MODEL=gpt-5-mini
OPENAI_VISION_EFFORT=low   # optional: faster scan reading; leave unset for the model's default
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
```

With `OPENAI_API_KEY` set, WhatsApp images are processed with vision OCR into lead fields, voice notes are transcribed into notes/lead fields. Badge QR codes in photos are decoded with OpenCV. vCard and MECARD QR codes become lead fields; other QR URLs are recorded and, if they point at a public address, their page text is read (private addresses and redirects to them are blocked). The file is copied to a temp file for processing and deleted right after; the original stays in the bucket. If OpenAI fails, the message is still saved with a "pending" placeholder instead of being lost.

Repeat scans: a new card whose email or phone matches a saved lead, and whose name does not contradict it, fills that lead's blank fields instead of creating a second lead ("Already saved as #N"). A match on name + organisation only, or on a shared phone/email with a different name, is saved as a new lead with a "possible duplicate of #N" badge for you to check. Two scans of the same new person at the same instant can still both create a lead.

Export for Orbit: the **Export** page builds a CSV or Excel file for Orbit CRM's Organisations → Import (one row per lead, `name` = organisation). Filter by capture date (India time), who captured it, and organisation type. Leads Orbit would reject (no organisation name, no organisation type, invalid email) are listed on the page and left out; fix them on the lead page and export again. Orbit takes at most 500 organisations per file and skips ones it already has. Organisation type is one fixed list, the same as Orbit's `type`: SCHOOL, SCHOOL_CHAIN, CSR, NGO, GOVERNMENT, DISTRIBUTOR, INSTITUTION, VENDOR, OTHER (the last four must exist in Orbit before importing leads of those types).

## Notes

LinkedIn discovery is intentionally limited to public links found on crawled pages. The app does not authenticate to LinkedIn or scrape private LinkedIn content.

AI extraction is represented by the `AIProvider` interface in `app/ai_provider.py`; crawler and parser logic do not depend on any one AI vendor.
