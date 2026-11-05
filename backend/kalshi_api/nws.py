"""
NWS Daily Summary Message (DSM) for KMIA, shown on the page. DISPLAY ONLY: nothing here feeds the forecast
models or the trading rule (the models keep using the HFMETAR pipeline in weather/).

  * DSM         the ASOS Daily Summary Message for Miami (product DSMMIA). NWS's own API (api.weather.gov) has no
                Miami DSM, so it is read from the Iowa Mesonet's mirror of the NWS text stream.

It is cached in memory. If a refresh fails, the last good copy is returned and marked "stale".

DSM format (one message, groups separated by "/", whitespace ignored; ASOS daily summary users guide):

    KMIA DS [HHMM] DD/MM  HHH+HHMM/ LLL+HHMM//  coopHigh/ coopLow//  minSLP+HHMM/ precip24h/ p01/../p24/ ...
                                                                    ^ hourly precip, hundredths of an inch

  * HHMM right after DS is the report time of an intermediate (partial-day) message; a final summary has none.
  * High / low are whole degrees F with the time they occurred. All times are LOCAL STANDARD TIME (ASOS clocks
    do not follow daylight saving), and the DSM "day" is midnight to midnight LST.
  * Precipitation is in hundredths of an inch ("T" = trace, "-" = hour not reached yet, "M" = missing).
  * Wind, sky cover and the trailing codes are not decoded; they are always visible in "raw".
"""
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

STATION = "KMIA"
DSM_PIL = "DSMMIA"
IEM_BASE = "https://mesonet.agron.iastate.edu"
USER_AGENT = "kalshi-trading-bot/1.0"
HTTP_TIMEOUT_SEC = 15

# How long a fetched copy is reused. Kept short (the page polls every 60 s) so a new DSM shows up quickly;
# with several viewers the upstream service still sees at most one listing request per TTL.
DSM_TTL_SEC = 30
DSM_LOOKBACK_PRODUCTS = 6      # newest products inspected when looking for the latest and the last final summary
LST = timezone(timedelta(hours=-5), "LST")   # fixed offset: ASOS local standard time, never daylight saving


# ── DSM parsing (pure) ────────────────────────────────────────────────────────

_HEADER = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4})\s+DS(?:\s+COR)?(?:\s+(\d{4}))?\s+(\d{2})/(\d{2})\s+(.*)$", re.S)


def _clean(text: str) -> str:
    """Drop the teletype framing characters; a message wraps after a '/', so such line breaks join the groups."""
    text = text.replace("\x01", "").replace("\x03", "").replace("\r", "")
    text = re.sub(r"/[ \t]*\n[ \t]*", "/", text)
    return text.replace("\n", " ").strip().split("=")[0].strip()


def _temp_and_time(group: str):
    """'851425' -> (85, '1425'); 'M' / '' / '-' -> (None, None)."""
    group = group.strip()
    m = re.fullmatch(r"(-?\d+)(\d{4})", group)
    if m:
        return int(m.group(1)), m.group(2)
    if re.fullmatch(r"-?\d+", group):
        return int(group), None
    return None, None


def _temp(group: str):
    group = group.strip()
    return int(group) if re.fullmatch(r"-?\d+", group) else None


def _hhmm(value):
    return None if not value else f"{value[:2]}:{value[2:]}"


def _precip(group: str):
    """Hundredths of an inch -> (inches or None, trace?)."""
    group = group.strip()
    if group == "T":
        return 0.0, True
    if re.fullmatch(r"\d{1,4}", group):
        return int(group) / 100.0, False
    return None, False          # "-" (hour not reached yet) or "M"


def _min_slp(group: str):
    """'9891622' -> (29.89 inHg, '1622'). The value is hundredths of inHg with the leading '2'/'3' dropped."""
    m = re.fullmatch(r"(\d{3})(\d{4})?", group.strip())
    if not m:
        return None, None
    value = int(m.group(1))
    return round((20 + value / 100.0) if value >= 500 else (30 + value / 100.0), 2), m.group(2)


def parse_dsm(text: str, issued_at: datetime = None):
    """
    One DSM message -> dict, or None if it does not look like a DSM. `issued_at` (UTC) is when the message was
    issued; it is used only to pick the year. Never raises on odd input.
    """
    if not text:
        return None
    match = _HEADER.search(_clean(text))
    if not match:
        return None
    station, report_time, day, month, body = match.groups()
    f = [part.strip() for part in body.split("/")]
    if len(f) < 8:
        return None

    high, high_time = _temp_and_time(f[0])
    low, low_time = _temp_and_time(f[1])
    slp, slp_time = _min_slp(f[6]) if len(f) > 6 else (None, None)
    total, total_trace = _precip(f[7])
    hourly = []
    for hour_ending, group in enumerate(f[8:32], start=1):
        inches, trace = _precip(group)
        hourly.append({"hour_ending": hour_ending, "inches": inches, "trace": trace})

    # The day/month of the summary, with the year taken from when the message was issued.
    issued = (issued_at or datetime.now(timezone.utc)).astimezone(LST)
    try:
        summary_date = date(issued.year, int(month), int(day))
        if summary_date > issued.date() + timedelta(days=1):    # e.g. a 12/31 summary issued on 1 Jan
            summary_date = date(issued.year - 1, int(month), int(day))
    except ValueError:
        return None

    return {
        "station": station,
        "kind": "partial" if report_time else "final",
        "report_time_lst": _hhmm(report_time),
        "date": summary_date.isoformat(),
        "high_f": high, "high_time_lst": _hhmm(high_time),
        "low_f": low, "low_time_lst": _hhmm(low_time),
        "coop_high_f": _temp(f[3]), "coop_low_f": _temp(f[4]),
        "min_slp_inhg": slp, "min_slp_time_lst": _hhmm(slp_time),
        "precip_in": total, "precip_trace": total_trace,
        "hourly_precip": hourly,
    }


# ── caching ───────────────────────────────────────────────────────────────────

_cache = {}                 # key -> (monotonic time, value)
_locks = {}
_locks_guard = threading.Lock()
_text_cache = {}            # product_id -> text (a published product never changes)


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _cached(key, ttl, loader):
    """Fresh cached value, else loader(). On a loader failure return the old value flagged stale, else re-raise."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    with _lock_for(key):
        hit = _cache.get(key)                       # another thread may have refreshed while we waited
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            value = loader()
        except Exception as exc:
            if hit:
                return {**hit[1], "stale": True, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            raise
        _cache[key] = (time.monotonic(), value)
        return value


def _get(url, **kwargs):
    # no-cache: ask any intermediate cache for a fresh copy (both services advertise max-age hints)
    headers = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache", "Pragma": "no-cache", **kwargs.pop("headers", {})}
    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SEC, **kwargs)
    response.raise_for_status()
    return response


# ── DSM ───────────────────────────────────────────────────────────────────────

def _dsm_products_for(day):
    """[(issued_at UTC, product_id)] newest first for one UTC date."""
    payload = _get(f"{IEM_BASE}/api/1/nws/afos/list.json", params={"pil": DSM_PIL, "date": day.isoformat()}).json()
    found = []
    for row in payload.get("data", []):
        try:
            issued = datetime.strptime(row["entered"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            found.append((issued, row["product_id"]))
        except (KeyError, ValueError):
            continue
    return sorted(found, reverse=True)


def _product_text(product_id):
    if product_id not in _text_cache:
        if len(_text_cache) > 50:
            _text_cache.clear()
        _text_cache[product_id] = _get(f"{IEM_BASE}/api/1/nwstext/{product_id}").text
    return _text_cache[product_id]


def _load_dsm(now=None):
    now = now or datetime.now(timezone.utc)
    latest, last_final = None, None
    inspected = 0
    # Today's listing first; earlier days are only requested while the last final summary is still missing.
    for offset in range(3):
        for issued, product_id in _dsm_products_for((now - timedelta(days=offset)).date()):
            if inspected >= DSM_LOOKBACK_PRODUCTS or (latest and last_final):
                break
            inspected += 1
            text = _product_text(product_id)
            parsed = parse_dsm(text, issued)
            if parsed is None:
                continue
            entry = {**parsed, "issued_at": issued.isoformat(), "product_id": product_id, "raw": _clean(text) + "="}
            if latest is None:
                latest = entry
            if entry["kind"] == "final" and last_final is None:
                last_final = entry
        if inspected >= DSM_LOOKBACK_PRODUCTS or (latest and last_final):
            break
    return {
        "station": STATION, "source": "NWS ASOS Daily Summary (DSMMIA) via Iowa Mesonet",
        "fetched_at": now.isoformat(), "latest": latest, "last_final": last_final, "stale": False,
        "note": "DSM times are local standard time (no daylight saving); the DSM day runs midnight to midnight LST.",
    }


def get_dsm():
    return _cached("dsm", DSM_TTL_SEC, _load_dsm)
