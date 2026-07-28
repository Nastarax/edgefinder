"""
ForexFactory economic calendar scraper.

Pulls Actual / Forecast / Previous values for every macro release globally.
Data we can get this way: PMIs, PPI, Core CPI, Jobless Claims, NFP, ADP,
JOLTS, Retail Sales, Consumer Confidence, GDP, etc.

History accumulates in data/cache/ff_history.json. Each run scrapes the
current week and merges new releases (deduped by country+event+date).

Uses curl_cffi to bypass Cloudflare bot protection (same trick as Myfxbook).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

from bs4 import BeautifulSoup

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
HISTORY_FILE = CACHE_DIR / "ff_history.json"

FF_BASE = "https://www.forexfactory.com/calendar"
RATES_OUTLOOK_FILE = Path(__file__).resolve().parents[2] / "data" / "cache" / "ff_rates_outlook.json"


# Event-name patterns -> indicator id.
# Lowercased, substring-matched. Order matters: more specific first.
INDICATOR_PATTERNS = [
    # Core inflation (check before plain CPI)
    ("pce",              ["core pce", "core cpi", "core hicp", "core inflation rate"]),
    # PMIs
    ("mpmi",             ["manufacturing pmi", "mfg pmi", "manufacturing index", "ism manufacturing", "caixin manufacturing"]),
    ("spmi",             ["services pmi", "non-manufacturing pmi", "services index", "ism non-manufacturing", "ism services", "caixin services"]),
    # Headline inflation. Vector's CPI cell is YoY, so only y/y events map to the
    # cpi id: an m/m print reaching the generic TE+FF fallback (which happens if a
    # currency's Investing CPI cache is ever empty) would be scored as a YoY
    # surprise, mixing a ~0.1 monthly figure into a ~0.3 annual cell. (PPI has the
    # same m/m patterns but its scoring block always continues via TE/Investing/
    # Myfxbook and never reaches the FF fallback, so it is left as-is.)
    ("cpi",              ["cpi y/y", "cpi yoy"]),
    ("ppi",              ["ppi y/y", "ppi m/m", "ppi yoy", "ppi mom", "producer price"]),
    # Jobs
    ("jobless_claims",   ["unemployment claims", "jobless claims", "initial claims", "claimant count"]),
    ("nfp",              ["non-farm employment", "non farm payrolls", "nonfarm payrolls", "employment change"]),
    ("unemployment_rate", ["unemployment rate"]),
    # Growth
    ("gdp",              ["gdp y/y", "gdp q/q", "advance gdp", "preliminary gdp", "final gdp"]),
    ("retail_sales",     ["retail sales", "core retail sales"]),
    ("consumer_conf",    ["consumer confidence", "cb consumer confidence", "uom consumer sentiment", "michigan consumer"]),
    # Rates - all 8 central banks
    # Fed: "Federal Funds Rate"
    # ECB: "Main Refinancing Rate"
    # BoE: "Official Bank Rate" / "Bank Rate"
    # BoJ: "Monetary Policy Statement" / "BOJ Policy Rate"
    # SNB: "SNB Policy Rate"
    # RBA: "Cash Rate"
    # BoC: "Overnight Rate" / "BOC Rate"
    # RBNZ: "Official Cash Rate" / "RBNZ Rate Statement"
    ("rates",            ["federal funds rate", "fomc statement", "official cash rate", "main refinancing",
                          "deposit facility rate", "monetary policy", "boj policy", "snb policy",
                          "official bank rate", "bank rate", "cash rate", "overnight rate",
                          "boc rate", "rbnz rate", "snb"]),
]


# ForexFactory uses 3-letter currency codes that match ours
SUPPORTED_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}


@dataclass
class Release:
    country: str
    event: str
    indicator_id: str
    date: str            # YYYY-MM-DD
    actual: float | None
    forecast: float | None
    previous: float | None
    surprise: float | None  # actual - forecast, normalized
    impact: str           # "high" / "medium" / "low" / "holiday"


def _get_browser_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.forexfactory.com/",
    }


def _get(url: str, timeout: int = 25):
    if HAS_CFFI:
        return cffi_requests.get(url, impersonate="chrome120", timeout=timeout)
    return cffi_requests.get(url, headers=_get_browser_headers(), timeout=timeout)


def _get_with_session(target_url: str = None, timeout: int = 25):
    """
    Try a session-based approach: visit FF homepage first to get the
    Cloudflare challenge cookies, then request the target URL with those cookies.
    Often gets through where direct requests get 403.
    Returns a (status_code, text) tuple, or (None, None) on failure.
    """
    target_url = target_url or FF_BASE
    profiles = ["chrome120", "chrome116", "safari17_2", "firefox120"]
    for profile in profiles:
        try:
            if HAS_CFFI:
                session = cffi_requests.Session()
                home = session.get("https://www.forexfactory.com/", impersonate=profile, timeout=timeout)
                if home.status_code != 200:
                    continue
                time.sleep(1.5)
                r = session.get(target_url, impersonate=profile, timeout=timeout)
                if r.status_code == 200:
                    return r.status_code, r.text
            else:
                session = cffi_requests.Session()
                session.headers.update(_get_browser_headers())
                home = session.get("https://www.forexfactory.com/", timeout=timeout)
                if home.status_code != 200:
                    continue
                time.sleep(1.5)
                r = session.get(target_url, timeout=timeout)
                if r.status_code == 200:
                    return r.status_code, r.text
        except Exception as e:
            print(f"[ff] session attempt with {profile} failed: {e}")
            continue
    return None, None


def _generate_week_params(weeks_back: int) -> list[str]:
    """
    Generate FF ?week= parameter strings going back N weeks.
    FF uses format like 'may7.2026' (lowercase month abbrev, day, year).
    """
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    # FF weeks start on Sunday
    days_to_sunday = (today.weekday() + 1) % 7
    this_sunday = today - timedelta(days=days_to_sunday)

    params = []
    for w in range(weeks_back):
        sunday = this_sunday - timedelta(weeks=w)
        param = sunday.strftime("%b%d.%Y").lower()
        params.append(param)
    return params


def backfill_history(weeks: int = 26) -> dict:
    """
    Backfill FF history by scraping past N weeks.
    Polite delay between requests (2s) to avoid rate limiting.
    """
    print(f"[ff backfill] Starting backfill for past {weeks} weeks...")
    week_params = _generate_week_params(weeks)

    total_new = 0
    for i, wparam in enumerate(week_params, 1):
        url = f"{FF_BASE}?week={wparam}"
        print(f"[ff backfill] [{i:>2}/{weeks}] {wparam}...", end=" ", flush=True)
        try:
            status, html = _get_with_session(url)
            if not html:
                print("FAILED (no html)")
                time.sleep(3)
                continue
            releases = parse_calendar_html(html)
            print(f"{len(releases)} releases")
            if releases:
                update_history(releases)
                total_new += len(releases)
        except Exception as e:
            print(f"FAILED ({e})")
        time.sleep(2)  # rate limit politeness

    print(f"\n[ff backfill] Complete. Added ~{total_new} releases.")
    history = load_history()
    print(f"[ff backfill] Total history: {sum(len(v) for v in history.values())} releases across {len(history)} indicator/country pairs")
    return history


def _classify_event(event_name: str) -> str | None:
    """Map an FF event name to one of our indicator IDs (or None if unknown)."""
    lower = event_name.lower()
    for indicator_id, patterns in INDICATOR_PATTERNS:
        for p in patterns:
            if p in lower:
                return indicator_id
    return None


def _parse_value(text: str) -> float | None:
    """Parse FF cell text like '1.2%', '52.4', '231K', '-0.1%' -> float."""
    if not text or not text.strip():
        return None
    t = text.strip().replace(",", "")
    multiplier = 1.0
    if t.endswith("K"):
        multiplier = 1_000
        t = t[:-1]
    elif t.endswith("M"):
        multiplier = 1_000_000
        t = t[:-1]
    elif t.endswith("B"):
        multiplier = 1_000_000_000
        t = t[:-1]
    if t.endswith("%"):
        t = t[:-1]
    try:
        return float(t) * multiplier
    except ValueError:
        return None


def parse_calendar_html(html: str) -> list[Release]:
    """Parse FF calendar HTML into a list of Release objects."""
    soup = BeautifulSoup(html, "html.parser")
    releases: list[Release] = []

    current_date = None
    rows = soup.select("tr.calendar__row")
    if not rows:
        # FF renders the calendar inside a main table; fall back to broader selector
        rows = soup.select("table.calendar__table tr") or soup.select("tr")

    for row in rows:
        # Date cell carries forward across rows of the same day
        date_cell = row.select_one(".calendar__date, td.date")
        if date_cell:
            txt = date_cell.get_text(strip=True)
            if txt:
                current_date = _parse_date(txt)

        ccy_cell = row.select_one(".calendar__currency, td.currency")
        if not ccy_cell:
            continue
        ccy = ccy_cell.get_text(strip=True).upper()
        if ccy not in SUPPORTED_CCY:
            continue

        event_cell = row.select_one(".calendar__event, td.event")
        if not event_cell:
            continue
        event_name = event_cell.get_text(" ", strip=True)
        indicator_id = _classify_event(event_name)
        if not indicator_id:
            continue

        actual = _parse_value((row.select_one(".calendar__actual, td.actual") or _empty()).get_text(strip=True))
        forecast = _parse_value((row.select_one(".calendar__forecast, td.forecast") or _empty()).get_text(strip=True))
        previous = _parse_value((row.select_one(".calendar__previous, td.previous") or _empty()).get_text(strip=True))

        impact = "low"
        impact_el = row.select_one(".calendar__impact span, .calendar__impact-icon")
        if impact_el and impact_el.get("class"):
            impact_classes = " ".join(impact_el.get("class") or [])
            if "high" in impact_classes or "red" in impact_classes:
                impact = "high"
            elif "medium" in impact_classes or "orange" in impact_classes:
                impact = "medium"

        # Compute surprise
        surprise = None
        if actual is not None and forecast is not None:
            denom = abs(forecast) if abs(forecast) > 1e-9 else max(abs(actual), 1.0)
            surprise = (actual - forecast) / denom

        releases.append(Release(
            country=ccy,
            event=event_name,
            indicator_id=indicator_id,
            date=current_date or "",
            actual=actual,
            forecast=forecast,
            previous=previous,
            surprise=surprise,
            impact=impact,
        ))

    return releases


def _empty():
    """Returns a soup-like object with empty get_text()."""
    class Empty:
        def get_text(self, *a, **k): return ""
    return Empty()


def _parse_date(txt: str) -> str | None:
    """Parse FF date strings like 'May 7' or 'MonMay 7' -> YYYY-MM-DD."""
    txt = re.sub(r"^[A-Za-z]{3}", "", txt).strip()  # strip weekday prefix
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})", txt)
    if not m:
        return None
    month_name, day = m.group(1), int(m.group(2))
    try:
        # Assume current year. If month is in future relative to today, use prior year.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        candidate = datetime.strptime(f"{month_name} {day} {now.year}", "%b %d %Y")
        if candidate > now + timedelta(days=30):
            candidate = candidate.replace(year=now.year - 1)
        return candidate.strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_recent_releases() -> list[Release]:
    """Scrape FF's current week."""
    # Try session-based approach (handles Cloudflare challenge cookies)
    status, html = _get_with_session()
    if status == 200 and html:
        return parse_calendar_html(html)

    # Fallback: simple direct request
    try:
        r = _get(FF_BASE)
        if r.status_code != 200:
            print(f"[ff] HTTP {r.status_code} from ForexFactory (all attempts failed)")
            return []
        return parse_calendar_html(r.text)
    except Exception as e:
        print(f"[ff] scrape failed: {e}")
        return []


def load_history() -> dict[str, list[dict]]:
    """history[(country, indicator_id)] = list of release dicts (newest first)."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(history: dict[str, list[dict]]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)


def update_history(new_releases: list[Release]) -> dict[str, list[dict]]:
    """Merge new releases into history. Dedupe by (country, indicator_id, date, event)."""
    history = load_history()
    for r in new_releases:
        if not r.date:
            continue
        key = f"{r.country}|{r.indicator_id}"
        bucket = history.get(key, [])
        # Dedup
        sig = (r.date, r.event)
        if any((b.get("date"), b.get("event")) == sig for b in bucket):
            continue
        bucket.append(asdict(r))
        bucket.sort(key=lambda x: x.get("date") or "", reverse=True)
        # Keep most recent 36 readings
        history[key] = bucket[:36]
    save_history(history)
    return history


def fetch_ff() -> dict[str, list[dict]]:
    """
    Main entry: scrape latest, merge with history, return organized dict.
    Returns: history[country|indicator_id] = list[release dict] (newest first)
    """
    new = fetch_recent_releases()
    print(f"[ff] scraped {len(new)} releases")
    history = update_history(new)
    print(f"[ff] history now has {sum(len(v) for v in history.values())} releases across {len(history)} indicator/country pairs")
    return history


def _generate_future_week_params(weeks_ahead: int) -> list[str]:
    """Generate FF ?week= parameter strings going FORWARD N weeks."""
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    days_to_sunday = (today.weekday() + 1) % 7
    this_sunday = today - timedelta(days=days_to_sunday)
    params = []
    for w in range(weeks_ahead + 1):  # include current week
        sunday = this_sunday + timedelta(weeks=w)
        param = sunday.strftime("%b%d.%Y").lower()
        params.append(param)
    return params


def fetch_upcoming_rate_decisions(weeks_ahead: int = 8) -> dict:
    """
    Scrape FF calendar for upcoming "Interest Rate Decision" events.

    Returns dict keyed by currency:
      {"USD": {"date": "2026-06-17", "forecast": 3.75, "event": "Federal Funds Rate"}, ...}

    Picks the NEXT upcoming rate decision per currency (earliest future date
    where actual is still None and a forecast is published). Caches to
    ff_rates_outlook.json so we have a fallback if FF blocks the next run.

    "Updates all the time" means this is called on every main.py run.
    """
    cache = _load_rates_outlook()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    upcoming: dict[str, dict] = {}
    fresh = 0

    print(f"[ff-rates] scraping next {weeks_ahead} weeks for upcoming rate decisions...")
    seen_no_forecast: list[tuple[str, str, str]] = []  # (ccy, date, event)
    for wparam in _generate_future_week_params(weeks_ahead):
        url = f"{FF_BASE}?week={wparam}"
        try:
            status, html = _get_with_session(url)
            if not html:
                print(f"[ff-rates] week {wparam}: no html (FF blocked?)")
                continue
            releases = parse_calendar_html(html)
            week_rates_count = sum(1 for r in releases if r.indicator_id == "rates")
            if week_rates_count > 0:
                print(f"[ff-rates] week {wparam}: {week_rates_count} rate event(s) found")
            for r in releases:
                if r.indicator_id != "rates":
                    continue
                if r.actual is not None:
                    continue  # past decision, skip
                if not r.date or r.date < today_str:
                    continue  # not actually future
                if r.forecast is None:
                    seen_no_forecast.append((r.country, r.date, r.event))
                    continue
                ccy = r.country
                # Keep the EARLIEST upcoming decision per currency
                if ccy not in upcoming or r.date < upcoming[ccy]["date"]:
                    upcoming[ccy] = {
                        "date": r.date,
                        "forecast": r.forecast,
                        "event": r.event,
                    }
        except Exception as e:
            print(f"[ff-rates] week {wparam} failed: {e}")
            continue
        time.sleep(1.5)

    if seen_no_forecast:
        print(f"[ff-rates] found {len(seen_no_forecast)} upcoming rate event(s) without consensus forecast yet:")
        for ccy, date, ev in seen_no_forecast[:10]:
            print(f"  {ccy} {date}: {ev}")

    # Merge with cache: prefer fresh data, fall back to cached if missing
    merged = dict(cache)
    for ccy, rel in upcoming.items():
        merged[ccy] = rel
        fresh += 1
    # Drop cached entries whose date has passed (no longer "upcoming")
    merged = {c: r for c, r in merged.items() if r.get("date", "") >= today_str}
    _save_rates_outlook(merged)
    print(f"[ff-rates] {fresh} fresh, {len(merged) - fresh} from cache, {len(merged)}/{len(SUPPORTED_CCY)} currencies covered")
    for ccy in sorted(merged):
        r = merged[ccy]
        print(f"  {ccy}: forecast={r.get('forecast')}  meeting={r.get('date')}  ({r.get('event', '')})")
    return merged


def _load_rates_outlook() -> dict:
    if not RATES_OUTLOOK_FILE.exists():
        return {}
    try:
        with open(RATES_OUTLOOK_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_rates_outlook(data: dict):
    RATES_OUTLOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RATES_OUTLOOK_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_rates_outlook() -> dict:
    """Read-only access to cached rates outlook (used in backtest mode)."""
    return _load_rates_outlook()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ForexFactory calendar fetcher.")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Backfill past N weeks of history (e.g., 26 for 6 months)")
    args = parser.parse_args()

    print(f"curl_cffi installed: {HAS_CFFI}")
    if args.backfill > 0:
        history = backfill_history(weeks=args.backfill)
    else:
        history = fetch_ff()

    summary: dict[str, set[str]] = {}
    for key, releases in history.items():
        ccy, ind = key.split("|")
        summary.setdefault(ind, set()).add(ccy)
    print("\nCoverage summary:")
    for ind, ccys in sorted(summary.items()):
        print(f"  {ind:18s} {sorted(ccys)}")
