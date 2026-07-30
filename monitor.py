from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))
END_DATE = date.fromisoformat(os.getenv("END_DATE", "2026-08-06"))
CHECK_INTERVAL_SECONDS = max(15, int(os.getenv("CHECK_INTERVAL_SECONDS", "15")))
CONCURRENCY = max(1, min(6, int(os.getenv("CONCURRENCY", "3"))))
CONTEXT_REFRESH_SECONDS = int(os.getenv("CONTEXT_REFRESH_SECONDS", "2700"))
BROWSER_REFRESH_SECONDS = int(os.getenv("BROWSER_REFRESH_SECONDS", "14400"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "30000"))
HEALTH_STALE_SECONDS = int(os.getenv("HEALTH_STALE_SECONDS", "300"))
HEALTH_WARNING_SECONDS = int(os.getenv("HEALTH_WARNING_SECONDS", "300"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/state.db"))
def resolve_port() -> int:
    """Use the host-provided public port, then a portable fallback."""
    raw = (os.getenv("PORT") or os.getenv("HEALTH_PORT") or "8080").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid PORT/HEALTH_PORT value: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"PORT/HEALTH_PORT must be between 1 and 65535, got {port}")
    return port


def detect_hosting_platform() -> str:
    if os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"):
        return "Render"
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        return "Railway"
    if os.getenv("FLY_APP_NAME"):
        return "Fly.io"
    if os.getenv("KOYEB_APP_NAME"):
        return "Koyeb"
    if os.getenv("NORTHFLANK_PROJECT_ID"):
        return "Northflank"
    return "generic Docker/local"


PORT = resolve_port()
HOSTING_PLATFORM = detect_hosting_platform()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()
SEND_STARTUP_NOTIFICATION = os.getenv("SEND_STARTUP_NOTIFICATION", "true").lower() in {"1", "true", "yes"}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
)

AMC_TEMPLATE = "https://www.amctheatres.com/movies/the-odyssey-76238/showtimes?date={date}"
FANDANGO_TEMPLATE = "https://www.fandango.com/the-odyssey-the-imax-70mm-experience-2026-241386/movie-overview?date={date}"

THEATER_TERMS = ("amc lincoln square 13", "lincoln square 13")
FORMAT_TERMS = ("imax 70mm", "imax 70 mm", "70mm imax", "70 mm imax")
EXCLUDED_FORMAT_TERMS = ("imax with laser", "imax laser", "digital imax")
ACCESSIBLE_TERMS = (
    "wheelchair", "accessible", "accessibility", "ada", "companion seat", "companion-only",
    "wheel chair", "handicap", "mobility", "transfer seat",
)
UNAVAILABLE_TERMS = (
    "unavailable", "occupied", "sold", "reserved", "taken", "disabled", "blocked", "not available",
)
SEAT_TERMS = ("seat", "row", "recliner")
BOOKING_TERMS = ("showtime", "tickets", "buy", "reserve", "select seats", "continue")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("odyssey-monitor")


@dataclass(frozen=True)
class Showtime:
    source: str
    date_iso: str
    label: str
    url: str


@dataclass(frozen=True)
class SeatMatch:
    showtime: Showtime
    seat_label: str
    booking_url: str


class RuntimeState:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.last_success_at = 0.0
        self.last_round_at = 0.0
        self.last_error = ""
        self.rounds = 0
        self.pages_ok = 0
        self.pages_failed = 0
        self.alerts_sent = 0
        self.health_warning_sent = False
        self.shutting_down = False
        self.lock = threading.Lock()

    def success(self) -> None:
        with self.lock:
            self.last_success_at = time.time()
            self.last_error = ""

    def error(self, message: str) -> None:
        with self.lock:
            self.last_error = message[:500]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            startup_grace = now - self.started_at <= 180
            healthy = (
                not self.shutting_down
                and (
                    startup_grace
                    or (self.last_success_at > 0 and now - self.last_success_at <= HEALTH_STALE_SECONDS)
                )
            )
            return {
                "status": "ok" if healthy else "degraded",
                "uptime_seconds": round(now - self.started_at),
                "seconds_since_success": None if not self.last_success_at else round(now - self.last_success_at),
                "last_round_at": self.last_round_at,
                "last_error": self.last_error,
                "rounds": self.rounds,
                "pages_ok": self.pages_ok,
                "pages_failed": self.pages_failed,
                "alerts_sent": self.alerts_sent,
                "date_range": get_date_range_labels(),
            }


runtime = RuntimeState()
stop_event = asyncio.Event()


def get_monitor_dates(now: datetime | None = None) -> list[date]:
    current = (now or datetime.now(TZ)).astimezone(TZ).date()
    start = current + timedelta(days=1)
    if start > END_DATE:
        return []
    return [start + timedelta(days=i) for i in range((END_DATE - start).days + 1)]


def get_date_range_labels() -> dict[str, str | None]:
    dates = get_monitor_dates()
    return {
        "start": dates[0].isoformat() if dates else None,
        "end": dates[-1].isoformat() if dates else None,
    }


def normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def is_qualifying_context(text: str) -> bool:
    t = normalize(text)
    return (
        any(term in t for term in THEATER_TERMS)
        and any(term in t for term in FORMAT_TERMS)
        and not any(term in t for term in EXCLUDED_FORMAT_TERMS)
    )


def is_standard_available_seat(text: str, disabled: bool = False) -> bool:
    import re

    t = normalize(text)
    if disabled or not t or len(t) > 350:
        return False
    if any(term in t for term in ACCESSIBLE_TERMS):
        return False
    if any(term in t for term in UNAVAILABLE_TERMS):
        return False
    seat_code = bool(re.search(r"\b(?:seat\s*)?[a-z]{1,3}\s*[- ]?\d{1,3}\b", t))
    explicitly_available = any(term in t for term in ("available", "select seat", "choose seat", "open seat"))
    seat_semantics = any(term in t for term in SEAT_TERMS)
    return seat_code or (seat_semantics and explicitly_available)


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS alerts (fingerprint TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.conn.commit()
        self.lock = threading.Lock()

    def seen(self, fingerprint: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM alerts WHERE fingerprint = ?", (fingerprint,)).fetchone()
            return row is not None

    def record(self, fingerprint: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO alerts(fingerprint, created_at, payload) VALUES (?, ?, ?)",
                (fingerprint, datetime.now(TZ).isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.commit()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health", "/healthz"}:
            self.send_response(404)
            self.end_headers()
            return

        snapshot = runtime.snapshot()
        status = 200 if snapshot["status"] == "ok" else 503
        body = json.dumps(snapshot).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health", "/healthz"}:
            self.send_response(404)
            self.end_headers()
            return

        snapshot = runtime.snapshot()
        status = 200 if snapshot["status"] == "ok" else 503

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        return


def start_health_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    log.info("Health server listening on 0.0.0.0:%s (%s)", PORT, HOSTING_PLATFORM)
    return server


async def notify(title: str, message: str, *, click: str | None = None, priority: str = "high", tags: str = "movie_camera") -> bool:
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC is not configured; notification not sent")
        return False
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    if click:
        headers["Click"] = click
        headers["Actions"] = f"view, Open booking page, {click}, clear=true"
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.post(url, content=message.encode("utf-8"), headers=headers)
            response.raise_for_status()
        return True
    except Exception as exc:
        runtime.error(f"ntfy failure: {exc}")
        log.exception("Failed to send ntfy notification")
        return False


async def configure_page(page: Page) -> None:
    await page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "media", "font"}
        else route.continue_(),
    )


async def goto_resilient(page: Page, url: str) -> bool:
    for attempt in range(3):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = response.status if response else 0
            if status in {403, 429}:
                delay = min(120, (2 ** attempt) * 15 + random.uniform(1, 5))
                log.warning("Rate limited (%s) on %s; backing off %.1fs", status, url, delay)
                await asyncio.sleep(delay)
                continue
            await page.wait_for_timeout(1500)
            body = normalize(await page.locator("body").inner_text(timeout=5000))
            if any(marker in body for marker in ("captcha", "verify you are human", "access denied", "unusual traffic")):
                delay = min(180, (2 ** attempt) * 30 + random.uniform(3, 10))
                log.warning("Anti-bot page on %s; backing off %.1fs", url, delay)
                await asyncio.sleep(delay)
                continue
            runtime.success()
            runtime.pages_ok += 1
            return True
        except (PlaywrightTimeoutError, Exception) as exc:
            runtime.pages_failed += 1
            runtime.error(f"navigation failed: {type(exc).__name__}: {exc}")
            if attempt == 2:
                log.warning("Navigation failed after retries: %s", url)
                return False
            await asyncio.sleep((attempt + 1) * 3 + random.uniform(0, 2))
    return False


DISCOVER_JS = r"""
() => {
  const visible = el => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const out = [];
  const controls = [...document.querySelectorAll('a[href], button, [role="button"]')].filter(visible);
  for (const el of controls) {
    let node = el;
    let context = '';
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      const txt = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
      if (txt.length > context.length && txt.length <= 2500) context = txt;
    }
    const label = [el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
      .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
    const anchor = el.closest('a[href]');
    const href = anchor?.href || el.getAttribute('data-url') || el.getAttribute('data-href') || '';
    const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true';
    out.push({label, context, href, disabled});
  }
  return out;
}
"""

SEATS_JS = r"""
() => {
  const visible = el => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const selectors = [
    'button', '[role="button"]', '[role="checkbox"]', '[data-seat]', '[data-seat-id]',
    '[class*="seat" i]', '[id*="seat" i]', 'svg [aria-label]'
  ];
  const nodes = [...new Set(selectors.flatMap(s => [...document.querySelectorAll(s)]))];
  return nodes.filter(visible).map(el => {
    const parentText = (el.parentElement?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500);
    const label = [
      el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
      el.getAttribute('data-seat'), el.getAttribute('data-seat-id'), el.getAttribute('data-label'),
      el.className?.baseVal || el.className, parentText
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().slice(0, 1000);
    const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' ||
      el.getAttribute('data-available') === 'false' || el.classList.contains('disabled');
    return {label, disabled};
  });
}
"""


async def discover_showtimes(page: Page, source: str, target_date: date, url: str) -> list[Showtime]:
    if not await goto_resilient(page, url):
        return []
    controls: list[dict[str, Any]] = await page.evaluate(DISCOVER_JS)
    found: dict[str, Showtime] = {}
    for item in controls:
        combined = f"{item.get('context', '')} {item.get('label', '')}"
        if item.get("disabled") or not is_qualifying_context(combined):
            continue
        label = normalize(item.get("label", ""))
        if not any(term in label or term in normalize(combined) for term in BOOKING_TERMS):
            continue
        href = item.get("href") or ""
        if not href:
            continue
        absolute = urljoin(page.url, href)
        if absolute.startswith("javascript:"):
            continue
        found[absolute] = Showtime(source, target_date.isoformat(), item.get("label") or "IMAX 70mm showtime", absolute)
    return list(found.values())


async def find_available_standard_seat(page: Page, showtime: Showtime) -> SeatMatch | None:
    if not await goto_resilient(page, showtime.url):
        return None
    # Some booking pages need a short hydration period or redirect chain.
    for _ in range(3):
        entries: list[dict[str, Any]] = await page.evaluate(SEATS_JS)
        for entry in entries:
            label = entry.get("label", "")
            if is_standard_available_seat(label, bool(entry.get("disabled"))):
                return SeatMatch(showtime, " ".join(label.split())[:180], page.url)
        await page.wait_for_timeout(1500)
    return None


def fingerprint(match: SeatMatch) -> str:
    raw = f"{match.showtime.date_iso}|{match.booking_url}|{normalize(match.seat_label)}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def check_source(context: BrowserContext, semaphore: asyncio.Semaphore, source: str, target_date: date, url: str) -> list[Showtime]:
    async with semaphore:
        page = await context.new_page()
        try:
            await configure_page(page)
            return await discover_showtimes(page, source, target_date, url)
        finally:
            await page.close()


async def check_seat(context: BrowserContext, semaphore: asyncio.Semaphore, showtime: Showtime) -> SeatMatch | None:
    async with semaphore:
        page = await context.new_page()
        try:
            await configure_page(page)
            return await find_available_standard_seat(page, showtime)
        finally:
            await page.close()


async def send_match(db: StateDB, match: SeatMatch) -> None:
    fp = fingerprint(match)
    if db.seen(fp):
        return
    message = (
        f"A standard non-wheelchair seat appears available.\n"
        f"Date: {match.showtime.date_iso}\n"
        f"Source: {match.showtime.source}\n"
        f"Seat: {match.seat_label}\n\n"
        "Tap immediately to open the booking page. Availability may disappear quickly."
    )
    sent = await notify(
        "THE ODYSSEY — SEAT AVAILABLE",
        message,
        click=match.booking_url,
        priority="urgent",
        tags="rotating_light,movie_camera,ticket",
    )
    if sent:
        db.record(fp, {"url": match.booking_url, "seat": match.seat_label, "date": match.showtime.date_iso})
        runtime.alerts_sent += 1
        log.warning("SEAT ALERT SENT: %s | %s", match.showtime.date_iso, match.booking_url)


async def create_browser(playwright: Any) -> Browser:
    return await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--no-sandbox",
        ],
    )


async def create_context(browser: Browser) -> BrowserContext:
    return await browser.new_context(
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        viewport={"width": 1440, "height": 1100},
        java_script_enabled=True,
    )


async def run_monitor() -> None:
    dates = get_monitor_dates()
    if not dates:
        await notify("Odyssey monitor finished", "The configured date range has ended.", priority="default", tags="checkered_flag")
        log.info("No dates remain between tomorrow and %s", END_DATE)
        return
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC must be configured")

    db = StateDB(STATE_PATH)
    if SEND_STARTUP_NOTIFICATION:
        await notify(
            "Odyssey monitor online",
            f"Monitoring AMC Lincoln Square 13 IMAX 70mm standard seats every {CHECK_INTERVAL_SECONDS}s, "
            f"from {dates[0]} through {dates[-1]}.",
            priority="default",
            tags="white_check_mark,movie_camera",
        )

    async with async_playwright() as pw:
        browser: Browser | None = None
        context: BrowserContext | None = None
        browser_started = 0.0
        context_started = 0.0
        last_heartbeat = 0.0
        last_any_success = time.time()
        restart_failures = 0

        while not stop_event.is_set():
            round_start = time.monotonic()
            runtime.last_round_at = time.time()
            runtime.rounds += 1
            try:
                now_mono = time.monotonic()
                if browser is None or not browser.is_connected() or now_mono - browser_started >= BROWSER_REFRESH_SECONDS:
                    if context:
                        await context.close()
                    if browser:
                        await browser.close()
                    browser = await create_browser(pw)
                    context = await create_context(browser)
                    browser_started = context_started = now_mono
                    log.info("Browser started/refreshed")
                elif context is None or now_mono - context_started >= CONTEXT_REFRESH_SECONDS:
                    if context:
                        await context.close()
                    context = await create_context(browser)
                    context_started = now_mono
                    log.info("Browser context refreshed")

                assert context is not None
                semaphore = asyncio.Semaphore(CONCURRENCY)
                source_jobs = []
                for d in get_monitor_dates():
                    source_jobs.append(check_source(context, semaphore, "AMC", d, AMC_TEMPLATE.format(date=d.isoformat())))
                    source_jobs.append(check_source(context, semaphore, "Fandango", d, FANDANGO_TEMPLATE.format(date=d.isoformat())))
                results = await asyncio.gather(*source_jobs, return_exceptions=True)
                showtimes: dict[str, Showtime] = {}
                for result in results:
                    if isinstance(result, Exception):
                        runtime.error(f"source check error: {result}")
                        continue
                    for st in result:
                        showtimes[st.url] = st

                seat_jobs = [check_seat(context, semaphore, st) for st in showtimes.values()]
                seat_results = await asyncio.gather(*seat_jobs, return_exceptions=True)
                for result in seat_results:
                    if isinstance(result, Exception):
                        runtime.error(f"seat check error: {result}")
                    elif result:
                        await send_match(db, result)

                if runtime.last_success_at > last_any_success:
                    last_any_success = runtime.last_success_at
                    restart_failures = 0
                    if runtime.health_warning_sent:
                        await notify(
                            "Odyssey monitor recovered",
                            "The monitor can reach the ticket sites again and has resumed normal checks.",
                            priority="high",
                            tags="white_check_mark,movie_camera",
                        )
                        runtime.health_warning_sent = False

                if time.time() - last_any_success >= HEALTH_WARNING_SECONDS and not runtime.health_warning_sent:
                    await notify(
                        "Odyssey monitor unhealthy",
                        f"No successful ticket-page check for {round((time.time()-last_any_success)/60)} minutes. "
                        "The service is restarting its browser automatically. Check Railway logs if this continues.",
                        priority="urgent",
                        tags="warning,movie_camera",
                    )
                    runtime.health_warning_sent = True
                    if context:
                        await context.close(); context = None
                    if browser:
                        await browser.close(); browser = None

                if time.time() - last_heartbeat >= 60:
                    snap = runtime.snapshot()
                    log.info(
                        "Heartbeat: no new seat alert | rounds=%s pages_ok=%s pages_failed=%s showtimes=%s",
                        snap["rounds"], snap["pages_ok"], snap["pages_failed"], len(showtimes),
                    )
                    last_heartbeat = time.time()

            except Exception as exc:
                restart_failures += 1
                runtime.error(f"monitor loop failure: {type(exc).__name__}: {exc}")
                log.exception("Monitor loop failed; automatic recovery attempt %s", restart_failures)
                try:
                    if context:
                        await context.close()
                except Exception:
                    pass
                try:
                    if browser:
                        await browser.close()
                except Exception:
                    pass
                context = None
                browser = None
                await asyncio.sleep(min(120, 5 * (2 ** min(restart_failures, 4))))

            elapsed = time.monotonic() - round_start
            sleep_for = max(0.5, CHECK_INTERVAL_SECONDS - elapsed) + random.uniform(0, 2.0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

        runtime.shutting_down = True
        if context:
            await context.close()
        if browser:
            await browser.close()


def request_stop(*_: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(stop_event.set)
    except RuntimeError:
        pass


async def main() -> None:
    server = start_health_server()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    try:
        await run_monitor()
    finally:
        runtime.shutting_down = True
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
