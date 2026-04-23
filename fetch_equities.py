#!/usr/bin/env python3
"""
fetch_equities.py
Pull fundamental valuation metrics for 25 tech/fintech equities.
Primary source: yfinance. Fallback: FMP (demo key).
Output: output/YYMMDD_equities.tsv

Notes:
- yfinance 1.2.0 returns dividendYield already in percentage form
  (e.g. 0.98 = 0.98%), not as a decimal (0.0098). Stored as-is in TSV.
- grossMargins / operatingMargins are returned as decimals (0.686 = 68.6%).
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
from datetime import date
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

DATE_STR = date.today().strftime("%y%m%d")
DATE_ISO = date.today().isoformat()
TSV_FILE = OUTPUT_DIR / f"{DATE_STR}_equities.tsv"
LOG_FILE = LOG_DIR    / f"{DATE_STR}_equities.log"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TICKERS = [
    # Platform / Infrastructure
    "MSFT", "GOOGL", "ORCL", "IBM",
    # Consumer / Social
    "AAPL", "META", "SNAP",
    # Semiconductors
    "NVDA", "AMD", "INTC", "QCOM", "AVGO",
    # SaaS / Enterprise
    "CRM", "ADBE", "NOW", "INTU", "WDAY",
    # Fintech / Payments
    "PYPL", "SQ", "V", "MA", "COIN", "HOOD",
    # Exchanges / Market Infrastructure
    "CME", "ICE",
]

FMP_BASE = "https://financialmodelingprep.com/api/v3"
SLEEP_S       = 1.0    # between tickers
RETRY_MAX     = 3      # attempts per ticker
RETRY_DELAY_S = 45     # seconds to wait before retry after timeout
COOLDOWN_S    = 90     # extra sleep after consecutive failures

TSV_COLUMNS = [
    "date", "ticker", "company", "sector", "industry",
    "market_cap", "total_revenue_ttm", "net_income_ttm",
    "trailing_eps", "shares_outstanding", "current_price",
    "pe_ratio", "ps_ratio", "earnings_yield", "dividend_yield_pct",
    "gross_margins", "operating_margins",
    "revenue_momentum", "supply_inflation", "real_yield",
    "data_source", "notes",
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
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def safe_float(val):
    """Convert to Python float; return None if NaN, None, or unconvertible."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


_REV_LABELS    = ["Total Revenue", "TotalRevenue", "Revenue"]
_SHARES_LABELS = ["Ordinary Shares Number", "Share Issued"]


def _compute_rev_momentum(tk_obj, ticker):
    """
    Revenue momentum = (most recent quarter revenue / prior quarter) - 1.
    Returns decimal (e.g. 0.12 = 12% QoQ acceleration). None if unavailable.
    """
    for attr in ("quarterly_income_stmt", "quarterly_financials"):
        try:
            fins = getattr(tk_obj, attr, None)
            if fins is None or fins.empty:
                continue
            for lbl in _REV_LABELS:
                if lbl in fins.index:
                    row = fins.loc[lbl]
                    if len(row) >= 2:
                        q0 = safe_float(row.iloc[0])
                        q1 = safe_float(row.iloc[1])
                        if q0 is not None and q1 and q1 > 0:
                            return q0 / q1 - 1
                    break
        except Exception as e:
            log.debug("  %s: quarterly revenue error: %s", ticker, e)
    return None


def _compute_supply_inflation(tk_obj, shares_now, ticker):
    """
    Supply inflation = (shares_now / shares_4q_ago - 1) × 100.
    Negative when the company is buying back shares.
    Returns percent (e.g. -2.5 = 2.5% buyback). None if unavailable.
    """
    shares_now = safe_float(shares_now)
    if not shares_now or shares_now <= 0:
        return None
    try:
        bs = getattr(tk_obj, "quarterly_balance_sheet", None)
        if bs is None or bs.empty:
            return None
        for lbl in _SHARES_LABELS:
            if lbl in bs.index:
                row = bs.loc[lbl]
                if len(row) >= 4:
                    shares_1y = safe_float(row.iloc[3])
                    if shares_1y and shares_1y > 0:
                        return (shares_now / shares_1y - 1) * 100
                break
    except Exception as e:
        log.debug("  %s: quarterly balance sheet error: %s", ticker, e)
    return None


def fetch_json(url, label=""):
    req = urllib.request.Request(url, headers={"User-Agent": "equities-screener/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.debug("HTTP %d fetching %s", e.code, label or url)
        return None
    except Exception as e:
        log.debug("Error fetching %s: %s", label or url, e)
        return None

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_yfinance(ticker):
    """
    Fetch via yfinance with retry on timeout. Returns dict of extracted fields, or {} on failure.
    """
    import yfinance as yf

    last_err = None
    info     = None

    try:
        tk_obj = yf.Ticker(ticker)
    except Exception as e:
        log.warning("  yfinance: failed to init Ticker for %s: %s", ticker, e)
        return {}

    for attempt in range(1, RETRY_MAX + 1):
        try:
            info = tk_obj.info
            if info and info.get("quoteType") != "NONE":
                break
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_timeout = "timed out" in err_str or "timeout" in err_str or "curl: (28)" in err_str
            if attempt < RETRY_MAX and is_timeout:
                log.warning("  timeout on %s (attempt %d/%d), waiting %ds…",
                            ticker, attempt, RETRY_MAX, RETRY_DELAY_S)
                time.sleep(RETRY_DELAY_S)
                continue
            log.warning("  yfinance error for %s: %s", ticker, e)
            return {}
    else:
        # All retries exhausted
        log.warning("  yfinance: %s failed after %d attempts: %s", ticker, RETRY_MAX, last_err)
        return {}

    if not info or info.get("quoteType") == "NONE":
        return {}

    trailing_pe    = info.get("trailingPE")
    price_to_sales = info.get("priceToSalesTrailing12Months")
    dividend_yield = info.get("dividendYield")   # already in % form (e.g. 0.98 = 0.98%)
    trailing_eps   = info.get("trailingEps")

    ey_raw = safe_div(1.0, trailing_pe)
    earnings_yield = ey_raw * 100 if ey_raw is not None else None

    return {
        "company":            info.get("longName") or info.get("shortName"),
        "sector":             info.get("sector"),
        "industry":           info.get("industry"),
        "market_cap":         info.get("marketCap"),
        "total_revenue_ttm":  info.get("totalRevenue"),
        "net_income_ttm":     info.get("netIncomeToCommon"),
        "trailing_eps":       trailing_eps,
        "shares_outstanding": info.get("sharesOutstanding"),
        "current_price":      info.get("currentPrice") or info.get("regularMarketPrice"),
        "pe_ratio":           trailing_pe,
        "ps_ratio":           price_to_sales,
        "earnings_yield":     earnings_yield,
        "dividend_yield_pct": dividend_yield,    # % form, e.g. 0.98 for 0.98%
        "gross_margins":      info.get("grossMargins"),
        "operating_margins":  info.get("operatingMargins"),
        "revenue_momentum":   _compute_rev_momentum(tk_obj, ticker),
        "supply_inflation":   _compute_supply_inflation(tk_obj, info.get("sharesOutstanding"), ticker),
    }


def fetch_fmp(ticker):
    """
    FMP fallback — tries /profile then /ratios-ttm.
    Returns dict of any fields that FMP has and yfinance missed.
    Returns {} if FMP is unavailable (demo key is restricted).
    """
    result = {}

    # Profile endpoint
    profile_data = fetch_json(
        f"{FMP_BASE}/profile/{ticker}?apikey=demo",
        f"FMP profile/{ticker}",
    )
    if profile_data and isinstance(profile_data, list) and profile_data:
        p = profile_data[0]
        if p.get("pe"):
            result["pe_ratio"] = float(p["pe"])
        if p.get("eps"):
            result["trailing_eps"] = float(p["eps"])
        # FMP dividendYield is in decimal form (0.0082) — convert to %
        if p.get("lastDiv") and p.get("price"):
            div_pct = safe_div(float(p["lastDiv"]), float(p["price"]))
            if div_pct is not None:
                result["dividend_yield_pct"] = div_pct * 100

    # Ratios endpoint for P/S if still missing
    if "ps_ratio" not in result:
        ratios_data = fetch_json(
            f"{FMP_BASE}/ratios-ttm/{ticker}?apikey=demo",
            f"FMP ratios/{ticker}",
        )
        if ratios_data and isinstance(ratios_data, list) and ratios_data:
            r = ratios_data[0]
            if r.get("priceToSalesRatioTTM"):
                result["ps_ratio"] = float(r["priceToSalesRatioTTM"])

    return result


def fetch_ticker(ticker):
    """
    Fetch all data for a ticker. yfinance primary, FMP fallback for any None fields.
    Returns a row dict ready for TSV.
    """
    yf_data  = fetch_yfinance(ticker)
    notes    = []
    sources  = []

    if yf_data:
        sources.append("yfinance")

    # Identify fields that need FMP fallback
    key_fields = ["pe_ratio", "ps_ratio", "trailing_eps", "dividend_yield_pct"]
    needs_fmp  = any(yf_data.get(f) is None for f in key_fields) if yf_data else True

    fmp_data = {}
    if needs_fmp:
        fmp_data = fetch_fmp(ticker)
        if fmp_data:
            sources.append("fmp")

    # Merge: yfinance wins, FMP fills gaps
    merged = {**yf_data}
    fmp_filled = []
    for field, val in fmp_data.items():
        if merged.get(field) is None and val is not None:
            merged[field] = val
            fmp_filled.append(field)

    if fmp_filled:
        notes.append(f"fmp_filled: {','.join(fmp_filled)}")

    # Recalculate earnings_yield if pe_ratio was filled by FMP
    if "earnings_yield" not in merged or merged.get("earnings_yield") is None:
        pe = merged.get("pe_ratio")
        ey_raw = safe_div(1.0, pe)
        merged["earnings_yield"] = ey_raw * 100 if ey_raw is not None else None

    # Real yield: earnings_yield minus supply inflation (both in %)
    ey = merged.get("earnings_yield")
    si = merged.get("supply_inflation")
    real_yield = ey - si if (ey is not None and si is not None) else None

    # Flag fully-unavailable tickers
    all_none = all(merged.get(f) is None for f in key_fields)
    if all_none:
        notes.append("all_key_fields_unavailable")

    # Determine data_source summary
    if not sources:
        data_source = "unavailable"
    elif len(sources) == 1:
        data_source = sources[0]
    else:
        data_source = "+".join(sources)

    return {
        "date":               DATE_ISO,
        "ticker":             ticker,
        "company":            merged.get("company"),
        "sector":             merged.get("sector"),
        "industry":           merged.get("industry"),
        "market_cap":         merged.get("market_cap"),
        "total_revenue_ttm":  merged.get("total_revenue_ttm"),
        "net_income_ttm":     merged.get("net_income_ttm"),
        "trailing_eps":       merged.get("trailing_eps"),
        "shares_outstanding": merged.get("shares_outstanding"),
        "current_price":      merged.get("current_price"),
        "pe_ratio":           merged.get("pe_ratio"),
        "ps_ratio":           merged.get("ps_ratio"),
        "earnings_yield":     merged.get("earnings_yield"),
        "dividend_yield_pct": merged.get("dividend_yield_pct"),
        "gross_margins":      merged.get("gross_margins"),
        "operating_margins":  merged.get("operating_margins"),
        "revenue_momentum":   merged.get("revenue_momentum"),
        "supply_inflation":   merged.get("supply_inflation"),
        "real_yield":         real_yield,
        "data_source":        data_source,
        "notes":              "; ".join(notes) if notes else "",
    }

# ---------------------------------------------------------------------------
# TSV output
# ---------------------------------------------------------------------------

def fmt_val(val):
    if val is None:
        return ""
    if isinstance(val, float):
        return f"{val:.6f}"
    return str(val)


def write_tsv(rows, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({col: fmt_val(r.get(col)) for col in TSV_COLUMNS})
    log.info("Wrote %d rows → %s", len(rows), path.name)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    log.info("=" * 55)
    log.info("Equity Screener — %s  (%d tickers)", DATE_ISO, len(TICKERS))
    log.info("=" * 55)

    rows             = []
    success          = 0
    failed           = []
    consec_failures  = 0

    for i, ticker in enumerate(TICKERS):
        log.info("[%d/%d] %s…", i + 1, len(TICKERS), ticker)
        try:
            row = fetch_ticker(ticker)
            rows.append(row)
            has_data = row.get("pe_ratio") or row.get("ps_ratio") or row.get("market_cap")
            if has_data:
                success += 1
                consec_failures = 0
                pe  = f"PE={row['pe_ratio']:.1f}" if row.get("pe_ratio") else "PE=—"
                ps  = f"PS={row['ps_ratio']:.1f}" if row.get("ps_ratio") else "PS=—"
                ey  = f"EY={row['earnings_yield']:.1f}%" if row.get("earnings_yield") else "EY=—"
                log.info("  → %s  %s  %s  %s", row.get("company", ticker), pe, ps, ey)
            else:
                failed.append(ticker)
                consec_failures += 1
                log.warning("  → %s: no key fields returned", ticker)
        except Exception as e:
            failed.append(ticker)
            consec_failures += 1
            log.error("  → %s: unhandled error: %s", ticker, e)
            rows.append({
                "date": DATE_ISO, "ticker": ticker,
                "data_source": "unavailable",
                "notes": f"error: {e}",
            })

        if i < len(TICKERS) - 1:
            # If 2+ consecutive failures, take a cooldown before continuing
            if consec_failures >= 2:
                log.info("  Rate limit detected — cooling down %ds…", COOLDOWN_S)
                time.sleep(COOLDOWN_S)
                consec_failures = 0
            else:
                time.sleep(SLEEP_S)

    write_tsv(rows, TSV_FILE)

    log.info("")
    log.info("Done.  %d/%d tickers fetched successfully.", success, len(TICKERS))
    if failed:
        log.warning("Failed / no data: %s", failed)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
