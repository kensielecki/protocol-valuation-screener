#!/usr/bin/env python3
"""
fetch_historical.py
Pull daily/quarterly historical data for 4 focus assets
(Hyperliquid, Aave, COIN, NVDA), compute revenue / P/E / yield
over time, and write output TSVs for charting.

Outputs:
  output/YYMMDD_historical_defi.tsv          — daily rows per DeFi asset
  output/YYMMDD_historical_equity.tsv        — quarterly rows per equity
  output/YYMMDD_historical_equity_daily.tsv  — daily price/mcap per equity
"""

import csv
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.request
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT       = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
LOG_DIR    = OUTPUT_DIR / "logs"

for d in (OUTPUT_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

DATE_STR          = date.today().strftime("%y%m%d")
DATE_ISO          = date.today().isoformat()
DEFI_TSV          = OUTPUT_DIR / f"{DATE_STR}_historical_defi.tsv"
EQUITY_TSV        = OUTPUT_DIR / f"{DATE_STR}_historical_equity.tsv"
EQUITY_DAILY_TSV  = OUTPUT_DIR / f"{DATE_STR}_historical_equity_daily.tsv"
LOG_FILE          = LOG_DIR    / f"{DATE_STR}_historical.log"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Asset lists are loaded dynamically from snapshot TSVs in main().
# Canonical name aliases: fundamentals.tsv protocol name → display name used in TSV output.
DEFI_NAME_ALIASES = {
    "Aave V3": "Aave",
}

DL_BASE        = "https://api.llama.fi/summary/fees"
CG_BASE        = "https://api.coingecko.com/api/v3/coins"
DL_SLEEP_S     = 1.0   # between DefiLlama calls
CG_SLEEP_S     = 2.0   # CoinGecko free-tier courtesy pause
CG_RETRY_WAIT  = 60    # seconds to wait on 429
ROLLING_WINDOW = 30    # days for rolling revenue sum
ANNUALISE      = 365 / 30

# TSV columns — locked
DEFI_COLUMNS = [
    "date", "asset", "dl_slug", "gecko_id",
    "daily_revenue", "daily_fees", "daily_holders_rev",
    "ann_revenue_rolling", "ann_fees_rolling", "ann_holders_rolling",
    "mcap", "pe_ratio", "ps_ratio", "holder_yield",
]

EQUITY_COLUMNS = [
    "date", "ticker", "company",
    "quarterly_revenue", "quarterly_earnings",
    "ann_revenue", "ann_earnings",
    "mcap", "pe_ratio", "ps_ratio", "div_yield",
]

EQUITY_DAILY_COLUMNS = [
    "date", "ticker", "mcap", "close_price", "div_yield_trailing",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
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


def safe_float(val):
    """Convert yfinance/pandas value to Python float, or None if NaN/None."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def fetch_json_raw(url, label=""):
    """HTTP GET → parsed JSON. Raises urllib.error.HTTPError on HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "token-valuations/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        log.warning("Error fetching %s: %s", label or url, e)
        return None


def fetch_json(url, label=""):
    """HTTP GET → parsed JSON. Returns None on any error (including HTTP errors)."""
    try:
        return fetch_json_raw(url, label)
    except urllib.error.HTTPError as e:
        log.warning("HTTP %d fetching %s", e.code, label or url)
        return None


def fmt_val(val):
    if val is None:
        return ""
    if isinstance(val, float):
        return f"{val:.6f}"
    return str(val)


def write_tsv(rows, path, columns):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({col: fmt_val(r.get(col)) for col in columns})
    log.info("Wrote %d rows → %s", len(rows), path.name)


# ---------------------------------------------------------------------------
# Incremental write helpers
# ---------------------------------------------------------------------------

def open_tsv_writer(path, columns):
    """
    Open a TSV for writing, write the header row immediately, and return
    (file_handle, DictWriter).  Caller must close the file handle when done.
    """
    fh = open(path, "w", newline="")
    writer = csv.DictWriter(fh, fieldnames=columns, delimiter="\t",
                            extrasaction="ignore")
    writer.writeheader()
    fh.flush()
    log.info("Opened for incremental write → %s", path.name)
    return fh, writer


def flush_rows(writer, fh, rows):
    """Append rows to an already-open TSV writer and flush to disk."""
    for r in rows:
        writer.writerow({col: fmt_val(r.get(col)) for col in writer.fieldnames})
    fh.flush()


# ---------------------------------------------------------------------------
# Dynamic asset loading from snapshot TSVs
# ---------------------------------------------------------------------------

def find_latest_tsv(pattern):
    """Return the most recently dated output file matching *pattern*."""
    candidates = sorted(OUTPUT_DIR.glob(pattern), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            f"No file matching '{pattern}' in {OUTPUT_DIR}"
        )
    return candidates[0]


def load_defi_assets():
    """
    Read all DeFi protocols from the most recent fundamentals.tsv.
    Returns a list of {name, dl_slug, gecko_id} dicts.
    Protocols missing a slug or gecko ID are skipped with a warning.
    """
    path = find_latest_tsv("??????_fundamentals.tsv")
    log.info("Loading DeFi assets from %s", path.name)
    assets = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            raw_name = row.get("protocol", "").strip()
            slug     = row.get("defillama_slug", "").strip()
            gid      = row.get("coingecko_id", "").strip()
            if not slug or not gid:
                log.warning("  Skipping %s — missing slug or gecko_id", raw_name)
                continue
            name = DEFI_NAME_ALIASES.get(raw_name, raw_name)
            assets.append({"name": name, "dl_slug": slug, "gecko_id": gid})
    log.info("  Loaded %d DeFi assets", len(assets))
    return assets


def load_equity_assets():
    """
    Read all equity tickers from the most recent equities.tsv.
    Returns a list of {ticker, company} dicts.
    """
    path = find_latest_tsv("??????_equities.tsv")
    log.info("Loading equity assets from %s", path.name)
    assets = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            ticker  = row.get("ticker", "").strip()
            company = row.get("company", "").strip()
            if not ticker:
                continue
            assets.append({"ticker": ticker, "company": company})
    log.info("  Loaded %d equity assets", len(assets))
    return assets


# ---------------------------------------------------------------------------
# Part 1 — DeFi historical data
# ---------------------------------------------------------------------------

def fetch_dl_chart(dl_slug, data_type):
    """
    Fetch totalDataChart for a slug + dataType.
    Returns dict {date_str: float_or_None}.
    """
    url = f"{DL_BASE}/{dl_slug}?dataType={data_type}"
    log.info("    DL %s  dataType=%s …", dl_slug, data_type)
    try:
        data = fetch_json_raw(url, f"DL/{dl_slug}/{data_type}")
    except urllib.error.HTTPError as e:
        log.warning("    HTTP %d for DL %s/%s", e.code, dl_slug, data_type)
        return {}
    if not data:
        log.warning("    No data returned for DL %s/%s", dl_slug, data_type)
        return {}

    result = {}
    for entry in data.get("totalDataChart", []):
        if not entry or len(entry) < 2:
            continue
        ts, val = entry[0], entry[1]
        try:
            d = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
            result[d] = float(val) if val is not None else None
        except (ValueError, TypeError, OSError):
            continue
    log.info("    → %d data points", len(result))
    return result


def fetch_cg_market_chart(gecko_id):
    """
    Fetch market cap history from CoinGecko (free tier).
    Tries `days=max` first (returns weekly for older data on Pro tier);
    falls back to `days=365` (daily, works on free tier) on 401/403.
    Forward-fill of any weekly gaps happens in build_defi_history.
    Retries once on 429. Returns dict {date_str: float}.
    """
    log.info("    CoinGecko market_chart for %s …", gecko_id)

    for days in ("max", "365"):
        url = (f"{CG_BASE}/{gecko_id}/market_chart"
               f"?vs_currency=usd&days={days}")
        data = None
        for attempt in range(2):
            try:
                data = fetch_json_raw(url, f"CoinGecko/{gecko_id}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    log.warning("    CoinGecko 429 — sleeping %ds, retrying…", CG_RETRY_WAIT)
                    time.sleep(CG_RETRY_WAIT)
                    continue
                if e.code in (401, 403) and days == "max":
                    log.warning("    CoinGecko days=max returned %d — falling back to days=365", e.code)
                    break  # break inner loop, outer loop continues to days=365
                log.warning("    CoinGecko HTTP %d for %s", e.code, gecko_id)
                return {}
            except Exception as e:
                log.warning("    CoinGecko error for %s: %s", gecko_id, e)
                return {}
        if data is not None:
            break
    else:
        return {}

    if not data:
        return {}

    result = {}
    for ts_ms, val in data.get("market_caps", []):
        try:
            d = datetime.utcfromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d")
            result[d] = float(val)
        except (ValueError, TypeError):
            continue
    log.info("    → %d data points", len(result))
    return result


def build_defi_history(asset):
    """
    Fetch and compute historical metrics for one DeFi asset.
    Returns list of row dicts (one per day).
    """
    import pandas as pd

    name     = asset["name"]
    dl_slug  = asset["dl_slug"]
    gecko_id = asset["gecko_id"]

    log.info("  [DeFi] %s", name)

    try:
        rev_chart     = fetch_dl_chart(dl_slug, "dailyRevenue")
        time.sleep(DL_SLEEP_S)
        fees_chart    = fetch_dl_chart(dl_slug, "dailyFees")
        time.sleep(DL_SLEEP_S)
        holders_chart = fetch_dl_chart(dl_slug, "dailyHoldersRevenue")
        time.sleep(CG_SLEEP_S)
        mcap_chart    = fetch_cg_market_chart(gecko_id)
    except Exception as e:
        log.error("  %s: unexpected error during fetch: %s", name, e)
        return []

    if not rev_chart and not fees_chart:
        log.warning("  %s: no revenue/fees data — skipping", name)
        return []

    # Build DataFrame indexed by date
    all_dates = sorted(
        set(rev_chart) | set(fees_chart) | set(holders_chart) | set(mcap_chart)
    )
    if not all_dates:
        return []

    def chart_to_series(chart, dates):
        return pd.Series(
            {pd.Timestamp(d): chart.get(d) for d in dates},
            dtype=float,
        )

    df = pd.DataFrame(index=pd.DatetimeIndex(all_dates))
    df.index.name = "date"
    df["daily_revenue"]     = chart_to_series(rev_chart,     all_dates)
    df["daily_fees"]        = chart_to_series(fees_chart,    all_dates)
    df["daily_holders_rev"] = chart_to_series(holders_chart, all_dates)
    df["mcap"]              = chart_to_series(mcap_chart,    all_dates)

    # Rolling 30-day sums — require full window so partial early dates are excluded
    df["rev_30d"]     = df["daily_revenue"].rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).sum()
    df["fees_30d"]    = df["daily_fees"].rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).sum()
    df["holders_30d"] = df["daily_holders_rev"].rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).sum()

    # Annualise
    df["ann_revenue_rolling"]  = df["rev_30d"]     * ANNUALISE
    df["ann_fees_rolling"]     = df["fees_30d"]    * ANNUALISE
    df["ann_holders_rolling"]  = df["holders_30d"] * ANNUALISE

    # Forward-fill market cap to bridge weekly→daily gaps in older CoinGecko data.
    # Rows before the first ever market cap data point remain NaN.
    df["mcap"] = df["mcap"].ffill()

    # Limit to last 5 years
    cutoff = pd.Timestamp(date.today()) - pd.Timedelta(days=5 * 365)
    df = df[df.index >= cutoff]

    # Require rolling revenue; market cap can be absent (ratios will be None)
    df = df.dropna(subset=["ann_revenue_rolling"])

    if df.empty:
        log.warning("  %s: no complete rows after filtering", name)
        return []

    rows = []
    for dt, row in df.iterrows():
        mcap        = safe_float(row["mcap"])
        ann_rev     = safe_float(row["ann_revenue_rolling"])
        ann_fees    = safe_float(row["ann_fees_rolling"])
        ann_holders = safe_float(row["ann_holders_rolling"])

        # Both pe_ratio and ps_ratio use ann_revenue_rolling as denominator
        ps_ratio    = safe_div(mcap, ann_rev)
        pe_ratio    = safe_div(mcap, ann_rev) if (ann_rev and ann_rev > 0) else None

        h_raw        = safe_div(ann_holders, mcap)
        holder_yield = h_raw * 100 if h_raw is not None else None

        rows.append({
            "date":                dt.strftime("%Y-%m-%d"),
            "asset":               name,
            "dl_slug":             dl_slug,
            "gecko_id":            gecko_id,
            "daily_revenue":       safe_float(row["daily_revenue"]),
            "daily_fees":          safe_float(row["daily_fees"]),
            "daily_holders_rev":   safe_float(row["daily_holders_rev"]),
            "ann_revenue_rolling": ann_rev,
            "ann_fees_rolling":    ann_fees,
            "ann_holders_rolling": ann_holders,
            "mcap":                mcap,
            "pe_ratio":            pe_ratio,
            "ps_ratio":            ps_ratio,
            "holder_yield":        holder_yield,
        })

    log.info("  %s: %d rows  (%s → %s)",
             name, len(rows), rows[0]["date"], rows[-1]["date"])
    return rows


# ---------------------------------------------------------------------------
# Part 2 — Equity historical data
# ---------------------------------------------------------------------------

# Row labels yfinance may use for each metric (old and new API)
_REV_LABELS = [
    "Total Revenue", "TotalRevenue", "Revenue",
]
_INC_LABELS = [
    "Net Income", "NetIncome",
    "Net Income Common Stockholders",
    "Net Income Applicable To Common Shares",
]


def _find_fin_row(df, labels):
    """Return the first matching row series from a financials DataFrame, or None."""
    for lbl in labels:
        if lbl in df.index:
            return df.loc[lbl]
    return None


def build_equity_history(asset):
    """
    Fetch quarterly income statement + 4y daily price history for one equity.
    Returns (quarterly_rows, daily_rows).
    """
    import yfinance as yf
    import pandas as pd

    ticker  = asset["ticker"]
    company = asset["company"]

    log.info("  [Equity] %s (%s)", company, ticker)

    try:
        tk = yf.Ticker(ticker)
    except Exception as e:
        log.error("  %s: failed to create Ticker: %s", ticker, e)
        return [], []

    # --- Shares outstanding ---
    shares_out = None
    try:
        info = tk.info
        shares_out = safe_float(info.get("sharesOutstanding"))
    except Exception as e:
        log.warning("  %s: tk.info error: %s", ticker, e)

    if shares_out is None:
        log.warning("  %s: sharesOutstanding unavailable — mcap will be None", ticker)

    # --- Daily price history (4 years) ---
    hist = None
    try:
        hist = tk.history(period="4y")
        if hist is None or hist.empty:
            log.warning("  %s: empty price history", ticker)
            hist = None
        else:
            # Normalise index to tz-naive UTC dates
            if hist.index.tz is not None:
                hist.index = hist.index.tz_convert(None)
            log.info("  %s: %d days of price history", ticker, len(hist))
    except Exception as e:
        log.warning("  %s: price history error: %s", ticker, e)

    # Build date → close-price lookup (string keys for safe cross-format joins)
    price_by_date = {}
    if hist is not None:
        for dt, row in hist.iterrows():
            c = safe_float(row.get("Close"))
            if c is not None:
                price_by_date[dt.strftime("%Y-%m-%d")] = c

    def get_price_on_or_before(date_str, max_lookback=7):
        """Return close price on date_str or nearest prior trading day."""
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        for delta in range(max_lookback + 1):
            d = (target - timedelta(days=delta)).strftime("%Y-%m-%d")
            p = price_by_date.get(d)
            if p is not None:
                return p
        return None

    # --- Dividends ---
    divs = None
    try:
        divs = tk.dividends
        if divs is None or divs.empty:
            divs = None
        else:
            if divs.index.tz is not None:
                divs.index = divs.index.tz_convert(None)
    except Exception as e:
        log.warning("  %s: dividends error: %s", ticker, e)

    def trailing_div_sum(as_of_date):
        """Sum dividends paid in the 365 days up to and including as_of_date."""
        if divs is None:
            return 0.0
        cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=365)
        end    = pd.Timestamp(as_of_date)
        mask   = (divs.index >= cutoff) & (divs.index <= end)
        return float(divs[mask].sum())

    # --- Quarterly financials ---
    fins = None
    for attr in ("quarterly_income_stmt", "quarterly_financials"):
        try:
            candidate = getattr(tk, attr, None)
            if candidate is not None and not candidate.empty:
                fins = candidate
                log.info("  %s: quarterly financials via tk.%s  (%d quarters)",
                         ticker, attr, len(fins.columns))
                break
        except Exception:
            continue

    if fins is None:
        log.warning("  %s: no quarterly financials — skipping quarterly rows", ticker)

    # --- Build quarterly rows ---
    quarterly_rows = []
    if fins is not None:
        rev_series = _find_fin_row(fins, _REV_LABELS)
        inc_series = _find_fin_row(fins, _INC_LABELS)

        if rev_series is None:
            log.warning("  %s: 'Total Revenue' row not found in financials", ticker)
        if inc_series is None:
            log.warning("  %s: 'Net Income' row not found in financials", ticker)

        # Most-recent 16 quarters; columns are sorted most-recent-first by yfinance
        quarters = list(fins.columns)[:16]

        for q_col in quarters:
            q_str = (q_col.strftime("%Y-%m-%d")
                     if hasattr(q_col, "strftime") else str(q_col)[:10])

            q_rev  = safe_float(rev_series[q_col]) if rev_series is not None else None
            q_earn = safe_float(inc_series[q_col]) if inc_series is not None else None

            # Annualise: quarter × 4
            ann_rev  = q_rev  * 4 if q_rev  is not None else None
            ann_earn = q_earn * 4 if q_earn is not None else None

            # Market cap on quarter-end date
            q_close = get_price_on_or_before(q_str)
            q_mcap  = q_close * shares_out if (q_close and shares_out) else None

            pe_ratio = (safe_div(q_mcap, ann_earn)
                        if (ann_earn and ann_earn > 0) else None)
            ps_ratio = safe_div(q_mcap, ann_rev)

            tdiv = trailing_div_sum(q_str)
            div_yield = safe_div(tdiv, q_close) * 100 if (tdiv and q_close) else None

            quarterly_rows.append({
                "date":               q_str,
                "ticker":             ticker,
                "company":            company,
                "quarterly_revenue":  q_rev,
                "quarterly_earnings": q_earn,
                "ann_revenue":        ann_rev,
                "ann_earnings":       ann_earn,
                "mcap":               q_mcap,
                "pe_ratio":           pe_ratio,
                "ps_ratio":           ps_ratio,
                "div_yield":          div_yield,
            })

        quarterly_rows.sort(key=lambda r: r["date"])
        log.info("  %s: %d quarterly rows", ticker, len(quarterly_rows))

    # --- Annual financials (extend P/E + Revenue history beyond recent quarters) ---
    # yfinance returns ~4 fiscal years of annual data; we prepend any annual rows
    # whose date falls before the earliest quarterly row to fill the gap.
    annual_fins = None
    for attr in ("income_stmt", "annual_income_stmt", "financials"):
        try:
            candidate = getattr(tk, attr, None)
            if candidate is not None and not candidate.empty:
                annual_fins = candidate
                log.info("  %s: annual financials via tk.%s  (%d years)",
                         ticker, attr, len(annual_fins.columns))
                break
        except Exception:
            continue

    if annual_fins is not None:
        a_rev_series = _find_fin_row(annual_fins, _REV_LABELS)
        a_inc_series = _find_fin_row(annual_fins, _INC_LABELS)
        earliest_q   = quarterly_rows[0]["date"] if quarterly_rows else None

        annual_rows = []
        for a_col in list(annual_fins.columns)[:8]:   # up to 8 fiscal years
            a_str = (a_col.strftime("%Y-%m-%d")
                     if hasattr(a_col, "strftime") else str(a_col)[:10])

            # Only add annual rows that pre-date the first quarterly report
            if earliest_q and a_str >= earliest_q:
                continue

            a_rev  = safe_float(a_rev_series[a_col]) if a_rev_series is not None else None
            a_earn = safe_float(a_inc_series[a_col]) if a_inc_series is not None else None
            # Annual figures are already full-year totals — no ×4 needed

            a_close = get_price_on_or_before(a_str)
            a_mcap  = a_close * shares_out if (a_close and shares_out) else None

            pe_ratio  = (safe_div(a_mcap, a_earn)
                         if (a_earn and a_earn > 0) else None)
            ps_ratio  = safe_div(a_mcap, a_rev)
            tdiv      = trailing_div_sum(a_str)
            div_yield = safe_div(tdiv, a_close) * 100 if (tdiv and a_close) else None

            annual_rows.append({
                "date":               a_str,
                "ticker":             ticker,
                "company":            company,
                "quarterly_revenue":  None,   # annual period, not a single quarter
                "quarterly_earnings": None,
                "ann_revenue":        a_rev,
                "ann_earnings":       a_earn,
                "mcap":               a_mcap,
                "pe_ratio":           pe_ratio,
                "ps_ratio":           ps_ratio,
                "div_yield":          div_yield,
            })

        if annual_rows:
            annual_rows.sort(key=lambda r: r["date"])
            quarterly_rows = annual_rows + quarterly_rows
            log.info("  %s: prepended %d annual rows → %d total financial rows",
                     ticker, len(annual_rows), len(quarterly_rows))

    # --- Build daily rows ---
    daily_rows = []
    if hist is not None:
        for dt, row in hist.iterrows():
            close = safe_float(row.get("Close"))
            if close is None:
                continue
            mcap = close * shares_out if shares_out else None

            tdiv = trailing_div_sum(dt.date())
            div_yield_trailing = (safe_div(tdiv, close) * 100
                                  if (tdiv and close) else None)

            daily_rows.append({
                "date":               dt.strftime("%Y-%m-%d"),
                "ticker":             ticker,
                "mcap":               mcap,
                "close_price":        close,
                "div_yield_trailing": div_yield_trailing,
            })
        log.info("  %s: %d daily rows", ticker, len(daily_rows))

    return quarterly_rows, daily_rows


# ---------------------------------------------------------------------------
# Summary printing
# ---------------------------------------------------------------------------

def _fm(val):
    if val is None: return "—"
    if abs(val) >= 1e9: return f"${val/1e9:.2f}B"
    if abs(val) >= 1e6: return f"${val/1e6:.1f}M"
    return f"${val:,.0f}"

def _fx(val):
    return "—" if val is None else f"{val:.1f}x"

def _fp(val):
    return "—" if val is None else f"{val:.2f}%"


def print_defi_summary(summaries):
    """
    summaries: list of dicts built incrementally during the fetch run:
      {name, dl_slug, n_rows, first_date, last_date,
       ann_revenue, mcap, pe_ratio, ps_ratio, holder_yield}
    """
    print()
    print("=" * 68)
    print(f"  DeFi Historical Summary  ·  {DATE_ISO}  ({len(summaries)} assets)")
    print("=" * 68)
    for s in summaries:
        print(f"\n  {s['name']}  ({s['dl_slug']})")
        print(f"    Date range  : {s['first_date']} → {s['last_date']}")
        print(f"    Data points : {s['n_rows']}")
        print(f"    Ann revenue : {_fm(s.get('ann_revenue'))}")
        print(f"    MCap        : {_fm(s.get('mcap'))}")
        print(f"    P/E         : {_fx(s.get('pe_ratio'))}")
        print(f"    P/S         : {_fx(s.get('ps_ratio'))}")
        print(f"    Holder yield: {_fp(s.get('holder_yield'))}")
    print()


def print_equity_summary(summaries):
    """
    summaries: list of dicts built incrementally during the fetch run:
      {ticker, company, n_quarterly, n_daily,
       ann_revenue, ann_earnings, mcap, pe_ratio, ps_ratio, div_yield}
    """
    print()
    print("=" * 68)
    print(f"  Equity Historical Summary  ·  {DATE_ISO}  ({len(summaries)} assets)")
    print("=" * 68)
    for s in summaries:
        print(f"\n  {s['ticker']}  ({s['company']})")
        print(f"    Quarterly   : {s.get('q_first','—')} → {s.get('q_last','—')}"
              f"  ({s['n_quarterly']} quarters)")
        print(f"    Daily       : {s.get('d_first','—')} → {s.get('d_last','—')}"
              f"  ({s['n_daily']} days)")
        print(f"    Ann revenue : {_fm(s.get('ann_revenue'))}")
        print(f"    Ann earnings: {_fm(s.get('ann_earnings'))}")
        print(f"    MCap        : {_fm(s.get('mcap'))}")
        print(f"    P/E         : {_fx(s.get('pe_ratio'))}")
        print(f"    P/S         : {_fx(s.get('ps_ratio'))}")
        print(f"    Div yield   : {_fp(s.get('div_yield'))}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    log.info("=" * 60)
    log.info("Historical Data Fetch — %s", DATE_ISO)
    log.info("=" * 60)

    # --- Load asset lists from snapshot TSVs ---
    try:
        defi_assets   = load_defi_assets()
        equity_assets = load_equity_assets()
    except FileNotFoundError as e:
        log.error("Cannot load asset lists: %s", e)
        return 1

    log.info("Assets: %d DeFi  +  %d equity", len(defi_assets), len(equity_assets))

    # --- Open output files and write headers immediately ---
    # Any data written so far is on disk; a crash/timeout only loses the
    # current in-flight asset, not earlier ones.
    defi_fh,  defi_writer  = open_tsv_writer(DEFI_TSV,         DEFI_COLUMNS)
    eq_q_fh,  eq_q_writer  = open_tsv_writer(EQUITY_TSV,       EQUITY_COLUMNS)
    eq_d_fh,  eq_d_writer  = open_tsv_writer(EQUITY_DAILY_TSV, EQUITY_DAILY_COLUMNS)

    defi_summaries   = []
    equity_summaries = []
    defi_ok = defi_skip = 0
    eq_ok   = eq_skip   = 0

    try:
        # ── Part 1: DeFi ──────────────────────────────────────────────────
        log.info("")
        log.info("PART 1 — DeFi historical  (%d assets)", len(defi_assets))

        for i, asset in enumerate(defi_assets, 1):
            log.info("  [%d/%d] %s", i, len(defi_assets), asset["name"])
            try:
                rows = build_defi_history(asset)
            except Exception as e:
                log.error("  Unhandled error for %s: %s", asset["name"], e)
                defi_skip += 1
                continue

            if not rows:
                log.warning("  %s — no rows returned, skipping", asset["name"])
                defi_skip += 1
                continue

            flush_rows(defi_writer, defi_fh, rows)
            log.info("  %s — saved %d rows to %s",
                     asset["name"], len(rows), DEFI_TSV.name)
            defi_ok += 1

            last = rows[-1]
            defi_summaries.append({
                "name":        asset["name"],
                "dl_slug":     asset["dl_slug"],
                "n_rows":      len(rows),
                "first_date":  rows[0]["date"],
                "last_date":   last["date"],
                "ann_revenue": last.get("ann_revenue_rolling"),
                "mcap":        last.get("mcap"),
                "pe_ratio":    last.get("pe_ratio"),
                "ps_ratio":    last.get("ps_ratio"),
                "holder_yield":last.get("holder_yield"),
            })

        log.info("DeFi complete: %d saved, %d skipped / %d attempted",
                 defi_ok, defi_skip, len(defi_assets))

        # ── Part 2: Equity ────────────────────────────────────────────────
        log.info("")
        log.info("PART 2 — Equity historical  (%d assets)", len(equity_assets))

        for i, asset in enumerate(equity_assets, 1):
            log.info("  [%d/%d] %s (%s)",
                     i, len(equity_assets), asset["company"], asset["ticker"])
            try:
                q_rows, d_rows = build_equity_history(asset)
            except Exception as e:
                log.error("  Unhandled error for %s: %s", asset["ticker"], e)
                eq_skip += 1
                continue

            if q_rows:
                flush_rows(eq_q_writer, eq_q_fh, q_rows)
            if d_rows:
                flush_rows(eq_d_writer, eq_d_fh, d_rows)

            if not q_rows and not d_rows:
                log.warning("  %s — no rows returned, skipping", asset["ticker"])
                eq_skip += 1
                continue

            log.info("  %s — saved %d quarterly + %d daily rows",
                     asset["ticker"], len(q_rows), len(d_rows))
            eq_ok += 1

            lq = q_rows[-1] if q_rows else {}
            ld = d_rows[-1] if d_rows else {}
            equity_summaries.append({
                "ticker":      asset["ticker"],
                "company":     asset["company"],
                "n_quarterly": len(q_rows),
                "n_daily":     len(d_rows),
                "q_first":     q_rows[0]["date"]  if q_rows else None,
                "q_last":      q_rows[-1]["date"] if q_rows else None,
                "d_first":     d_rows[0]["date"]  if d_rows else None,
                "d_last":      d_rows[-1]["date"] if d_rows else None,
                "ann_revenue": lq.get("ann_revenue"),
                "ann_earnings":lq.get("ann_earnings"),
                "mcap":        lq.get("mcap"),
                "pe_ratio":    lq.get("pe_ratio"),
                "ps_ratio":    lq.get("ps_ratio"),
                "div_yield":   lq.get("div_yield"),
            })

        log.info("Equity complete: %d saved, %d skipped / %d attempted",
                 eq_ok, eq_skip, len(equity_assets))

    finally:
        # Always close output files — partial data is valid and usable
        defi_fh.close()
        eq_q_fh.close()
        eq_d_fh.close()
        log.info("Output files closed.")

    # --- Summaries ---
    if defi_summaries:
        print_defi_summary(defi_summaries)
    if equity_summaries:
        print_equity_summary(equity_summaries)

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
