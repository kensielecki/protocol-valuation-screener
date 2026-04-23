#!/usr/bin/env python3
"""
fetch_fundamentals.py  v3
Full pipeline: fetch DL + CG → compute metrics → write TSV → diff → log.

Column order is locked — do not reorder.

TSV columns (final):
  date | protocol | category | chain |
  fees_30d | revenue_30d | earnings_30d | holders_rev_30d |
  ann_fees | ann_revenue | ann_earnings | ann_holders_rev |
  mcap | fdv | circulating_supply |
  ps_ratio | pe_ratio | eps_token | earnings_yield | holder_yield |
  revenue_momentum | supply_inflation | real_yield |
  data_quality | coingecko_id | defillama_slug

Notes:
- earnings_30d = holders_rev_30d (DL dailyEarnings endpoint is dead — holders revenue
  is the correct proxy: it represents what accrues to token holders, i.e. equity earnings).
- pe_ratio, eps_token, earnings_yield share the same None condition: ann_earnings <= 0.
- holder_yield and earnings_yield are equivalent (both derived from holders revenue / mcap).
"""

import csv
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT       = Path(__file__).parent
SLUG_MAP   = ROOT / "slug_map.json"
OUTPUT_DIR = ROOT / "output"
LOG_DIR    = OUTPUT_DIR / "logs"
DIFF_DIR   = OUTPUT_DIR / "diffs"

for d in (OUTPUT_DIR, LOG_DIR, DIFF_DIR):
    d.mkdir(parents=True, exist_ok=True)

DATE_STR   = date.today().strftime("%y%m%d")          # YYMMDD
DATE_ISO   = date.today().isoformat()                  # YYYY-MM-DD
TSV_FILE   = OUTPUT_DIR / f"{DATE_STR}_fundamentals.tsv"
DIFF_FILE  = DIFF_DIR   / f"{DATE_STR}_fundamentals_changes.tsv"
LOG_FILE   = LOG_DIR    / f"{DATE_STR}_run.log"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DL_BASE         = ("https://api.llama.fi/overview/fees"
                   "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
DL_FEES_URL     = DL_BASE
DL_REVENUE_URL  = DL_BASE + "&dataType=dailyRevenue"
DL_HOLDERS_URL  = DL_BASE + "&dataType=dailyHoldersRevenue"
CG_MARKETS_URL  = "https://api.coingecko.com/api/v3/coins/markets"

CG_BATCH_SIZE   = 250
CG_SLEEP_S      = 2      # free-tier rate limit

# Alert thresholds (diff flagging; Telegram removed from scope)
EARNINGS_YIELD_THRESHOLD = 10.0   # % — flag when crossed above
PS_DROP_THRESHOLD        = 0.30   # 30% drop in ps_ratio day-over-day
TOP_N_EARNINGS_YIELD     = 10     # protocols to track for "newly entered top-N"

# TSV columns — final, locked, do not reorder
TSV_COLUMNS = [
    "date", "protocol", "category", "chain",
    "fees_30d", "revenue_30d", "earnings_30d", "holders_rev_30d",
    "ann_fees", "ann_revenue", "ann_earnings", "ann_holders_rev",
    "mcap", "fdv", "circulating_supply",
    "ps_ratio", "pe_ratio", "eps_token", "earnings_yield", "holder_yield",
    "revenue_momentum", "supply_inflation", "real_yield",
    "data_quality", "coingecko_id", "defillama_slug",
]

# ---------------------------------------------------------------------------
# Logging — writes to file and stdout simultaneously
# ---------------------------------------------------------------------------

def setup_logging():
    fmt = "[%(asctime)s] %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_div(num, denom):
    """Return num/denom, or None if either arg is None or denom is zero."""
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def annualise_30d(val):
    """30d total → annualised figure."""
    return None if val is None else val * 365 / 30


def fetch_json(url, label=""):
    req = urllib.request.Request(url, headers={"User-Agent": "token-valuations/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("HTTP %d fetching %s", e.code, label or url)
        return None
    except Exception as e:
        log.warning("Error fetching %s: %s", label or url, e)
        return None


def fmt_val(val, decimals=2):
    """Format a float for TSV output — empty string if None."""
    if val is None:
        return ""
    if isinstance(val, float):
        return f"{val:.{decimals}f}"
    return str(val)


def load_supply_30d_ago():
    """
    Find the fundamentals TSV closest to 30 days ago (within ±10 days).
    Returns {gecko_id: circulating_supply} for use in supply inflation calculation.
    """
    target = date.today() - timedelta(days=30)
    best_path, best_delta = None, float("inf")
    for f in OUTPUT_DIR.glob("??????_fundamentals.tsv"):
        if f.name == TSV_FILE.name:
            continue
        stem = f.stem[:6]
        try:
            file_date = datetime.strptime("20" + stem, "%Y%m%d").date()
        except ValueError:
            continue
        delta = abs((file_date - target).days)
        if delta < best_delta:
            best_delta = delta
            best_path = f
    if best_path is None or best_delta > 10:
        log.info("  No fundamentals TSV within 10d of 30d-ago target — supply_inflation will be None.")
        return {}
    log.info("  30d supply from %s  (Δ%d days from target %s)", best_path.name, best_delta, target)
    result = {}
    with open(best_path, newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            gid = row.get("coingecko_id", "").strip()
            val = row.get("circulating_supply", "").strip()
            if gid and val:
                try:
                    result[gid] = float(val)
                except ValueError:
                    pass
    log.info("  Loaded prior supply for %d gecko IDs", len(result))
    return result


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def load_slug_map():
    with open(SLUG_MAP) as f:
        return json.load(f)


def build_dl_index(url, label):
    """Fetch a DL overview endpoint → {slug: record}."""
    log.info("  Fetching DL %s…", label)
    data = fetch_json(url, label)
    if not data:
        log.error("  DL %s fetch failed", label)
        return {}
    index = {}
    for p in data.get("protocols", []):
        slug = p.get("slug")
        if slug:
            index[slug] = {
                "total24h":  p.get("total24h"),
                "total7d":   p.get("total7d"),
                "total30d":  p.get("total30d"),
                "category":  p.get("category"),
                "chains":    p.get("chains", []),
            }
    log.info("  → %d protocols", len(index))
    return index


def fetch_cg_markets(gecko_ids):
    """Fetch mcap, fdv, circulating_supply for gecko_ids. Returns {gecko_id: {...}}."""
    result = {}
    batches = [gecko_ids[i:i+CG_BATCH_SIZE] for i in range(0, len(gecko_ids), CG_BATCH_SIZE)]
    for i, batch in enumerate(batches):
        url = (f"{CG_MARKETS_URL}?vs_currency=usd&ids={','.join(batch)}"
               f"&per_page={CG_BATCH_SIZE}&sparkline=false")
        log.info("  Fetching CoinGecko batch %d/%d (%d IDs)…", i+1, len(batches), len(batch))
        data = fetch_json(url, "CoinGecko markets")
        if data:
            for coin in data:
                cid = coin.get("id")
                if cid:
                    result[cid] = {
                        "mcap":               coin.get("market_cap"),
                        "fdv":                coin.get("fully_diluted_valuation"),
                        "circulating_supply": coin.get("circulating_supply"),
                    }
            log.info("  → %d coins returned", len(data))
        else:
            log.error("  CoinGecko batch %d/%d failed", i+1, len(batches))
        if i < len(batches) - 1:
            time.sleep(CG_SLEEP_S)
    return result


# ---------------------------------------------------------------------------
# Metric computation (per-protocol)
# ---------------------------------------------------------------------------

def compute_row(slug, meta, fees_idx, rev_idx, holders_idx, cg_data, hist_supply):
    gecko_id  = meta["gecko_id"]
    display   = meta["display"]
    category  = meta["category"]

    fees_e    = fees_idx.get(slug, {})
    rev_e     = rev_idx.get(slug, {})
    holders_e = holders_idx.get(slug, {})
    cg_e      = cg_data.get(gecko_id, {})

    fees_30d     = fees_e.get("total30d")
    rev_30d      = rev_e.get("total30d")
    rev_7d       = rev_e.get("total7d")
    holders_30d  = holders_e.get("total30d")
    # earnings_30d = holders revenue (DL dailyEarnings endpoint is dead;
    # holders revenue is the correct proxy — what accrues to token equity holders)
    earnings_30d = holders_30d

    mcap               = cg_e.get("mcap")
    fdv                = cg_e.get("fdv")
    circulating_supply = cg_e.get("circulating_supply")

    # Annualised
    ann_fees    = annualise_30d(fees_30d)
    ann_rev     = annualise_30d(rev_30d)
    ann_earn    = annualise_30d(earnings_30d)
    ann_holders = annualise_30d(holders_30d)

    # Valuation metrics — all via safe_div, None if any input is None or zero
    ps_ratio = safe_div(mcap, ann_rev)

    # pe_ratio, eps_token, earnings_yield: None when ann_earnings <= 0
    if ann_earn and ann_earn > 0:
        pe_ratio      = safe_div(mcap, ann_earn)
        eps_token     = safe_div(ann_earn, circulating_supply)
        earn_yld_raw  = safe_div(ann_earn, mcap)
        earnings_yield = earn_yld_raw * 100 if earn_yld_raw is not None else None
    else:
        pe_ratio = eps_token = earnings_yield = None

    hld_raw      = safe_div(ann_holders, mcap)
    holder_yield = hld_raw * 100 if hld_raw is not None else None

    # Data quality: low if any primary source missing
    data_quality = "low" if (mcap is None or rev_30d is None or fees_30d is None) else "ok"

    # Revenue momentum: (7d daily rate / 30d daily rate) - 1
    rev_7d_daily  = safe_div(rev_7d, 7)
    rev_30d_daily = safe_div(rev_30d, 30)
    if rev_7d_daily is not None and rev_30d_daily and rev_30d_daily > 0:
        revenue_momentum = rev_7d_daily / rev_30d_daily - 1
    else:
        revenue_momentum = None

    # Supply inflation: annualised % change in circulating supply vs 30 days ago
    supply_30d_ago = hist_supply.get(gecko_id)
    if (supply_30d_ago and supply_30d_ago > 0
            and circulating_supply and circulating_supply > 0):
        supply_inflation = ((circulating_supply / supply_30d_ago) ** (365.0 / 30) - 1) * 100
    else:
        supply_inflation = None

    # Real yield: earnings yield minus annualised supply dilution
    if earnings_yield is not None and supply_inflation is not None:
        real_yield = earnings_yield - supply_inflation
    else:
        real_yield = None

    chains    = fees_e.get("chains", [])
    chain_str = chains[0] if len(chains) == 1 else ("Multi" if chains else "—")

    return {
        "date":              DATE_ISO,
        "protocol":          display,
        "category":          category,
        "chain":             chain_str,
        "fees_30d":          fees_30d,
        "revenue_30d":       rev_30d,
        "earnings_30d":      earnings_30d,
        "holders_rev_30d":   holders_30d,
        "ann_fees":          ann_fees,
        "ann_revenue":       ann_rev,
        "ann_earnings":      ann_earn,
        "ann_holders_rev":   ann_holders,
        "mcap":              mcap,
        "fdv":               fdv,
        "circulating_supply": circulating_supply,
        "ps_ratio":          ps_ratio,
        "pe_ratio":          pe_ratio,
        "eps_token":         eps_token,
        "earnings_yield":    earnings_yield,
        "holder_yield":      holder_yield,
        "revenue_momentum":  revenue_momentum,
        "supply_inflation":  supply_inflation,
        "real_yield":        real_yield,
        "data_quality":      data_quality,
        "coingecko_id":      gecko_id,
        "defillama_slug":    slug,
    }


# ---------------------------------------------------------------------------
# TSV I/O
# ---------------------------------------------------------------------------

def write_tsv(rows, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=TSV_COLUMNS,
            delimiter="\t",
            extrasaction="ignore",   # drops _circulating_supply and other internals
        )
        writer.writeheader()
        for r in rows:
            # Format floats; leave None as empty string
            out = {}
            for col in TSV_COLUMNS:
                val = r.get(col)
                if val is None:
                    out[col] = ""
                elif isinstance(val, float):
                    out[col] = f"{val:.6f}"
                else:
                    out[col] = str(val)
            writer.writerow(out)
    log.info("Wrote %d rows → %s", len(rows), path.name)


def load_tsv(path):
    """Load a TSV into a list of dicts. Returns [] if file missing."""
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def find_prev_tsv():
    """Find the most recent previous fundamentals TSV (not today's)."""
    candidates = sorted(
        OUTPUT_DIR.glob("??????_fundamentals.tsv"),
        reverse=True,
    )
    for c in candidates:
        if c.name != TSV_FILE.name:
            return c
    return None


# ---------------------------------------------------------------------------
# Diff / alert flagging
# ---------------------------------------------------------------------------

def compute_diff(current_rows, prev_tsv_path):
    """
    Compare current run against previous TSV.
    Returns list of dicts describing flagged changes.
    Conditions:
      1. ps_ratio dropped >30% day-over-day
      2. earnings_yield crossed above EARNINGS_YIELD_THRESHOLD
      3. Protocol newly entered top-N by earnings_yield
    """
    prev_rows = load_tsv(prev_tsv_path) if prev_tsv_path else []
    if not prev_rows:
        log.info("  No previous TSV found — skipping diff.")
        return []

    def _f(row, col):
        """Parse float from TSV row, return None if empty/invalid."""
        v = row.get(col, "")
        try:
            return float(v) if v != "" else None
        except ValueError:
            return None

    prev_by_slug = {r["defillama_slug"]: r for r in prev_rows}

    # Top-N by earnings_yield in current run (skip None)
    sorted_by_ey = sorted(
        [r for r in current_rows if r.get("earnings_yield") is not None],
        key=lambda r: r["earnings_yield"] or 0,
        reverse=True,
    )
    current_top_n = {r["defillama_slug"] for r in sorted_by_ey[:TOP_N_EARNINGS_YIELD]}

    prev_sorted_ey = sorted(
        [r for r in prev_rows if _f(r, "earnings_yield") is not None],
        key=lambda r: _f(r, "earnings_yield") or 0,
        reverse=True,
    )
    prev_top_n = {r["defillama_slug"] for r in prev_sorted_ey[:TOP_N_EARNINGS_YIELD]}

    flags = []

    for r in current_rows:
        slug = r["defillama_slug"]
        name = r["protocol"]
        pr   = prev_by_slug.get(slug)

        # Newly entered top-N
        if slug in current_top_n and slug not in prev_top_n:
            flags.append({
                "defillama_slug": slug,
                "protocol":       name,
                "flag":           "TOP_N_ENTRY",
                "detail":         f"Entered top-{TOP_N_EARNINGS_YIELD} by earnings_yield",
                "current_value":  fmt_val(r.get("earnings_yield")),
                "prev_value":     fmt_val(_f(pr, "earnings_yield")) if pr else "",
            })

        if pr is None:
            continue

        # P/S dropped >30%
        cur_ps  = r.get("ps_ratio")
        prev_ps = _f(pr, "ps_ratio")
        if cur_ps is not None and prev_ps and prev_ps > 0:
            drop = (prev_ps - cur_ps) / prev_ps
            if drop >= PS_DROP_THRESHOLD:
                flags.append({
                    "defillama_slug": slug,
                    "protocol":       name,
                    "flag":           "PS_DROP",
                    "detail":         f"P/S dropped {drop*100:.1f}%",
                    "current_value":  fmt_val(cur_ps),
                    "prev_value":     fmt_val(prev_ps),
                })

        # Earnings yield crossed above threshold
        cur_ey  = r.get("earnings_yield")
        prev_ey = _f(pr, "earnings_yield")
        if (cur_ey is not None and prev_ey is not None
                and prev_ey < EARNINGS_YIELD_THRESHOLD <= cur_ey):
            flags.append({
                "defillama_slug": slug,
                "protocol":       name,
                "flag":           "EARNINGS_YIELD_CROSS",
                "detail":         f"Earnings yield crossed {EARNINGS_YIELD_THRESHOLD}%",
                "current_value":  fmt_val(cur_ey),
                "prev_value":     fmt_val(prev_ey),
            })

    return flags


def write_diff(flags, path):
    if not flags:
        log.info("  No alert-level changes detected.")
        return
    cols = ["defillama_slug", "protocol", "flag", "detail", "current_value", "prev_value"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
        writer.writeheader()
        writer.writerows(flags)
    log.info("  %d alert(s) written → %s", len(flags), path.name)


# ---------------------------------------------------------------------------
# Terminal display
# ---------------------------------------------------------------------------

def print_table(rows):
    W = {
        "protocol": 22, "category": 16, "fees_30d": 11,
        "revenue_30d": 11, "mcap": 10, "ps_ratio": 8,
        "pe_ratio": 8, "earnings_yield": 10, "holder_yield": 9,
        "revenue_momentum": 8,
    }

    def _fv(val, fmt_str, suffix=""):
        return "—" if val is None else format(val, fmt_str) + suffix

    def _fm(val):
        if val is None: return "—"
        if abs(val) >= 1e9: return f"${val/1e9:.1f}B"
        if abs(val) >= 1e6: return f"${val/1e6:.1f}M"
        if abs(val) >= 1e3: return f"${val/1e3:.0f}K"
        return f"${val:,.0f}"

    def _fmom(val):
        if val is None: return "—"
        arrow = "▲" if val >= 0 else "▼"
        return f"{arrow}{abs(val)*100:.0f}%"

    header = (
        f"{'Protocol':<{W['protocol']}}"
        f"{'Category':<{W['category']}}"
        f"{'Fees 30d':>{W['fees_30d']}}"
        f"{'Rev 30d':>{W['revenue_30d']}}"
        f"{'MCap':>{W['mcap']}}"
        f"{'P/S':>{W['ps_ratio']}}"
        f"{'P/E':>{W['pe_ratio']}}"
        f"{'Earn Yld%':>{W['earnings_yield']}}"
        f"{'HldYld%':>{W['holder_yield']}}"
        f"{'Mom%':>{W['revenue_momentum']}}"
        f"  DQ"
    )
    sep = "─" * len(header)

    print()
    print("=" * len(header))
    print(f"  DeFi Protocol Fundamental Valuations  ·  {DATE_ISO}")
    print("=" * len(header))
    print(header)
    print(sep)

    for r in rows:
        dq = "" if r["data_quality"] == "ok" else "⚠"
        line = (
            f"{r['protocol']:<{W['protocol']}}"
            f"{r['category']:<{W['category']}}"
            f"{_fm(r['fees_30d']):>{W['fees_30d']}}"
            f"{_fm(r['revenue_30d']):>{W['revenue_30d']}}"
            f"{_fm(r['mcap']):>{W['mcap']}}"
            f"{_fv(r['ps_ratio'],      '.1f', 'x'):>{W['ps_ratio']}}"
            f"{_fv(r['pe_ratio'],      '.1f', 'x'):>{W['pe_ratio']}}"
            f"{_fv(r['earnings_yield'],'.2f', '%'):>{W['earnings_yield']}}"
            f"{_fv(r['holder_yield'],  '.2f', '%'):>{W['holder_yield']}}"
            f"{_fmom(r.get('revenue_momentum')):>{W['revenue_momentum']}}"
            f"  {dq}"
        )
        print(line)

    print(sep)
    ok  = sum(1 for r in rows if r["data_quality"] == "ok")
    low = sum(1 for r in rows if r["data_quality"] == "low")
    with_earnings = sum(1 for r in rows if r.get("earnings_30d") is not None)
    print(f"  {len(rows)} protocols  ·  {ok} ok  ·  {low} low-quality (⚠)"
          f"  ·  earnings data: {with_earnings}/{len(rows)} protocols")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    log.info("=" * 60)
    log.info("DeFi Valuation Screener — %s", DATE_ISO)
    log.info("=" * 60)

    slug_map  = load_slug_map()
    slugs     = list(slug_map.keys())
    gecko_ids = list({v["gecko_id"] for v in slug_map.values()})
    log.info("Loaded %d protocols from slug_map.json", len(slugs))

    log.info("Fetching data sources…")
    fees_idx    = build_dl_index(DL_FEES_URL,    "fees")
    rev_idx     = build_dl_index(DL_REVENUE_URL, "Protocol Revenue")
    holders_idx = build_dl_index(DL_HOLDERS_URL, "Holder Revenue")
    time.sleep(CG_SLEEP_S)
    cg_data     = fetch_cg_markets(gecko_ids)

    log.info("Loading 30d-ago supply for supply inflation…")
    hist_supply = load_supply_30d_ago()

    log.info("Computing metrics…")
    rows = [
        compute_row(slug, meta, fees_idx, rev_idx, holders_idx, cg_data, hist_supply)
        for slug, meta in slug_map.items()
    ]

    # Sort: earnings_yield descending (None last), then revenue_30d descending as tiebreaker
    rows.sort(key=lambda r: (
        -(r["earnings_yield"] or 0) if r["earnings_yield"] is not None else float("inf"),
        -(r["revenue_30d"]   or 0),
    ))

    print_table(rows)

    log.info("Writing TSV snapshot…")
    write_tsv(rows, TSV_FILE)

    log.info("Computing diff vs previous run…")
    prev_tsv = find_prev_tsv()
    if prev_tsv:
        log.info("  Comparing against %s", prev_tsv.name)
    flags = compute_diff(rows, prev_tsv)
    if flags:
        write_diff(flags, DIFF_FILE)
    else:
        log.info("  No alert-level changes.")

    # Diagnostics
    missing_dl = [s for s in slugs if s not in fees_idx]
    missing_cg = [v["gecko_id"] for v in slug_map.values() if v["gecko_id"] not in cg_data]
    if missing_dl:
        log.warning("Slugs not found in DL fees: %s", missing_dl)
    if missing_cg:
        log.warning("Gecko IDs not found in CoinGecko: %s", missing_cg)

    ok  = sum(1 for r in rows if r["data_quality"] == "ok")
    low = sum(1 for r in rows if r["data_quality"] == "low")
    log.info("Done. %d protocols  ·  %d ok  ·  %d low-quality  ·  %d alerts",
             len(rows), ok, low, len(flags))

    return 0 if not missing_dl and not missing_cg else 1


if __name__ == "__main__":
    sys.exit(main())
