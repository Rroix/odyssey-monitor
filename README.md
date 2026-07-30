# Odyssey IMAX 70mm seat monitor — Railway + ntfy

This worker checks AMC and Fandango every 15 seconds for **AMC Lincoln Square 13**, explicit **IMAX 70mm**, and at least one enabled standard seat. It excludes wheelchair, accessible/ADA, companion-only, occupied, sold, reserved, blocked, and disabled seats.

It sends an urgent ntfy notification whose tap action opens the booking page. It does **not** store payment data or complete a purchase.

## Dates

The date range is generated dynamically from **tomorrow in America/New_York** through `END_DATE` (default `2026-08-06`).

## Reliability

- Chromium context refresh every 45 minutes.
- Full Chromium restart every 4 hours.
- Three navigation attempts with exponential backoff.
- Detects 403, 429, CAPTCHA, anti-bot, and access-denied pages.
- Persistent SQLite alert deduplication at `/data/state.db`.
- `/health` endpoint for Railway.
- Urgent ntfy warning after five minutes without a successful page check.
- Recovery notification when successful checks resume.
- Railway process restart policy.

## Environment variables

Copy values from `.env.example`. `NTFY_TOPIC` is required. `NTFY_TOKEN` is optional.

## Local Docker test

```bash
docker build -t odyssey-monitor .
docker run --rm --init --ipc=host \
  -p 8080:8080 \
  --env-file .env \
  -v odyssey-data:/data \
  odyssey-monitor
```

Then open `http://localhost:8080/health`.

## Test the phone notification

Inside the container or locally with dependencies installed:

```bash
python test_notification.py
```

## Important limitation

Ticket sites change their HTML and may block cloud-hosting IP addresses or show CAPTCHA. The monitor reports prolonged failure instead of silently stopping, but no scraper can guarantee uninterrupted access. A notification means the seat appeared available at that instant; it may be taken before checkout completes.
