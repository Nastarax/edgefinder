"""
Optional 'unblocker' proxy for the Cloudflare-protected sources (Investing.com
and Myfxbook).

Why this exists: GitHub Actions runs on datacenter IPs that Cloudflare blocks,
so the curl_cffi TLS-impersonation path in the Investing/Myfxbook fetchers gets
403'd there. That is why those caches could only ever be refreshed from the
laptop (residential IP). When a scraping-API key is present in the environment,
the fetchers route their page requests through that service instead: it fetches
from residential IPs and solves the Cloudflare challenge, so GitHub Actions can
refresh the same data on its own, no always-on home machine needed.

Locally (no key set) nothing changes: `enabled()` is False and every fetcher
keeps using free curl_cffi exactly as before.

Config (all via env vars / GitHub Actions secrets):
  SCRAPER_API_KEY   provider API key. Unset -> disabled (curl_cffi as before).
                    This is the only one you must set to turn the feature on.
  SCRAPER_PROVIDER  'scraperapi' (default) | 'scrapingbee'.
  SCRAPER_PREMIUM   '1' to use the provider's premium/residential pool. Turn
                    this on if plain proxying still gets Cloudflare-blocked
                    (Investing is aggressive). Costs more credits per request.
  SCRAPER_RENDER    '1' to execute the page's JavaScript. Default off: the data
                    we parse (the "Latest Release" block and the __NEXT_DATA__
                    blob) is already in the initial HTML, so rendering just
                    burns credits.
  SCRAPER_COUNTRY   optional 2-letter geo for the exit IP (e.g. 'us').

Free tiers (as of 2026): ScraperAPI ~1000 credits/mo, ScrapingBee 1000-credit
trial. Because the Actions refresh is calendar-gated (--due only fetches cells
whose release window has passed), request volume is a handful per day, so this
generally stays inside a free tier. Premium requests cost ~10x more credits
each, so keep SCRAPER_PREMIUM OFF unless you are seeing block-throughs; leaving
it on is the fastest way to drain the free tier (learned 2026-07-24).

Credit discipline (2026-07-24, after a free tier was exhausted):
  * fetch() does NOT retry a returned non-200 (a Cloudflare block page is billed
    as a 200, and an immediate re-request lands on the same wall). Only true
    transport errors are retried, and only up to SCRAPER_MAX_ATTEMPTS (default 1).
  * scripts/refresh_investing.py skips its pass-2 retry whenever enabled() is
    True, so a still-blocked cell is not re-fetched (and re-billed) in one run;
    the laptop's free curl_cffi path recovers it later.
"""
from __future__ import annotations

import os
import time

import requests

_ENDPOINTS = {
    "scraperapi": "https://api.scraperapi.com/",
    "scrapingbee": "https://app.scrapingbee.com/api/v1/",
}


def _key() -> str | None:
    k = os.environ.get("SCRAPER_API_KEY", "").strip()
    return k or None


def enabled() -> bool:
    """True when an unblocker key is configured. When False, callers fall back
    to their normal curl_cffi path (the local/laptop behaviour)."""
    return _key() is not None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _provider() -> str:
    p = os.environ.get("SCRAPER_PROVIDER", "scraperapi").strip().lower()
    return p if p in _ENDPOINTS else "scraperapi"


def _params(target: str) -> dict:
    key = _key()
    render = _truthy("SCRAPER_RENDER")
    premium = _truthy("SCRAPER_PREMIUM")
    country = os.environ.get("SCRAPER_COUNTRY", "").strip().lower()

    if _provider() == "scrapingbee":
        p = {"api_key": key, "url": target,
             "render_js": "true" if render else "false"}
        if premium:
            p["premium_proxy"] = "true"
        if country:
            p["country_code"] = country
        return p

    # ScraperAPI (default).
    p = {"api_key": key, "url": target}
    if render:
        p["render"] = "true"
    if premium:
        p["premium"] = "true"
    if country:
        p["country_code"] = country
    return p


def _max_attempts() -> int:
    """How many times to hit the scraping API for one URL. Default 1.

    Retrying an HTTP response the provider already returned does not help with
    Cloudflare (an immediate re-request lands on the same wall) and can cost
    another credit, since providers bill a returned block page as a 200. So we
    do NOT retry on a non-200 here; the only reason to raise this is to ride out
    transient network/timeout errors (those return no response and are not
    billed). Override with SCRAPER_MAX_ATTEMPTS if ever needed."""
    try:
        return max(1, int(os.environ.get("SCRAPER_MAX_ATTEMPTS", "1")))
    except ValueError:
        return 1


def fetch(url: str, max_attempts: int | None = None, timeout: int = 70):
    """Fetch `url` through the configured scraping API.

    Returns (status_code, html) on success, or (last_status, None) on failure,
    matching the contract of each fetcher's own _fetch_with_retries so callers
    can `return unblock.fetch(url)` directly.

    Only genuine transport errors (no response, hence not billed) are retried;
    a returned non-200 is surfaced immediately so we never pay twice for the
    same block. See `_max_attempts`.
    """
    provider = _provider()
    endpoint = _ENDPOINTS[provider]
    params = _params(url)
    attempts = max_attempts if max_attempts is not None else _max_attempts()
    last_status = 0
    for attempt in range(attempts):
        try:
            r = requests.get(endpoint, params=params, timeout=timeout)
            last_status = r.status_code
            if r.status_code == 200 and r.text:
                return 200, r.text
            # Non-200: a block or provider error. Retrying an already-returned
            # response burns credits without clearing Cloudflare, so stop here.
            print(f"[unblock] {provider} status {r.status_code} for {url}")
            return last_status, None
        except Exception as e:
            print(f"[unblock] {provider} attempt {attempt + 1}/{attempts} error: {e}")
            if attempt + 1 < attempts:
                time.sleep(2 ** (attempt + 1))
    print(f"[unblock] {provider} gave up on {url} (last status {last_status})")
    return last_status, None
