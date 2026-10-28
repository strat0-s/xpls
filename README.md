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

The app stores data in Postgres. Set `DATABASE_URL` in `.env` (Supabase for real use).

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

Commands:

```text
/summary
/hot
/followups
/export excel
/send brochure 9845xxxxx
```

Visitor QR flow:

```text
Visitor: hi
Bot: shows menu
Visitor: 1
Bot: shows brochure options
Visitor: 2
Bot: sends TiQer brochure link
```

Lead capture flow:

```text
Visitor: menu
Visitor: 2
Bot: asks role
Visitor: CSR
Bot: asks city/state
Visitor: Bangalore
Bot: asks requirement
Visitor: science lab for 20 schools
Bot: saves and scores the lead
```

Dashboard:

```text
http://127.0.0.1:5001/dashboard
http://127.0.0.1:5001/brochures
http://127.0.0.1:5001/export/report
```

Export manually:

```bash
venv/bin/python -m didac_app.cli export --output outputs/didac/didac_leads.xlsx
```

Clear local test leads before the live event:

```bash
venv/bin/python -m didac_app.cli reset-sample
```

Optional OpenAI intelligence:

```bash
OPENAI_API_KEY=...
OPENAI_VISION_MODEL=gpt-5-mini
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_TTS_VOICE=alloy
```

With `OPENAI_API_KEY` set, WhatsApp images are processed with vision OCR into lead fields, voice notes are transcribed into notes/lead fields, and `/tts your text` creates an MP3 file.

Media cap:

```bash
MAX_MEDIA_PER_MESSAGE=3
```

The bot processes only the first 3 images/voice notes in one WhatsApp message and asks the sender to send additional files separately.

## Notes

LinkedIn discovery is intentionally limited to public links found on crawled pages. The app does not authenticate to LinkedIn or scrape private LinkedIn content.

AI extraction is represented by the `AIProvider` interface in `app/ai_provider.py`; crawler and parser logic do not depend on any one AI vendor.
