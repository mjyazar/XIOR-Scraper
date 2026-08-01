#!/usr/bin/env python3
"""
XIOR Zernike Tower availability watcher.

Polls the Yardi-backed availability endpoint that the XIOR website itself calls,
and pushes an instant notification the moment a room appears.

Standard library only - no pip install, so a CI run reaches the first check in
about a second instead of thirty.

Usage:
    python3 xior_watch.py                 # one pass over every target
    python3 xior_watch.py --loop 240 --interval 60
    python3 xior_watch.py --test-notify   # verify notification wiring
    python3 xior_watch.py --status        # print state, no network calls
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("XIOR_CONFIG") or os.path.join(ROOT, "config.json")
STATE_PATH = os.environ.get("XIOR_STATE") or os.path.join(ROOT, "state", "state.json")

AJAX_URL = "https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php"
ORIGIN = "https://www.xiorstudenthousing.eu"

# Rotated per request. A realistic User-Agent is required, not cosmetic.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]

# Outcome classes. The distinction between EMPTY and BROKEN/ERROR is the whole
# ballgame: a broken query also returns zero units, and silently treating that
# as "no rooms" is how you watch an empty endpoint for six months and miss the
# room. Only EMPTY is trustworthy evidence of genuine unavailability.
AVAILABLE = "AVAILABLE"
EMPTY = "EMPTY"
BROKEN = "BROKEN"
ERROR = "ERROR"
RATELIMITED = "RATELIMITED"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log(msg: str) -> None:
    print(f"[{iso(now())}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def _decode(resp) -> str:
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def _headers(document):
    if document:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
    # Mirrors the headers the site's own front-end sends for this call.
    # The full header set is required; see NOTES-local.md.
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


_HAVE_CURL = None


def _curl_available():
    global _HAVE_CURL
    if _HAVE_CURL is None:
        try:
            import subprocess
            subprocess.run(["curl", "--version"], capture_output=True, timeout=10)
            _HAVE_CURL = True
        except Exception:
            _HAVE_CURL = False
    return _HAVE_CURL


def _via_curl(url, data, document, timeout):
    """curl with HTTP/2 - measurably the most reliable transport here."""
    import subprocess
    cmd = ["curl", "-s", "--http2", "--compressed", "-m", str(timeout),
           "-w", "\n%{http_code}", "-A", random.choice(USER_AGENTS)]
    for k, v in _headers(document).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-X", "POST", "--data", urllib.parse.urlencode(data)]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    except Exception as e:
        return 0, str(e)
    out = p.stdout.rsplit("\n", 1)
    if len(out) != 2:
        return 0, "curl produced no status"
    try:
        return int(out[1].strip()), out[0]
    except ValueError:
        return 0, out[0][:200]


def _via_urllib(url, data, document, timeout):
    req = urllib.request.Request(url, method="POST" if data is not None else "GET")
    req.add_header("User-Agent", random.choice(USER_AGENTS))
    for k, v in _headers(document).items():
        req.add_header(k, v)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return r.status, _decode(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _decode(e)
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, str(e)


def is_rate_limited(status, body):
    return status == 429 or "Too many requests" in (body or "")[:400]


def http(url, data=None, document=False, timeout=30, tries=3):
    """Return (status, body). status is an int, or 0 for a transport failure.

    Prefers curl/HTTP/2, falls back to urllib. Retries 403/5xx with backoff.
    Does NOT retry 429 - the limiter is per-IP and hammering it only extends
    the ban; the caller backs off instead.
    """
    transports = [_via_curl, _via_urllib] if _curl_available() else [_via_urllib]
    last = (0, "")
    for attempt in range(tries):
        fn = transports[min(attempt, len(transports) - 1)]
        status, body = fn(url, data, document, timeout)
        if status == 200:
            return status, body
        last = (status, body)
        if is_rate_limited(status, body):
            return last
        if status not in (403, 500, 502, 503, 504, 0):
            return last
        if attempt < tries - 1:
            time.sleep((1.8 ** attempt) + random.uniform(0.5, 2.0))
    return last


# ----------------------------------------------------------------------------
# Site config discovery
# ----------------------------------------------------------------------------

def scrape_page_config(url):
    """Pull propertyPageId, semester id and room type ids out of the page HTML.

    Re-read periodically rather than hardcoded: when XIOR opens a new booking
    term the semester id on the page changes, and a stale id would silently
    query the wrong term forever.
    """
    status, html = http(url, document=True)
    if status != 200 or len(html) < 20000:
        return None, f"page fetch failed (status={status}, bytes={len(html)})"

    m = re.search(r"propertyPageId\s*=\s*(\d+)", html)
    page_id = m.group(1) if m else None
    m = re.search(r'id="yardi-semester"[^>]*value="(\d*)"', html)
    if not m:
        m = re.search(r'name="semester"\s+value="(\d*)"', html)
    semester = m.group(1) if m else None
    rooms = sorted(set(re.findall(r'data-room-id="(\d+)"', html)))

    if not page_id:
        return None, "no Yardi booking modal on page (layout changed?)"
    return {"page_id": page_id, "semester_id": semester or "", "room_type_ids": rooms}, None


def check_availability(page_id, semester_id, room_type_id=0):
    """Query the availability endpoint. Returns (outcome, units, detail)."""
    status, body = http(AJAX_URL, data={
        "action": "yardi_room_availability",
        "cf-turnstile-response": "",
        "property_page_id": page_id,
        "room_type_id": room_type_id,
        "semester_id": semester_id,
    })
    if is_rate_limited(status, body):
        return RATELIMITED, [], "429 too many requests"
    if status != 200:
        return ERROR, [], f"http {status}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ERROR, [], f"non-JSON response ({body[:80]!r})"

    if not payload.get("success"):
        msg = (payload.get("data") or {}).get("message") or "unknown failure"
        if "too many" in msg.lower():
            return RATELIMITED, [], msg
        return BROKEN, [], msg

    data = payload.get("data") or {}
    units = data.get("units") or []
    if units:
        return AVAILABLE, units, f"{len(units)} unit(s)"

    # Zero units: decide whether that is real. The upstream Yardi error is
    # echoed back in availability_response.
    ar = data.get("availability_response")
    if isinstance(ar, str):
        try:
            ar = json.loads(ar)
        except Exception:
            ar = {"errorMessage": ar}
    code = (ar or {}).get("errorCode")
    msg = (ar or {}).get("errorMessage") or ""

    # 204 = upstream No Content = genuinely nothing available.
    if code == 204:
        return EMPTY, [], "no units (204)"
    # 400 "The AcademicTermID field is required." and friends = malformed query.
    if code and code != 204:
        return BROKEN, [], f"upstream {code}: {msg}"
    if not ar:
        return EMPTY, [], "no units"
    return BROKEN, [], f"unexpected upstream: {msg or ar}"


# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------

def _post(url, payload, headers=None, form=False):
    data = urllib.parse.urlencode(payload).encode() if form else (
        payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    )
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type",
                   "application/x-www-form-urlencoded" if form else "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status < 300, f"{r.status}"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return False, f"{e.code} {detail}"
    except Exception as e:
        return False, str(e)


def notify(title, body, url=None, priority="high"):
    """Fan out to every configured channel. Returns list of (channel, ok, info).

    Deliberately best-effort across all channels rather than first-success:
    redundancy is the point. One missed push can cost the room.
    """
    results = []

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        # Sent as plain text with no parse_mode on purpose. Apartment names and
        # Yardi URLs routinely contain _ * [ ] ( ) . - which Telegram's Markdown
        # parser rejects with a 400, and a formatting nicety is not worth a
        # silently undelivered "room available" alert. Telegram auto-links bare
        # URLs anyway.
        text = f"{title}\n\n{body}"
        if url and url not in body:
            text += f"\n\n{url}"
        ok, info = _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat, "text": text[:4000]},
            form=True,
        )
        results.append(("telegram", ok, info))

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": title.encode("ascii", "ignore").decode() or "XIOR alert",
            "Priority": {"high": "urgent", "normal": "default"}.get(priority, "default"),
            "Tags": "house,rotating_light" if priority == "high" else "information_source",
        }
        if url:
            headers["Click"] = url
        ok, info = _post(f"{server}/{topic}",
                         body.encode("utf-8"), headers=headers)
        results.append(("ntfy", ok, info))

    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if hook:
        content = f"**{title}**\n{body}"
        if url:
            content += f"\n{url}"
        ok, info = _post(hook, {"content": content[:1900]})
        results.append(("discord", ok, info))

    generic = os.environ.get("GENERIC_WEBHOOK", "").strip()
    if generic:
        ok, info = _post(generic, {"title": title, "body": body, "url": url})
        results.append(("webhook", ok, info))

    if not results:
        log("!! no notification channel configured - alert not delivered")
    for ch, ok, info in results:
        log(f"   notify {ch}: {'ok' if ok else 'FAILED ' + info}")
    return results


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"targets": {}, "last_heartbeat": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------------

def describe_unit(u):
    name = u.get("apartmentName") or u.get("floorplanName") or "room"
    plan = u.get("floorplanName") or ""
    sqm = u.get("sqm") or u.get("sqM") or ""
    rent = u.get("minimumRent")
    rmax = u.get("maximumRent")
    avail = u.get("availableDate") or "now"
    bits = [f"#{name}"]
    if plan and plan != name:
        bits.append(plan)
    if sqm:
        bits.append(f"{sqm}m2")
    if rent:
        price = f"EUR {rent}"
        if rmax and rmax != rent:
            price += f"-{rmax}"
        bits.append(price + "/mo")
    bits.append(f"from {avail}")
    if u.get("unitStatus"):
        bits.append(f"[{u['unitStatus']}]")
    return " | ".join(str(b) for b in bits)


def unit_key(u):
    return str(u.get("apartmentId") or u.get("apartmentName") or json.dumps(u, sort_keys=True))


# ----------------------------------------------------------------------------
# Core pass
# ----------------------------------------------------------------------------

def run_target(target, state, cfg, force_page_refresh=False):
    """Check one property. Mutates state. Returns True if an alert was sent."""
    key = target["key"]
    ts = state["targets"].setdefault(key, {})
    alerted = False

    # Checked before anything else, including the page fetch: when we are being
    # rate limited the correct response is to make no requests to this host at
    # all, not merely to skip the availability call.
    cooling_until = parse_iso(ts.get("rate_limited_until"))
    if cooling_until and now() < cooling_until:
        left = int((cooling_until - now()).total_seconds())
        log(f"{key}: cooling down after rate limit, {left}s left - skipping")
        return alerted

    # --- refresh scraped page config if stale -------------------------------
    refresh_min = cfg.get("page_refresh_minutes", 20)
    last_scrape = parse_iso(ts.get("last_page_scrape"))
    stale = force_page_refresh or last_scrape is None or \
        (now() - last_scrape) > timedelta(minutes=refresh_min)

    if stale:
        found, err = scrape_page_config(target["url"])
        if err:
            log(f"{key}: page scrape failed - {err} (using cached config)")
            ts["page_scrape_error"] = err
        else:
            ts["page_scrape_error"] = None
            ts["last_page_scrape"] = iso(now())
            prev_sem = ts.get("semester_id")
            prev_rooms = ts.get("room_type_ids")

            # A changed semester id means XIOR opened a new booking term. That
            # often precedes units appearing, so it is worth an alert on its own.
            if prev_sem is not None and found["semester_id"] != prev_sem:
                log(f"{key}: !! semester changed {prev_sem} -> {found['semester_id']}")
                notify(
                    f"XIOR {target['label']}: new booking term opened",
                    f"The academic term id changed from {prev_sem} to "
                    f"{found['semester_id']}.\nThis usually means a new booking "
                    f"window is opening. Check now.",
                    target["url"],
                )
                alerted = True
            if prev_rooms is not None and found["room_type_ids"] != prev_rooms:
                log(f"{key}: room types changed {prev_rooms} -> {found['room_type_ids']}")
                notify(
                    f"XIOR {target['label']}: room types changed",
                    f"Room type ids went from {prev_rooms} to {found['room_type_ids']}. "
                    f"A new room category may have been added.",
                    target["url"],
                )
                alerted = True

            ts["page_id"] = found["page_id"]
            ts["semester_id"] = found["semester_id"]
            ts["room_type_ids"] = found["room_type_ids"]

    page_id = ts.get("page_id") or target.get("page_id")
    semester_id = ts.get("semester_id") or target.get("semester_id") or ""
    if not page_id:
        log(f"{key}: no page_id known yet, skipping this pass")
        return alerted

    # --- availability -------------------------------------------------------
    outcome, units, detail = check_availability(page_id, semester_id)
    log(f"{key}: {outcome} ({detail}) page_id={page_id} term={semester_id}")

    ts["last_outcome"] = outcome
    ts["last_detail"] = detail
    ts["last_check"] = iso(now())

    if outcome == RATELIMITED:
        streak = ts.get("ratelimit_streak", 0) + 1
        ts["ratelimit_streak"] = streak
        base = cfg.get("ratelimit_cooldown_seconds", 600)
        # Exponential, capped: 10m, 20m, 40m, 60m...
        cool = min(base * (2 ** (streak - 1)), cfg.get("ratelimit_cooldown_max", 3600))
        ts["rate_limited_until"] = iso(now() + timedelta(seconds=cool))
        log(f"{key}: rate limited (streak {streak}) - backing off {cool}s")
        blind_after = cfg.get("ratelimit_alert_after", 6)
        if streak == blind_after:
            notify(
                f"XIOR watcher blocked: {target['label']}",
                f"Rate limited {streak} times in a row; the watcher is currently "
                f"blind and cannot see new rooms.\n\n"
                f"If this persists, lower the poll frequency "
                f"(check_interval) or check the site manually.",
                target["url"],
            )
            alerted = True
        return alerted

    ts["ratelimit_streak"] = 0
    ts["rate_limited_until"] = None

    if outcome in (ERROR, BROKEN):
        ts["consecutive_errors"] = ts.get("consecutive_errors", 0) + 1
        ts["last_error"] = f"{outcome}: {detail}"
        # Watchdog. A watcher that has been quietly failing is worse than no
        # watcher at all, because it looks like "no rooms".
        threshold = cfg.get("error_alert_after", 12)
        if ts["consecutive_errors"] == threshold:
            notify(
                f"XIOR watcher problem: {target['label']}",
                f"{ts['consecutive_errors']} consecutive failed checks.\n"
                f"Last error: {outcome} - {detail}\n\n"
                f"The watcher is NOT currently able to see availability. "
                f"Check the site manually and review the workflow logs.",
                target["url"],
                priority="high",
            )
            alerted = True
        return alerted

    ts["consecutive_errors"] = 0
    ts["last_error"] = None
    ts["last_good_check"] = iso(now())

    seen = set(ts.get("seen_unit_ids") or [])
    current = {unit_key(u): u for u in units}

    if outcome == AVAILABLE:
        fresh = [u for k, u in current.items() if k not in seen]
        if fresh:
            lines = [describe_unit(u) for u in fresh]
            apply_urls = [u.get("applyOnlineURL") for u in fresh if u.get("applyOnlineURL")]
            body = (f"{len(fresh)} room(s) just became available at "
                    f"{target['label']}:\n\n" + "\n".join(f"- {l}" for l in lines))
            if apply_urls:
                body += "\n\nDirect booking link:\n" + apply_urls[0]
            body += f"\n\nBook fast. Property page:\n{target['url']}"
            log(f"{key}: *** {len(fresh)} NEW UNIT(S) ***")
            for l in lines:
                log(f"      {l}")
            notify(f"ROOM AVAILABLE - XIOR {target['label']}", body,
                   apply_urls[0] if apply_urls else target["url"])
            alerted = True
        else:
            log(f"{key}: {len(current)} unit(s) still listed, already alerted")

    ts["seen_unit_ids"] = sorted(current.keys())
    ts["last_unit_count"] = len(current)
    if outcome == AVAILABLE:
        ts["last_available"] = iso(now())
        ts["last_units"] = [describe_unit(u) for u in units]
    return alerted


def heartbeat(state, cfg):
    """Periodic 'still alive' so silence is never ambiguous."""
    hours = cfg.get("heartbeat_hours", 24)
    if not hours:
        return
    last = parse_iso(state.get("last_heartbeat"))
    if last and (now() - last) < timedelta(hours=hours):
        return
    lines = []
    for key, ts in sorted(state.get("targets", {}).items()):
        lines.append(
            f"- {key}: {ts.get('last_outcome', '?')} "
            f"(term {ts.get('semester_id', '?')}, "
            f"checked {ts.get('last_check', 'never')})"
        )
    notify("XIOR watcher heartbeat",
           "Still watching. Current status:\n\n" + "\n".join(lines),
           priority="normal")
    state["last_heartbeat"] = iso(now())


def one_pass(cfg, state, force_page_refresh=False):
    alerted = False
    # Targets are spaced well apart on purpose. Measured budget is roughly
    # three requests inside ten seconds before the limiter trips, so firing
    # both properties back to back is enough to get blocked on its own.
    spacing = cfg.get("target_spacing_seconds", 25)
    targets = cfg["targets"]
    for i, target in enumerate(targets):
        try:
            if run_target(target, state, cfg, force_page_refresh):
                alerted = True
        except Exception as e:
            log(f"{target['key']}: unhandled error {type(e).__name__}: {e}")
            ts = state["targets"].setdefault(target["key"], {})
            ts["consecutive_errors"] = ts.get("consecutive_errors", 0) + 1
        if i < len(targets) - 1:
            time.sleep(spacing + random.uniform(-3, 3))
    return alerted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0,
                    help="run for N seconds, re-checking every --interval")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        res = notify("XIOR watcher test",
                     "Notifications are wired up correctly.\n"
                     "You will get a message like this the moment a room appears.",
                     "https://www.xiorstudenthousing.eu/netherlands/groningen/"
                     "zernike-tower-student-accommodation/",
                     priority="normal")
        ok = any(r[1] for r in res)
        print("\nRESULT:", "at least one channel delivered" if ok else "ALL CHANNELS FAILED")
        return 0 if ok else 1

    state = load_state()
    if args.status:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    cfg = load_config()
    deadline = time.time() + args.loop if args.loop else 0

    while True:
        # No forced page refresh: staleness is judged from the committed state,
        # so config is re-scraped on its own schedule rather than on every CI
        # run. Each page is ~1.2MB - refetching it every run would be ~700MB/day
        # of pointless traffic against a host that already rate-limits us.
        one_pass(cfg, state)
        heartbeat(state, cfg)
        save_state(state)
        if not args.loop or time.time() >= deadline - 5:
            break
        sleep_for = args.interval + random.uniform(-4, 4)
        remaining = deadline - time.time()
        time.sleep(max(5, min(sleep_for, remaining)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
