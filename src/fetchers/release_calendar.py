"""
Release calendar (Phase 1: build + visibility only, no scheduling change).

Derives, per (currency, indicator), when the next economic release is expected,
so Vector can later (Phase 2) fetch only what is actually due instead of blindly
re-sweeping every source every run. This module does NOT change scoring or any
fetch behaviour. It reads the data already on disk:

  - last release date: from each cell's LIVE source cache (the same source the
    scorer uses), via SOURCE_MAP.
  - cadence (period between releases): the median gap of known release dates
    (te_history multi-date rows, CPI reference-month archives); falls back to a
    per-indicator default (quarterly for GDP and the quarterly AU/NZ prints,
    monthly otherwise).
  - next release = last release + cadence, EXCEPT interest-rate decisions, where
    te_rates_outlook already carries the exact next meeting date.

Output: data/cache/release_calendar.json. Run `python -m src.fetchers.release_calendar`
to (re)build and print it, or scripts/show_calendar.py to just print the saved one.

Phase 2 will read this file and refresh a cell only when status == "due".
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CALENDAR_FILE = CACHE_DIR / "release_calendar.json"
DATE_FMT = "%Y-%m-%d"

CCYS = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]
PER_CCY_INDS = ["gdp", "mpmi", "spmi", "retail_sales", "consumer_conf",
                "cpi", "ppi", "unemployment_rate", "rates"]
US_ONLY_INDS = ["pce", "nfp", "adp", "jolts", "jobless_claims"]

# Default cadence in days when we cannot derive one from history.
MONTHLY, QUARTERLY = 30, 91
# Cells that release quarterly (everything else defaults monthly).
QUARTERLY_CELLS = {("gdp", c) for c in CCYS} | {
    ("cpi", "AUD"), ("cpi", "NZD"),   # AU/NZ CPI are quarterly
    ("ppi", "AUD"), ("ppi", "NZD"),   # AU/NZ PPI are quarterly
}


def _load(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _labels() -> dict[str, str]:
    try:
        import yaml
        cfg = yaml.safe_load(open(CONFIG_DIR / "indicators.yaml"))
        out = {}
        for cat in cfg.get("categories", {}).values():
            for ind in cat:
                out[ind["id"]] = ind.get("label", ind["id"])
        return out
    except Exception:
        return {}


def resolve_source(ind: str, ccy: str) -> str:
    """Which live source feeds this cell (mirrors build_currency_scores)."""
    if ind == "cpi":
        return "investing"          # JPY = Tokyo Core, also investing
    if ind == "mpmi":
        return "investing"
    if ind == "spmi":
        if ccy == "CHF":
            return "te"
        if ccy == "NZD":
            return "businessnz"
        return "investing"
    if ind == "ppi":
        if ccy in ("CHF", "AUD"):
            return "myfxbook"
        if ccy in ("NZD", "GBP", "JPY"):
            return "investing"
        return "te"
    if ind == "retail_sales":
        if ccy == "CAD":
            return "investing"
        if ccy == "AUD":
            return "abs"
        return "te"
    if ind == "consumer_conf":
        return "investing" if ccy == "USD" else "te"
    if ind in ("pce", "adp", "jolts"):
        return "investing"          # US-only investing cells
    if ind == "household_spending":
        return "investing"          # JPY Household Spending = Investing id 361
    if ind == "gdp":
        return "investing" if ccy == "JPY" else "te"   # JPY GDP = Investing id 119
    if ind in ("unemployment_rate", "rates", "nfp", "jobless_claims"):
        return "te"
    return "te"


# Lazily-loaded source caches (read once per build).
class _Sources:
    def __init__(self):
        self.te = _load(CACHE_DIR / "te_history.json")
        self.rates = _load(CACHE_DIR / "te_rates_outlook.json")
        self.mpmi = _load(CACHE_DIR / "investing_pmi.json")
        self.spmi = _load(CACHE_DIR / "spmi.json")
        self.cpi = _load(CACHE_DIR / "investing_cpi.json")
        self.ppi_inv = _load(CACHE_DIR / "investing_ppi.json")
        self.ppi_mfx = _load(CACHE_DIR / "myfxbook_ppi.json")
        self.gdp_inv = _load(CACHE_DIR / "investing_gdp.json")
        self.household = _load(CACHE_DIR / "investing_household.json")
        self.cc = _load(CACHE_DIR / "investing_consumer_conf.json")
        self.pce = _load(CACHE_DIR / "investing_pce.json")
        self.adp = _load(CACHE_DIR / "investing_adp.json")
        self.jolts = _load(CACHE_DIR / "investing_jolts.json")
        self.retail_cad = _load(CACHE_DIR / "investing_retail_sales.json")
        self.cpi_hist = _load(CACHE_DIR / "cpi_history_archive.json")


def _te_dates(src, ind, ccy):
    rels = src.te.get(f"{ccy}|{ind}", [])
    return sorted({r["date"] for r in rels if r.get("date")})


def _latest_date(src: _Sources, ind: str, ccy: str, source: str):
    """Last-release date from the cell's live source cache."""
    def d(cache, key=ccy):
        rec = cache.get(key) if isinstance(cache, dict) else None
        return rec.get("date") if isinstance(rec, dict) else None

    # Indicator-specific caches first: spmi.json holds ALL currencies'
    # services PMI regardless of upstream source (Investing / TE-Swiss /
    # BusinessNZ), so it must win over the generic te-history branch.
    if ind == "spmi":
        return d(src.spmi)
    if ind == "mpmi":
        return d(src.mpmi)
    if ind == "household_spending":               # JPY Household Spending id 361
        return d(src.household)
    if ind == "gdp" and source == "investing":   # JPY GDP via Investing id 119
        return d(src.gdp_inv)
    if source == "te":
        ds = _te_dates(src, ind, ccy)
        return ds[-1] if ds else None
    if ind == "cpi":
        return d(src.cpi)
    if ind == "ppi" and source == "investing":
        return d(src.ppi_inv)
    if ind == "ppi" and source == "myfxbook":
        return d(src.ppi_mfx)
    if ind == "consumer_conf":
        return d(src.cc)
    if ind == "pce":
        return d(src.pce)
    if ind == "adp":
        return d(src.adp)
    if ind == "jolts":
        return d(src.jolts)
    if ind == "retail_sales" and source == "investing":
        return d(src.retail_cad)
    return None


def _period_days(src: _Sources, ind: str, ccy: str) -> tuple[int, str]:
    """Median gap between known releases; else a per-indicator default."""
    dates = _te_dates(src, ind, ccy)
    if ind == "cpi":
        # CPI archive is keyed by reference month -> good cadence signal.
        hist = src.cpi_hist.get(ccy, {})
        if isinstance(hist, dict):
            dates = sorted(set(dates) | set(hist.keys()))
    if len(dates) >= 2:
        ds = [datetime.strptime(x, DATE_FMT).date() for x in dates]
        gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if (b - a).days > 0]
        if gaps:
            return int(round(statistics.median(gaps))), "derived"
    default = QUARTERLY if (ind, ccy) in QUARTERLY_CELLS else MONTHLY
    return default, "default"


def build_calendar(today: date | None = None, prior: dict | None = None) -> dict:
    today = today or date.today()
    src = _Sources()
    labels = _labels()
    labels.setdefault("household_spending", "Household Spending")  # not in indicators.yaml
    entries: dict[str, dict] = {}
    # Carry forward the last_checked timestamps so the due-gating backoff
    # survives a rebuild (a cell whose release is late stays marked-checked
    # and is not re-hammered every run).
    prior_checked = {}
    if prior:
        for k, e in prior.get("entries", {}).items():
            if e.get("last_checked"):
                prior_checked[k] = e["last_checked"]

    cells = [(i, c) for i in PER_CCY_INDS for c in CCYS] + \
            [(i, "USD") for i in US_ONLY_INDS] + \
            [("household_spending", "JPY")]   # JPY-only extra (Economic Heatmap)

    for ind, ccy in cells:
        source = resolve_source(ind, ccy)
        period, basis = _period_days(src, ind, ccy)

        if ind == "rates":
            # Exact next meeting date from the rate-outlook cache.
            nxt = src.rates.get(ccy, {}).get("date")
            last = None
        else:
            last = _latest_date(src, ind, ccy, source)
            nxt = None
            if last:
                try:
                    nxt = (datetime.strptime(last, DATE_FMT).date()
                           + timedelta(days=period)).strftime(DATE_FMT)
                except ValueError:
                    nxt = None

        days_until = None
        status = "unknown"
        if nxt:
            try:
                days_until = (datetime.strptime(nxt, DATE_FMT).date() - today).days
                status = "due" if days_until <= 0 else "upcoming"
            except ValueError:
                pass

        key = f"{ccy}|{ind}"
        entries[key] = {
            "currency": ccy,
            "indicator": ind,
            "label": labels.get(ind, ind),
            "source": source,
            "last_release": last,
            "period_days": period,
            "period_basis": basis,
            "next_release": nxt,
            "next_basis": "exact" if ind == "rates" else "estimated",
            "days_until": days_until,
            "status": status,
            "last_checked": prior_checked.get(key),
        }

    return {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entries": entries}


def save_calendar(cal: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CALENDAR_FILE, "w") as f:
        json.dump(cal, f, indent=2)


def load_calendar() -> dict:
    return _load(CALENDAR_FILE)


def mark_checked(cal: dict, keys, ts: str | None = None):
    """Stamp last_checked=now on the given cell keys.

    Callers must pass only cells whose source page was fetched and parsed
    successfully. A stamp means "we looked and there was no new print yet", so
    the cell can safely go quiet for the cooldown. Stamping a cell whose fetch
    FAILED silences it on data we never saw (see _DUE_COOLDOWN_HOURS in
    scripts/refresh_investing.py)."""
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for k in keys:
        if k in cal.get("entries", {}):
            cal["entries"][k]["last_checked"] = ts
    return cal


def checked_within(entry: dict, hours: float, now: datetime | None = None) -> bool:
    """True if this cell was checked less than `hours` ago (still cooling down)."""
    lc = entry.get("last_checked")
    if not lc:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        t = datetime.strptime(lc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (now - t).total_seconds() < hours * 3600


def print_calendar(cal: dict):
    ents = cal.get("entries", {})
    rows = sorted(ents.values(), key=lambda e: (e.get("next_release") or "9999"))
    print(f"Release calendar  (generated {cal.get('generated','?')})\n")
    print(f"{'next':<12} {'d':>5}  {'cell':<22} {'src':<10} "
          f"{'last':<12} {'period':>8}  status")
    for e in rows:
        cell = f"{e['currency']} {e['label']}"
        per = f"{e['period_days']}d{'~' if e['period_basis']=='default' else ''}"
        nxt = e.get("next_release") or "-"
        du = e.get("days_until")
        du = "-" if du is None else str(du)
        star = "*" if e.get("next_basis") == "exact" else ""
        print(f"{nxt+star:<12} {du:>5}  {cell:<22} {e['source']:<10} "
              f"{str(e.get('last_release') or '-'):<12} {per:>8}  {e['status']}")
    due = [e for e in ents.values() if e.get("status") == "due"]
    print(f"\n{len(ents)} cells, {len(due)} due now. (* = exact date; "
          f"~ = estimated cadence)")


if __name__ == "__main__":
    cal = build_calendar()
    save_calendar(cal)
    print_calendar(cal)
