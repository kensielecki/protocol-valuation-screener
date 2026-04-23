#!/usr/bin/env python3
"""
fetch_fundamentals.py  v1
Proof-of-concept: pull DL fees/revenue/holders + CG market data,
join on slug_map.json, compute TradFi-style valuation metrics,
print formatted table to terminal.  No file I/O.

DL earnings endpoint is currently non-functional (returns Internal Error
for both /overview/fees?dataType=dailyEarnings and per-protocol variants).
earnings_30d / P/E / earnings_yield will be None until it recovers.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLUG_MAP_PATH = Path(__file__).parent / "slug_map.json"

DL_BASE = "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
DL_FEES_URL     = DL_BASE
DL_REVENUE_URL  = DL_BASE + "&dataType=dailyRevenue"
DL_HOLDERS_URL  = DL_BASE + "&dataType=dailyHoldersRevenue"
DL_PROTOCOLS_URL = "https://api.llama.fi/protocols"

CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
CG_BATCH_SIZE  = 250
CG_SLEEP_S     = 2   # free-tier rate limit

ANNUALISE_DAYS = 30  # multiply 30d × (365/30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_div(num, denom):
    """Return num/denom, or None if either arg is None/zero."""
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def annualise(val_30d):
    """Convert 30d total to annualised figure."""
    if val_30d is None:
        return None
    return val_30d * 365 / ANNUALISE_DAYS


def fetch_json(url, label=""):
    """HTTP GET → parsed JSON.  Raises on non-200."""
    req = urllib.request.Request(url, headers={"User-Agent": "token-valuations/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} fetching {label or url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [WARN] Error fetching {label or url}: {e}", file=sys.stderr)
        return None


def fmt(val, fmt_str, suffix=""):
    """Format a value, returning '—' if None."""
    if val is None:
        return "—"
    return format(val, fmt_str) + suffix


def fmt_large(val):
    """Format large dollar values as $XB / $XM."""
    if val is None:
        return "—"
    if val >= 1e9:
        return f"${val/1e9:.1f}B"
    if val >= 1e6:
        return f"${val/1e6:.1f}M"
    return f"${val:,.0f}"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def load_slug_map():
    with open(SLUG_MAP_PATH) as f:
        return json.load(f)


def build_dl_index(url, label):
    """Fetch a DL overview endpoint → {slug: {total24h, total7d, total30d, category, chains}}."""
    print(f"  Fetching DL {label}…", end=" ", flush=True)
    data = fetch_json(url, label)
    if not data:
        print("FAILED")
        return {}
    index = {}
    for p in data.get("protocols", []):
        slug = p.get("slug")
        if slug:
            index[slug] = {
                "total24h": p.get("total24h"),
                "total7d":  p.get("total7d"),
                "total30d": p.get("total30d"),
                "category": p.get("category"),
                "chains":   p.get("chains", []),
            }
    print(f"{len(index)} protocols")
    return index


def build_tvl_index():
    """Fetch /protocols → {slug: tvl}."""
    print("  Fetching DL TVL (protocols)…", end=" ", flush=True)
    data = fetch_json(DL_PROTOCOLS_URL, "DL protocols")
    if not data:
        print("FAILED")
        return {}
    index = {p.get("slug"): p.get("tvl") for p in data if p.get("slug")}
    print(f"{len(index)} protocols")
    return index


def fetch_cg_markets(gecko_ids):
    """Fetch mcap + fdv for gecko_ids list. Returns {gecko_id: {mcap, fdv}}."""
    result = {}
    batches = [gecko_ids[i:i+CG_BATCH_SIZE] for i in range(0, len(gecko_ids), CG_BATCH_SIZE)]
    for i, batch in enumerate(batches):
        ids_str = ",".join(batch)
        url = (f"{CG_MARKETS_URL}?vs_currency=usd&ids={ids_str}"
               f"&per_page={CG_BATCH_SIZE}&sparkline=false")
        print(f"  Fetching CoinGecko batch {i+1}/{len(batches)} ({len(batch)} IDs)…", end=" ", flush=True)
        data = fetch_json(url, "CoinGecko markets")
        if data:
            for coin in data:
                cid = coin.get("id")
                if cid:
                    result[cid] = {
                        "mcap": coin.get("market_cap"),
                        "fdv":  coin.get("fully_diluted_valuation"),
                    }
            print(f"{len(data)} returned")
        else:
            print("FAILED")
        if i < len(batches) - 1:
            time.sleep(CG_SLEEP_S)
    return result


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_row(slug, meta, fees_idx, rev_idx, holders_idx, tvl_idx, cg_data):
    gecko_id = meta["gecko_id"]
    display   = meta["display"]
    category  = meta["category"]

    fees_entry    = fees_idx.get(slug, {})
    rev_entry     = rev_idx.get(slug, {})
    holders_entry = holders_idx.get(slug, {})
    cg_entry      = cg_data.get(gecko_id, {})

    fees_24h     = fees_entry.get("total24h")
    fees_30d     = fees_entry.get("total30d")
    rev_24h      = rev_entry.get("total24h")
    rev_30d      = rev_entry.get("total30d")
    holders_30d  = holders_entry.get("total30d")
    earnings_30d = None  # DL earnings endpoint non-functional; see module docstring

    mcap = cg_entry.get("mcap")
    fdv  = cg_entry.get("fdv")
    tvl  = tvl_idx.get(slug)

    # Annualised
    ann_rev      = annualise(rev_30d)
    ann_earn     = annualise(earnings_30d)  # None
    ann_fees     = annualise(fees_30d)
    ann_holders  = annualise(holders_30d)

    # Valuation metrics
    ps_ratio      = safe_div(mcap, ann_rev)
    fdv_revenue   = safe_div(fdv, ann_rev)
    pe_ratio      = safe_div(mcap, ann_earn) if ann_earn and ann_earn > 0 else None
    earnings_yield = (safe_div(ann_earn, mcap) * 100
                      if safe_div(ann_earn, mcap) is not None else None)
    take_rate_raw  = safe_div(rev_30d, fees_30d)
    take_rate      = take_rate_raw * 100 if take_rate_raw is not None else None
    holder_yield_raw = safe_div(ann_holders, mcap)
    holder_yield   = holder_yield_raw * 100 if holder_yield_raw is not None else None
    tvl_mcap       = safe_div(tvl, mcap)

    # Data quality: low if any primary source is missing
    missing = (mcap is None or rev_30d is None or fees_30d is None)
    data_quality = "low" if missing else "ok"

    chain = fees_entry.get("chains", [])
    chain_str = chain[0] if len(chain) == 1 else ("Multi" if len(chain) > 1 else "—")

    return {
        "protocol":      display,
        "category":      category,
        "chain":         chain_str,
        "fees_24h":      fees_24h,
        "revenue_24h":   rev_24h,
        "fees_30d":      fees_30d,
        "revenue_30d":   rev_30d,
        "earnings_30d":  earnings_30d,
        "holders_rev_30d": holders_30d,
        "ann_revenue":   ann_rev,
        "ann_earnings":  ann_earn,
        "ann_fees":      ann_fees,
        "ann_holders_rev": ann_holders,
        "mcap":          mcap,
        "fdv":           fdv,
        "tvl":           tvl,
        "ps_ratio":      ps_ratio,
        "fdv_revenue":   fdv_revenue,
        "pe_ratio":      pe_ratio,
        "earnings_yield": earnings_yield,
        "take_rate":     take_rate,
        "holder_yield":  holder_yield,
        "tvl_mcap":      tvl_mcap,
        "data_quality":  data_quality,
        "coingecko_id":  gecko_id,
        "defillama_slug": slug,
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_table(rows):
    COL_W = {
        "protocol":      22,
        "category":      16,
        "fees_30d":      12,
        "revenue_30d":   12,
        "mcap":          10,
        "take_rate":      9,
        "ps_ratio":       8,
        "fdv_revenue":    9,
        "holder_yield":   9,
        "tvl_mcap":       8,
        "data_quality":   5,
    }

    header = (
        f"{'Protocol':<{COL_W['protocol']}}"
        f"{'Category':<{COL_W['category']}}"
        f"{'Fees 30d':>{COL_W['fees_30d']}}"
        f"{'Rev 30d':>{COL_W['revenue_30d']}}"
        f"{'MCap':>{COL_W['mcap']}}"
        f"{'Take%':>{COL_W['take_rate']}}"
        f"{'P/S':>{COL_W['ps_ratio']}}"
        f"{'FDV/Rev':>{COL_W['fdv_revenue']}}"
        f"{'HldYld%':>{COL_W['holder_yield']}}"
        f"{'TVL/MC':>{COL_W['tvl_mcap']}}"
        f"  {'DQ'}"
    )
    sep = "─" * len(header)

    print()
    print("=" * len(header))
    print("  DeFi Protocol Fundamental Valuations")
    print("=" * len(header))
    print(header)
    print(sep)

    for r in rows:
        dq_flag = "" if r["data_quality"] == "ok" else "⚠"
        line = (
            f"{r['protocol']:<{COL_W['protocol']}}"
            f"{r['category']:<{COL_W['category']}}"
            f"{fmt_large(r['fees_30d']):>{COL_W['fees_30d']}}"
            f"{fmt_large(r['revenue_30d']):>{COL_W['revenue_30d']}}"
            f"{fmt_large(r['mcap']):>{COL_W['mcap']}}"
            f"{fmt(r['take_rate'], '.1f', '%'):>{COL_W['take_rate']}}"
            f"{fmt(r['ps_ratio'], '.1f', 'x'):>{COL_W['ps_ratio']}}"
            f"{fmt(r['fdv_revenue'], '.1f', 'x'):>{COL_W['fdv_revenue']}}"
            f"{fmt(r['holder_yield'], '.2f', '%'):>{COL_W['holder_yield']}}"
            f"{fmt(r['tvl_mcap'], '.2f', 'x'):>{COL_W['tvl_mcap']}}"
            f"  {dq_flag}"
        )
        print(line)

    print(sep)
    ok_count  = sum(1 for r in rows if r["data_quality"] == "ok")
    low_count = sum(1 for r in rows if r["data_quality"] == "low")
    print(f"  {len(rows)} protocols  |  {ok_count} ok  |  {low_count} low-quality (⚠)  |  P/E n/a (DL earnings endpoint down)")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    slug_map = load_slug_map()
    slugs = list(slug_map.keys())
    gecko_ids = list({v["gecko_id"] for v in slug_map.values()})

    print(f"Loaded {len(slugs)} protocols from slug_map.json")
    print()
    print("Fetching data…")

    fees_idx    = build_dl_index(DL_FEES_URL,    "fees")
    rev_idx     = build_dl_index(DL_REVENUE_URL, "revenue")
    holders_idx = build_dl_index(DL_HOLDERS_URL, "holdersRevenue")
    tvl_idx     = build_tvl_index()
    time.sleep(CG_SLEEP_S)
    cg_data     = fetch_cg_markets(gecko_ids)

    print()
    print("Computing metrics…")

    rows = []
    for slug, meta in slug_map.items():
        row = compute_row(slug, meta, fees_idx, rev_idx, holders_idx, tvl_idx, cg_data)
        rows.append(row)

    # Sort by revenue_30d descending (highest revenue first); None sorts last
    rows.sort(key=lambda r: r["revenue_30d"] or 0, reverse=True)

    print_table(rows)

    # Quick debug: flag any slugs missing from DL fees
    missing_dl = [s for s in slugs if s not in fees_idx]
    if missing_dl:
        print(f"  Slugs not found in DL fees endpoint: {missing_dl}")

    missing_cg = [v["gecko_id"] for v in slug_map.values() if v["gecko_id"] not in cg_data]
    if missing_cg:
        print(f"  Gecko IDs not found in CoinGecko: {missing_cg}")


if __name__ == "__main__":
    main()
