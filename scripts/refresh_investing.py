"""
Local cache refresh for Cloudflare-blocked sources (Investing.com + Myfxbook):
mPMI + sPMI + CPI + NZD PPI + CC + JOLTS + ADP + PCE + CHF PPI.

Why this script exists separately from main.py:
  These indicators are monthly. There's no reason to scrape Investing.com
  24 times a day. Doing so just risks getting our IP Cloudflare-flagged.

Strategy: two passes per indicator. First pass tries all currencies. Second
pass retries ONLY the ones that fell back to cache (Cloudflare-blocked or
parse-failed), with longer sleeps to avoid the rate limiter.

Each fetcher tracks which currencies it actually fetched fresh in a
module-level `_LAST_FRESH` set. The script uses that (NOT the result dict,
which mixes fresh + cached entries) to know what truly needs retry. Every
`refresh_*` returns the union of its passes' fresh keys, which `run_due` uses
to decide which due cells earn the cooldown stamp: only cells we genuinely
fetched. Blocked cells stay unstamped so the next run retries them.

Run locally on Yanaël's Windows machine, then commit + push the JSON
caches. GitHub Actions can't run this because Cloudflare blocks GitHub IPs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetchers import (investing, investing_adp, investing_consumer_conf,
                          investing_core, investing_cpi, investing_gdp,
                          investing_household, investing_jolts, investing_pce,
                          investing_ppi, investing_retail_sales, myfxbook_ppi,
                          services_pmi)
from src.fetchers import release_calendar as rc
from src.fetchers import unblock


def _summarize(label: str, all_keys, fresh_set, results):
    """Print accurate per-pass summary."""
    cached = [c for c in all_keys if c not in fresh_set and c in results]
    missing = [c for c in all_keys if c not in results]
    print(f"\n[{label}] pass complete: {len(fresh_set)} fresh, {len(cached)} fell back to cache, {len(missing)} missing entirely")
    if cached:
        print(f"  cache fallback: {cached}")
    if missing:
        print(f"  missing entirely: {missing}")


def refresh_mpmi() -> set[str]:
    print("\n============================================")
    print("REFRESHING MANUFACTURING PMI (mPMI)")
    print("============================================")
    all_keys = list(investing.MPMI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing.fetch_mpmi(sleep_between=12.0)
    fresh1 = set(investing._LAST_FRESH)
    _summarize("mpmi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nmPMI: all currencies fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing.MPMI_URLS.copy()
    try:
        for k in list(investing.MPMI_URLS.keys()):
            if k not in failed:
                del investing.MPMI_URLS[k]
        second = investing.fetch_mpmi(sleep_between=18.0)
        fresh2 = set(investing._LAST_FRESH)
    finally:
        investing.MPMI_URLS.clear()
        investing.MPMI_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nmPMI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"mPMI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("mPMI: all previously-failed currencies recovered on pass 2.")

    return fresh1 | fresh2


def refresh_spmi() -> set[str]:
    print("\n============================================")
    print("REFRESHING SERVICES PMI (sPMI)")
    print("============================================")
    all_keys = (list(services_pmi.SPMI_INVESTING_URLS.keys())
                + list(services_pmi.SPMI_TE_URLS.keys())
                + list(services_pmi.SPMI_BUSINESSNZ_URLS.keys())
                + list(services_pmi.SPMI_MYFXBOOK_URLS.keys()))
    print(f"Targeting {len(all_keys)} currencies "
          f"({len(services_pmi.SPMI_INVESTING_URLS)} Investing + "
          f"{len(services_pmi.SPMI_TE_URLS)} TE + "
          f"{len(services_pmi.SPMI_BUSINESSNZ_URLS)} BusinessNZ + "
          f"{len(services_pmi.SPMI_MYFXBOOK_URLS)} Myfxbook)\n")

    print("--- Pass 1: full fetch ---")
    first = services_pmi.fetch_spmi(sleep_between=12.0)
    fresh1 = set(services_pmi._LAST_FRESH)
    _summarize("spmi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nsPMI: all currencies fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig_investing = services_pmi.SPMI_INVESTING_URLS.copy()
    orig_te = services_pmi.SPMI_TE_URLS.copy()
    orig_bnz = services_pmi.SPMI_BUSINESSNZ_URLS.copy()
    orig_myfx = services_pmi.SPMI_MYFXBOOK_URLS.copy()
    try:
        for k in list(services_pmi.SPMI_INVESTING_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_INVESTING_URLS[k]
        for k in list(services_pmi.SPMI_TE_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_TE_URLS[k]
        for k in list(services_pmi.SPMI_BUSINESSNZ_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_BUSINESSNZ_URLS[k]
        for k in list(services_pmi.SPMI_MYFXBOOK_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_MYFXBOOK_URLS[k]
        second = services_pmi.fetch_spmi(sleep_between=18.0)
        fresh2 = set(services_pmi._LAST_FRESH)
    finally:
        services_pmi.SPMI_INVESTING_URLS.clear()
        services_pmi.SPMI_INVESTING_URLS.update(orig_investing)
        services_pmi.SPMI_TE_URLS.clear()
        services_pmi.SPMI_TE_URLS.update(orig_te)
        services_pmi.SPMI_BUSINESSNZ_URLS.clear()
        services_pmi.SPMI_BUSINESSNZ_URLS.update(orig_bnz)
        services_pmi.SPMI_MYFXBOOK_URLS.clear()
        services_pmi.SPMI_MYFXBOOK_URLS.update(orig_myfx)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nsPMI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"sPMI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("sPMI: all previously-failed currencies recovered on pass 2.")

    return fresh1 | fresh2


def refresh_cpi() -> set[str]:
    print("\n============================================")
    print("REFRESHING CPI YoY")
    print("============================================")
    all_keys = list(investing_cpi.CPI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_cpi.fetch_cpi(sleep_between=12.0)
    fresh1 = set(investing_cpi._LAST_FRESH)
    _summarize("cpi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCPI: all currencies fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_cpi.CPI_URLS.copy()
    try:
        for k in list(investing_cpi.CPI_URLS.keys()):
            if k not in failed:
                del investing_cpi.CPI_URLS[k]
        second = investing_cpi.fetch_cpi(sleep_between=18.0)
        fresh2 = set(investing_cpi._LAST_FRESH)
    finally:
        investing_cpi.CPI_URLS.clear()
        investing_cpi.CPI_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nCPI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"CPI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("CPI: all previously-failed currencies recovered on pass 2.")

    return fresh1 | fresh2


def refresh_ppi() -> set[str]:
    print("\n============================================")
    print("REFRESHING PPI YoY (NZD + GBP via Investing)")
    print("============================================")
    all_keys = list(investing_ppi.PPI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_ppi.fetch_ppi(sleep_between=12.0)
    fresh1 = set(investing_ppi._LAST_FRESH)
    _summarize("ppi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nPPI (NZD): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_ppi.PPI_URLS.copy()
    try:
        for k in list(investing_ppi.PPI_URLS.keys()):
            if k not in failed:
                del investing_ppi.PPI_URLS[k]
        second = investing_ppi.fetch_ppi(sleep_between=18.0)
        fresh2 = set(investing_ppi._LAST_FRESH)
    finally:
        investing_ppi.PPI_URLS.clear()
        investing_ppi.PPI_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nPPI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"PPI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("PPI: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_gdp() -> set[str]:
    print("\n============================================")
    print("REFRESHING GDP QoQ (JPY only via Investing)")
    print("============================================")
    all_keys = list(investing_gdp.GDP_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_gdp.fetch_gdp(sleep_between=12.0)
    fresh1 = set(investing_gdp._LAST_FRESH)
    _summarize("gdp", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nGDP (JPY): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_gdp.GDP_URLS.copy()
    try:
        for k in list(investing_gdp.GDP_URLS.keys()):
            if k not in failed:
                del investing_gdp.GDP_URLS[k]
        second = investing_gdp.fetch_gdp(sleep_between=18.0)
        fresh2 = set(investing_gdp._LAST_FRESH)
    finally:
        investing_gdp.GDP_URLS.clear()
        investing_gdp.GDP_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nGDP summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"GDP: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("GDP: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_household() -> set[str]:
    print("\n============================================")
    print("REFRESHING HOUSEHOLD SPENDING (JPY only via Investing)")
    print("============================================")
    all_keys = list(investing_household.HOUSEHOLD_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_household.fetch_household(sleep_between=12.0)
    fresh1 = set(investing_household._LAST_FRESH)
    _summarize("household", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nHousehold Spending (JPY): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_household.HOUSEHOLD_URLS.copy()
    try:
        for k in list(investing_household.HOUSEHOLD_URLS.keys()):
            if k not in failed:
                del investing_household.HOUSEHOLD_URLS[k]
        second = investing_household.fetch_household(sleep_between=18.0)
        fresh2 = set(investing_household._LAST_FRESH)
    finally:
        investing_household.HOUSEHOLD_URLS.clear()
        investing_household.HOUSEHOLD_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nHousehold summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Household: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("Household: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_consumer_conf() -> set[str]:
    print("\n============================================")
    print("REFRESHING US CONSUMER CONFIDENCE (CB, USD only via Investing)")
    print("============================================")
    all_keys = list(investing_consumer_conf.CC_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_consumer_conf.fetch_consumer_conf(sleep_between=12.0)
    fresh1 = set(investing_consumer_conf._LAST_FRESH)
    _summarize("cc", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nConsumer Confidence (USD): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_consumer_conf.CC_URLS.copy()
    try:
        for k in list(investing_consumer_conf.CC_URLS.keys()):
            if k not in failed:
                del investing_consumer_conf.CC_URLS[k]
        second = investing_consumer_conf.fetch_consumer_conf(sleep_between=18.0)
        fresh2 = set(investing_consumer_conf._LAST_FRESH)
    finally:
        investing_consumer_conf.CC_URLS.clear()
        investing_consumer_conf.CC_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nConsumer Confidence summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Consumer Confidence: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("Consumer Confidence: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_jolts() -> set[str]:
    print("\n============================================")
    print("REFRESHING US JOLTS JOB OPENINGS (USD only via Investing)")
    print("============================================")
    all_keys = list(investing_jolts.JOLTS_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_jolts.fetch_jolts(sleep_between=12.0)
    fresh1 = set(investing_jolts._LAST_FRESH)
    _summarize("jolts", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nJOLTS (USD): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_jolts.JOLTS_URLS.copy()
    try:
        for k in list(investing_jolts.JOLTS_URLS.keys()):
            if k not in failed:
                del investing_jolts.JOLTS_URLS[k]
        second = investing_jolts.fetch_jolts(sleep_between=18.0)
        fresh2 = set(investing_jolts._LAST_FRESH)
    finally:
        investing_jolts.JOLTS_URLS.clear()
        investing_jolts.JOLTS_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nJOLTS summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"JOLTS: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("JOLTS: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_adp() -> set[str]:
    print("\n============================================")
    print("REFRESHING US ADP EMPLOYMENT CHANGE (USD only via Investing)")
    print("============================================")
    all_keys = list(investing_adp.ADP_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_adp.fetch_adp(sleep_between=12.0)
    fresh1 = set(investing_adp._LAST_FRESH)
    _summarize("adp", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nADP (USD): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_adp.ADP_URLS.copy()
    try:
        for k in list(investing_adp.ADP_URLS.keys()):
            if k not in failed:
                del investing_adp.ADP_URLS[k]
        second = investing_adp.fetch_adp(sleep_between=18.0)
        fresh2 = set(investing_adp._LAST_FRESH)
    finally:
        investing_adp.ADP_URLS.clear()
        investing_adp.ADP_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nADP summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"ADP: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("ADP: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_pce() -> set[str]:
    print("\n============================================")
    print("REFRESHING US CORE PCE PRICE INDEX YoY (USD only via Investing)")
    print("============================================")
    all_keys = list(investing_pce.PCE_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_pce.fetch_pce(sleep_between=12.0)
    fresh1 = set(investing_pce._LAST_FRESH)
    _summarize("pce", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCore PCE (USD): fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_pce.PCE_URLS.copy()
    try:
        for k in list(investing_pce.PCE_URLS.keys()):
            if k not in failed:
                del investing_pce.PCE_URLS[k]
        second = investing_pce.fetch_pce(sleep_between=18.0)
        fresh2 = set(investing_pce._LAST_FRESH)
    finally:
        investing_pce.PCE_URLS.clear()
        investing_pce.PCE_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nCore PCE summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Core PCE: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("Core PCE: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_mfx_ppi() -> set[str]:
    print("\n============================================")
    print("REFRESHING Myfxbook PPI YoY (CHF + AUD)")
    print("============================================")
    all_keys = list(myfxbook_ppi.CHF_PPI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = myfxbook_ppi.fetch_ppi(sleep_between=8.0)
    fresh1 = set(myfxbook_ppi._LAST_FRESH)
    _summarize("mfx-ppi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nMyfxbook PPI: fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    second = myfxbook_ppi.fetch_ppi(sleep_between=15.0)
    fresh2 = set(myfxbook_ppi._LAST_FRESH)
    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nMyfxbook PPI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Myfxbook PPI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values. (Myfxbook needs curl_cffi; "
              "make sure it is installed locally.)")
    else:
        print("Myfxbook PPI: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_cad_retail() -> set[str]:
    print("\n============================================")
    print("REFRESHING CAD Retail Sales MoM (via Investing.com)")
    print("============================================")
    all_keys = list(investing_retail_sales.RETAIL_SALES_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_retail_sales.fetch_retail_sales(sleep_between=8.0)
    fresh1 = set(investing_retail_sales._LAST_FRESH)
    _summarize("retail-inv", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCAD Retail Sales: fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    second = investing_retail_sales.fetch_retail_sales(sleep_between=15.0)
    fresh2 = set(investing_retail_sales._LAST_FRESH)
    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nCAD Retail Sales summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"CAD Retail Sales: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("CAD Retail Sales: recovered on pass 2.")

    return fresh1 | fresh2


def refresh_core() -> set[str]:
    print("\n============================================")
    print("REFRESHING US CORE CPI + CORE PPI")
    print("============================================")
    all_keys = list(investing_core.CORE_URLS.keys())
    print(f"Targeting {len(all_keys)} indicators\n")

    print("--- Pass 1: full fetch ---")
    first = investing_core.fetch_core(sleep_between=12.0)
    fresh1 = set(investing_core._LAST_FRESH)
    _summarize("core", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCore: all indicators fetched fresh.")
        return fresh1

    # Pass 2 re-fetches cells that pass 1 could not get fresh. On the laptop
    # (curl_cffi) that recovers transient Cloudflare blocks for free. Under the
    # scraping API each re-fetch of a still-blocked cell is another billed
    # request (a Cloudflare block page comes back as a billed 200), so skip it:
    # pass 1 already saved whatever succeeded, and the laptop will pick up the
    # rest on a later run.
    if unblock.enabled():
        print(f"  skipping pass-2 retry under the scraper to save credits; "
              f"laptop will recover: {sorted(failed)}")
        return fresh1
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_core.CORE_URLS.copy()
    try:
        for k in list(investing_core.CORE_URLS.keys()):
            if k not in failed:
                del investing_core.CORE_URLS[k]
        second = investing_core.fetch_core(sleep_between=18.0)
        fresh2 = set(investing_core._LAST_FRESH)
    finally:
        investing_core.CORE_URLS.clear()
        investing_core.CORE_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    if still_failed:
        print(f"Core: still not fresh after 2 passes: {still_failed}")
    else:
        print("Core: all previously-failed indicators recovered on pass 2.")

    return fresh1 | fresh2


def refresh_cpi_history():
    """Deep monthly CPI YoY history for all 8 currencies (Investing
    __NEXT_DATA__). Powers the inflation line chart with continuous, current
    data so there's no FRED publication lag and no straight-line tails."""
    print("\n============================================")
    print("REFRESHING CPI HISTORY (all 8, for inflation chart)")
    print("============================================")
    hist = investing_cpi.fetch_all_cpi_history(sleep_between=5.0)
    got = sorted(hist.keys())
    print(f"\nCPI history: fetched {len(got)}/8 -> {got}")
    missing = [c for c in ("USD","EUR","GBP","JPY","CHF","AUD","CAD","NZD") if c not in hist]
    if missing:
        print(f"  missing (kept from cache/archive): {missing}")


# Registry of refreshable targets, in default run order. Lets you refresh a
# subset from the CLI instead of the full (slow) sweep, e.g.:
#   python scripts/refresh_investing.py jolts adp
#   python scripts/refresh_investing.py cc
# With no args, runs everything in this order.
REFRESHERS = {
    "mpmi": refresh_mpmi,
    "spmi": refresh_spmi,
    "cpi": refresh_cpi,
    "cpi_history": refresh_cpi_history,
    "ppi": refresh_ppi,
    "gdp": refresh_gdp,
    "household": refresh_household,
    "cc": refresh_consumer_conf,
    "jolts": refresh_jolts,
    "adp": refresh_adp,
    "pce": refresh_pce,
    "mfx_ppi": refresh_mfx_ppi,
    "cad_retail": refresh_cad_retail,
    "core": refresh_core,
}

# Cache file each target writes (for the commit hint).
_CACHE_FILES = {
    "mpmi": "data/cache/investing_pmi.json",
    "spmi": "data/cache/spmi.json",
    "cpi": "data/cache/investing_cpi.json",
    "cpi_history": "data/cache/cpi_investing_history.json",
    "ppi": "data/cache/investing_ppi.json",
    "gdp": "data/cache/investing_gdp.json",
    "household": "data/cache/investing_household.json",
    "cc": "data/cache/investing_consumer_conf.json",
    "jolts": "data/cache/investing_jolts.json",
    "adp": "data/cache/investing_adp.json",
    "pce": "data/cache/investing_pce.json",
    "mfx_ppi": "data/cache/myfxbook_ppi.json",
    "cad_retail": "data/cache/investing_retail_sales.json",
    "core": "data/cache/investing_core.json",
}
# JPY CPI snapshot rides along with the CPI refresh.
_EXTRA_CACHE_FILES = {"cpi": "data/cache/tokyo_core_cpi.json"}

# Hours to wait before re-attempting a due cell whose page we FETCHED AND PARSED
# successfully but which carried no new release (the print ran late), so a late
# release does not make us hammer the source every run.
#
# This cooldown is deliberately NOT applied to cells whose fetch failed
# (Cloudflare block / 429 / parse error). Stamping those was a bug: a blocked
# cell went quiet for 8h and `--due` then reported "Nothing due to fetch" while
# the data was genuinely stale (USD JOLTS + ADP sat a full print behind for
# ~a week in Aug 2026). A blocked cell now retries on the next run.
_DUE_COOLDOWN_HOURS = 8


def _cell_target(ind: str, ccy: str, source: str) -> str | None:
    """Map a due calendar cell to the refresh target THIS script owns, or None
    if the cell is fed by a source main.py/GitHub Actions refreshes (TE/ABS).

    This script fetches the Cloudflare-blocked sources: all Investing + Myfxbook
    cells, plus the whole sPMI sweep (refresh_spmi also pulls TE-Swiss + BusinessNZ)."""
    if ind == "mpmi":
        return "mpmi"
    if ind == "spmi":
        return "spmi"          # refresh_spmi covers Investing + CHF(TE) + NZD(BusinessNZ)
    if ind == "gdp" and source == "investing":   # JPY GDP via Investing id 119
        return "gdp"
    if ind == "household_spending":               # JPY Household Spending id 361
        return "household"
    if ind == "cpi":
        return "cpi"           # all 8 via Investing (JPY = Tokyo Core)
    if ind == "ppi":
        if source == "investing":   # NZD/GBP
            return "ppi"
        if source == "myfxbook":    # CHF/AUD
            return "mfx_ppi"
        return None                 # USD/EUR/JPY/CAD = TE -> main.py
    if ind == "consumer_conf" and source == "investing":   # USD
        return "cc"
    if ind == "pce":
        return "pce"
    if ind == "adp":
        return "adp"
    if ind == "jolts":
        return "jolts"
    if ind == "retail_sales" and source == "investing":    # CAD
        return "cad_retail"
    return None


def run_due(dry_run: bool = False):
    """Calendar-gated refresh: fetch only the cells whose release window has
    passed (status == 'due'), skipping ones checked within the cooldown. Most
    runs fetch nothing, which is the point: no blind sweep, far fewer 429s."""
    # Build in memory only. We persist the calendar (and thus produce a commit)
    # ONLY when a fetch actually advances it, so a no-op hourly run leaves the
    # file untouched instead of churning its "generated" timestamp every hour.
    cal = rc.build_calendar(prior=rc.load_calendar())

    targets: dict[str, list[str]] = {}     # target -> due cell keys driving it
    skipped_cooldown: list[str] = []
    not_owned = 0
    for k, e in cal["entries"].items():
        if e.get("status") != "due":
            continue
        tgt = _cell_target(e["indicator"], e["currency"], e["source"])
        if tgt is None:
            not_owned += 1
            continue
        if rc.checked_within(e, _DUE_COOLDOWN_HOURS):
            skipped_cooldown.append(k)
            continue
        targets.setdefault(tgt, []).append(k)

    # Core US CPI/PPI (gold scoring) rides along when USD CPI is due.
    if "cpi" in targets:
        targets.setdefault("core", [])

    print(f"=== Investing.com refresh (--due{' --dry-run' if dry_run else ''}) ===")
    due_total = sum(1 for e in cal["entries"].values() if e.get("status") == "due")
    print(f"due cells: {due_total} ({not_owned} owned by TE/ABS -> main.py, "
          f"{len(skipped_cooldown)} in cooldown)")
    if skipped_cooldown:
        print(f"  cooldown skip: {skipped_cooldown}")
    if not targets:
        print("Nothing due to fetch. Done.")
        return

    order = [t for t in REFRESHERS if t in targets]
    if dry_run:
        print("\nWould refresh these targets (dry run, no fetch):")
        for t in order:
            print(f"  {t:<11} <- {targets[t] or 'rides along'}")
        return

    fresh_by_target: dict[str, set[str]] = {}
    for t in order:
        driving = targets[t]
        print(f"\n>>> target '{t}'  (due: {driving or 'rides along'})")
        fresh_by_target[t] = REFRESHERS[t]() or set()

    # Rebuild so next_release advances off any new prints, then stamp ONLY the
    # cells whose source page we actually fetched and parsed. A cell that is
    # still due after a successful fetch just means the release ran late, so it
    # earns the cooldown; a cell we never got (Cloudflare / 429 / parse error)
    # is left unstamped so the next run retries it instead of going quiet for
    # _DUE_COOLDOWN_HOURS on data we never saw.
    cal = rc.build_calendar(prior=cal)
    fetched, blocked = [], []
    for t, keys in targets.items():
        fresh = fresh_by_target.get(t, set())
        for k in keys:
            (fetched if k.split("|")[0] in fresh else blocked).append(k)
    rc.mark_checked(cal, fetched)
    rc.save_calendar(cal)

    if fetched:
        print(f"\ncooldown stamped ({_DUE_COOLDOWN_HOURS}h, page fetched OK): {sorted(fetched)}")
    if blocked:
        print(f"NOT stamped (fetch failed, retry next run): {sorted(blocked)}")

    files = []
    for t in order:
        files.append(_CACHE_FILES[t])
        if t in _EXTRA_CACHE_FILES:
            files.append(_EXTRA_CACHE_FILES[t])
    files.append("data/cache/release_calendar.json")
    print("\nDone. Now commit + push:")
    print("  git add " + " ".join(files))
    print("  git commit -m 'Investing refresh (due)'")
    print("  git push")


def main():
    raw = [a.lower() for a in sys.argv[1:]]
    flags = {a.lstrip("-") for a in raw if a.startswith("-")}
    targets = [a for a in raw if not a.startswith("-")]

    # Calendar-gated mode: fetch only what the release calendar says is due.
    if "due" in flags:
        run_due(dry_run="dry-run" in flags or "dry_run" in flags)
        return

    if targets:
        unknown = [a for a in targets if a not in REFRESHERS]
        if unknown:
            print(f"Unknown refresh target(s): {unknown}")
            print(f"Available: {', '.join(REFRESHERS)}")
            return
        order = targets
    else:
        order = list(REFRESHERS)

    print(f"=== Investing.com cache refresh: {', '.join(order)} ===")
    for name in order:
        REFRESHERS[name]()

    files = []
    for name in order:
        files.append(_CACHE_FILES[name])
        if name in _EXTRA_CACHE_FILES:
            files.append(_EXTRA_CACHE_FILES[name])
    print("\nDone. Now commit + push:")
    print("  git add " + " ".join(files))
    print("  git commit -m 'Refresh Investing cache'")
    print("  git push")


if __name__ == "__main__":
    main()
