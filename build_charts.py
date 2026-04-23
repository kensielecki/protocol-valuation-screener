#!/usr/bin/env python3
"""
build_charts.py  (v3)
Read historical + snapshot TSVs from output/ and produce a single self-
contained HTML file with three interactive Plotly charts.

Layout:
  Left 75%   — filter bar + three stacked charts (height=700 each)
  Right 25%  — scrollable sidebar legend; clicking toggles traces
               across all three charts simultaneously

All assets treated identically:
  - Line trace if historical data exists
  - Snapshot dot if no historical data
  - One consistent colour per asset (Plotly Alphabet palette, cycling)
  - No special styling for any subset of assets

Filter buttons (above charts):
  Show All | DEX | Lending | Derivatives | Chain | Liquid Staking | Other
           | Technology | Financial Services | Communication Services
"""

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path

import plotly.colors as pc
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ─── Paths ────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
DATE_STR   = date.today().strftime("%y%m%d")
HTML_OUT   = OUTPUT_DIR / f"{DATE_STR}_charts.html"

# ─── Category normalisation ───────────────────────────────────────────────────
DEFI_CAT_NORM = {
    "DEX":           "DEX",
    "Derivatives":   "Derivatives",
    "Lending":       "Lending",
    "Chain":         "Chain",
    "Liquid Staking":"Liquid Staking",
    "Launchpad":     "Other",
    "Yield":         "Other",
    "CDP":           "Other",
    "Oracle":        "Other",
    "Basis Trading": "Other",
}

# DeFi protocol names in fundamentals.tsv → canonical name used in historical TSV.
# Must match DEFI_NAME_ALIASES in fetch_historical.py.
DEFI_NAME_CANON = {"Aave V3": "Aave"}

PE_CAP = 200

# ─── Colour palettes ──────────────────────────────────────────────────────────
# Terminal theme (default): Prism (10 colours)
# Report  theme:            T10   (10 colours)
_PALETTE_TERMINAL = pc.qualitative.Prism
_PALETTE_REPORT   = pc.qualitative.T10


def assign_asset_colours(fundamentals, eq_snap):
    """Return {name/ticker: {terminal: hex, report: hex}} cycling each palette."""
    assets = [r["name"] for r in fundamentals] + [r["ticker"] for r in eq_snap]
    return {
        name: {
            "terminal": _PALETTE_TERMINAL[i % len(_PALETTE_TERMINAL)],
            "report":   _PALETTE_REPORT[i % len(_PALETTE_REPORT)],
        }
        for i, name in enumerate(assets)
    }


def _colour(asset_colours, name):
    """Return the terminal-theme hex colour for name (default for chart traces)."""
    return asset_colours.get(name, {}).get("terminal", "#888888")

# ─── TSV helpers ──────────────────────────────────────────────────────────────
def _float(s):
    if s is None or (isinstance(s, str) and s.strip() == ""):
        return None
    try:
        v = float(s)
        return None if math.isnan(v) else v
    except (ValueError, TypeError):
        return None


def load_tsv(path):
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def find_latest(pattern):
    candidates = sorted(OUTPUT_DIR.glob(pattern), reverse=True)
    if not candidates:
        print(f"ERROR: no file matching {pattern} in {OUTPUT_DIR}", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


# ─── Data loading ─────────────────────────────────────────────────────────────
def load_fundamentals(path):
    rows = load_tsv(path)
    result = []
    for r in rows:
        raw  = r.get("protocol", "")
        name = DEFI_NAME_CANON.get(raw, raw)
        cat  = DEFI_CAT_NORM.get(r.get("category", ""), "Other")
        result.append({
            "name":         name,
            "category":     cat,
            "ann_revenue":       _float(r.get("ann_revenue")),
            "pe_ratio":          _float(r.get("pe_ratio")),
            "holder_yield":      _float(r.get("holder_yield")),
            "earnings_yield":    _float(r.get("earnings_yield")),
            "revenue_momentum":  _float(r.get("revenue_momentum")),
            "real_yield":        _float(r.get("real_yield")),
            "date":              r.get("date", ""),
        })
    return result


def load_equities_snapshot(path):
    rows = load_tsv(path)
    result = []
    for r in rows:
        result.append({
            "ticker":             r.get("ticker", ""),
            "company":            r.get("company", ""),
            "sector":             r.get("sector", "").strip(),
            "total_revenue_ttm":  _float(r.get("total_revenue_ttm")),
            "pe_ratio":           _float(r.get("pe_ratio")),
            "dividend_yield_pct": _float(r.get("dividend_yield_pct")),
            "earnings_yield":     _float(r.get("earnings_yield")),
            "revenue_momentum":   _float(r.get("revenue_momentum")),
            "real_yield":         _float(r.get("real_yield")),
            "date":               r.get("date", ""),
        })
    return result


def load_defi_history(path):
    rows = load_tsv(path)
    out  = {}
    for r in rows:
        name = r.get("asset", "")
        if name not in out:
            out[name] = {"dates": [], "ann_revenue_rolling": [],
                         "pe_ratio": [], "holder_yield": []}
        out[name]["dates"].append(r["date"])
        out[name]["ann_revenue_rolling"].append(_float(r.get("ann_revenue_rolling")))
        out[name]["pe_ratio"].append(_float(r.get("pe_ratio")))
        out[name]["holder_yield"].append(_float(r.get("holder_yield")))
    return out


def load_equity_quarterly(path):
    rows = load_tsv(path)
    out  = {}
    for r in rows:
        t = r.get("ticker", "")
        if t not in out:
            out[t] = []
        out[t].append({
            "date":         r.get("date", ""),
            "ann_revenue":  _float(r.get("ann_revenue")),
            "ann_earnings": _float(r.get("ann_earnings")),
        })
    for t in out:
        out[t].sort(key=lambda x: x["date"])
    return out


def load_equity_daily(path):
    rows = load_tsv(path)
    out  = {}
    for r in rows:
        t = r.get("ticker", "")
        if t not in out:
            out[t] = []
        out[t].append({
            "date":               r.get("date", ""),
            "mcap":               _float(r.get("mcap")),
            "div_yield_trailing": _float(r.get("div_yield_trailing")),
        })
    for t in out:
        out[t].sort(key=lambda x: x["date"])
    return out


# ─── Daily P/E for equities ───────────────────────────────────────────────────
def compute_daily_pe(eq_q_by_ticker, eq_d_by_ticker):
    """
    Forward-fill quarterly ann_earnings to daily; pe = mcap / ann_earnings.
    Returns {ticker: {dates, pe_daily}}.
    """
    result = {}
    for ticker, d_rows in eq_d_by_ticker.items():
        q_rows   = eq_q_by_ticker.get(ticker, [])
        dates    = []
        pe_daily = []
        for dr in d_rows:
            d_date   = dr["date"]
            mcap     = dr["mcap"]
            ann_earn = None
            for qr in q_rows:
                if qr["date"] <= d_date:
                    ann_earn = qr["ann_earnings"]
                else:
                    break
            if ann_earn is not None and ann_earn > 0 and mcap is not None:
                pe = min(mcap / ann_earn, PE_CAP)
            else:
                pe = None
            dates.append(d_date)
            pe_daily.append(pe)
        result[ticker] = {"dates": dates, "pe_daily": pe_daily}
    return result


# ─── Asset metadata (serialised to JS) ───────────────────────────────────────
def build_asset_meta(fundamentals, eq_snap, hist_defi, hist_equity, asset_colours):
    meta = {}
    for row in fundamentals:
        name = row["name"]
        c = asset_colours.get(name, {})
        meta[name] = {
            "type":          "defi",
            "category":      row["category"],
            "hasHistory":    name in hist_defi,
            "colour":        c.get("terminal", "#888888"),
            "colour_report": c.get("report",   "#888888"),
            "momentum":      row.get("revenue_momentum"),
        }
    for row in eq_snap:
        t = row["ticker"]
        c = asset_colours.get(t, {})
        meta[t] = {
            "type":          "equity",
            "sector":        row["sector"],
            "hasHistory":    t in hist_equity,
            "colour":        c.get("terminal", "#888888"),
            "colour_report": c.get("report",   "#888888"),
            "momentum":      row.get("revenue_momentum"),
        }
    return meta


# ─── Layout base ──────────────────────────────────────────────────────────────
_GREEN = "#4ade80"
_RED   = "#f87171"
_GREY  = "#555550"

LAYOUT_BASE = dict(
    paper_bgcolor="#141412",
    plot_bgcolor="#0c0c0a",
    font=dict(family="Inter, Helvetica Neue, Arial, sans-serif",
              size=13, color="#e8e6df"),
    showlegend=False,
    hovermode="x unified",
    height=700,
    margin=dict(l=70, r=90, t=90, b=60),
    xaxis=dict(
        showgrid=True,
        gridcolor="#252520",
        linecolor="#252520",
        tickformat="%b %Y",
    ),
)

YAXIS_BASE = dict(
    showgrid=True,
    gridcolor="#252520",
    linecolor="#252520",
    zeroline=True,
    zerolinecolor="#252520",
)

LAYOUT_BAR = dict(
    paper_bgcolor="#141412",
    plot_bgcolor="#0c0c0a",
    font=dict(family="Inter, Helvetica Neue, Arial, sans-serif",
              size=13, color="#e8e6df"),
    showlegend=False,
    hovermode="closest",
    height=500,
    margin=dict(l=70, r=30, t=90, b=130),
    xaxis=dict(
        showgrid=False,
        linecolor="#252520",
        tickangle=-55,
        tickfont=dict(size=10),
        automargin=True,
    ),
)


def _safe_pe(val):
    if val is None:
        return None
    return min(val, PE_CAP) if val > 0 else None


# ─── Chart 1: Revenue (split subplots) ───────────────────────────────────────
def build_revenue_chart(defi_hist, eq_q, fundamentals, eq_snap,
                        hist_defi, hist_equity, asset_colours):
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.10,
        row_heights=[0.5, 0.5],
        subplot_titles=("DeFi Protocol Revenue", "Equity Revenue (Quarterly)"),
    )

    # ── Top panel: DeFi ──────────────────────────────────────────────────────
    for row in fundamentals:
        name   = row["name"]
        colour = _colour(asset_colours, name)
        if name in hist_defi and name in defi_hist:
            d     = defi_hist[name]
            rev_m = [v / 1e6 if v is not None else None
                     for v in d["ann_revenue_rolling"]]
            fig.add_trace(go.Scatter(
                x=d["dates"], y=rev_m, name=name,
                line=dict(color=colour, width=1.5),
                mode="lines",
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{name}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Ann Rev: $%{{y:.1f}}M<extra></extra>"),
            ), row=1, col=1)
        else:
            rev   = row["ann_revenue"]
            rev_m = rev / 1e6 if rev is not None else None
            fig.add_trace(go.Scatter(
                x=[row["date"]], y=[rev_m], name=name,
                mode="markers",
                marker=dict(color=colour, size=8, symbol="circle",
                            line=dict(color="white", width=0.5)),
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{name}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Ann Rev (snapshot): $%{{y:.1f}}M<extra></extra>"),
            ), row=1, col=1)

    # ── Bottom panel: Equities ────────────────────────────────────────────────
    for row in eq_snap:
        t      = row["ticker"]
        colour = _colour(asset_colours, t)
        if t in hist_equity and t in eq_q:
            rows  = eq_q[t]
            dates = [r["date"] for r in rows]
            rev_b = [r["ann_revenue"] / 1e9 if r["ann_revenue"] is not None else None
                     for r in rows]
            fig.add_trace(go.Scatter(
                x=dates, y=rev_b, name=t,
                line=dict(color=colour, width=1.5, shape="hv"),
                mode="lines",
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{t}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Ann Rev (TTM×4): $%{{y:.2f}}B<extra></extra>"),
            ), row=2, col=1)
        else:
            rev   = row["total_revenue_ttm"]
            rev_b = rev / 1e9 if rev is not None else None
            fig.add_trace(go.Scatter(
                x=[row["date"]], y=[rev_b], name=t,
                mode="markers",
                marker=dict(color=colour, size=8, symbol="diamond",
                            line=dict(color="white", width=0.5)),
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{t}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"TTM Rev (snapshot): $%{{y:.2f}}B<extra></extra>"),
            ), row=2, col=1)

    # ── Layout ────────────────────────────────────────────────────────────────
    fig.update_layout(
        paper_bgcolor="#141412",
        plot_bgcolor="#0c0c0a",
        font=dict(family="Inter, Helvetica Neue, Arial, sans-serif",
                  size=13, color="#e8e6df"),
        showlegend=False,
        hovermode="x unified",
        height=900,
        margin=dict(l=70, r=30, t=90, b=60),
        title=dict(
            text=("<b>Revenue Over Time</b><br>"
                  "<span style='font-size:11px;color:#94a3b8'>"
                  "Circles = DeFi snapshot  ·  Diamonds = Equity snapshot"
                  "</span>"),
            x=0, xanchor="left", font=dict(size=16),
        ),
    )
    fig.update_xaxes(
        showgrid=True, gridcolor="#252520", linecolor="#252520",
        tickformat="%b %Y",
    )
    fig.update_yaxes(
        showgrid=True, gridcolor="#252520", linecolor="#252520",
        zeroline=True, zerolinecolor="#252520",
    )
    fig.update_yaxes(
        title_text="Protocol Revenue ($M)", tickprefix="$", ticksuffix="M",
        row=1, col=1,
    )
    fig.update_yaxes(
        title_text="Total Revenue TTM ($B)", tickprefix="$", ticksuffix="B",
        row=2, col=1,
    )
    # Set subplot title annotation colours to match terminal theme
    fig.for_each_annotation(lambda a: a.update(font_color="#e8e6df", font_size=12))
    return fig


# ─── Chart 2: P/E ─────────────────────────────────────────────────────────────
def build_pe_chart(defi_hist, eq_pe_daily, fundamentals, eq_snap,
                   hist_defi, hist_equity, asset_colours):
    fig = go.Figure()

    # DeFi
    for row in fundamentals:
        name   = row["name"]
        colour = _colour(asset_colours, name)
        if name in hist_defi and name in defi_hist:
            d  = defi_hist[name]
            pe = [_safe_pe(v) for v in d["pe_ratio"]]
            fig.add_trace(go.Scatter(
                x=d["dates"], y=pe, name=name,
                line=dict(color=colour, width=1.5),
                mode="lines", connectgaps=False,
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{name}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"P/E: %{{y:.1f}}x<extra></extra>"),
            ))
        else:
            fig.add_trace(go.Scatter(
                x=[row["date"]], y=[_safe_pe(row["pe_ratio"])], name=name,
                mode="markers",
                marker=dict(color=colour, size=8, symbol="circle",
                            line=dict(color="white", width=0.5)),
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{name}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"P/E (snapshot): %{{y:.1f}}x<extra></extra>"),
            ))

    # Equities
    for row in eq_snap:
        t      = row["ticker"]
        colour = _colour(asset_colours, t)
        if t in hist_equity and t in eq_pe_daily:
            d = eq_pe_daily[t]
            fig.add_trace(go.Scatter(
                x=d["dates"], y=d["pe_daily"], name=t,
                line=dict(color=colour, width=1.5),
                mode="lines", connectgaps=False,
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{t}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"P/E: %{{y:.1f}}x<extra></extra>"),
            ))
        else:
            fig.add_trace(go.Scatter(
                x=[row["date"]], y=[_safe_pe(row["pe_ratio"])], name=t,
                mode="markers",
                marker=dict(color=colour, size=8, symbol="diamond",
                            line=dict(color="white", width=0.5)),
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{t}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"P/E (snapshot): %{{y:.1f}}x<extra></extra>"),
            ))

    fig.update_layout(
        **LAYOUT_BASE,
        yaxis=dict(**YAXIS_BASE, title="P/E Ratio", ticksuffix="x",
                   range=[0, PE_CAP]),
        title=dict(
            text=("<b>P/E Ratio Over Time (capped at 200×)</b><br>"
                  "<span style='font-size:11px;color:#94a3b8'>"
                  "DeFi: MCap ÷ Ann. Protocol Revenue (daily)  ·  "
                  "Equity: MCap ÷ Net Income (daily, earnings forward-filled)  ·  "
                  "Gaps = negative or unavailable earnings"
                  "</span>"),
            x=0, xanchor="left", font=dict(size=16),
        ),
    )
    fig.update_layout(margin=dict(l=70, r=130, t=90, b=60))
    fig.add_hline(
        y=21,
        line_dash="dash", line_color="#6b6860", line_width=1,
        annotation_text="S&P 500 avg ~21x",
        annotation_position="right",
        annotation_font_color="#6b6860",
        annotation_font_size=11,
    )
    return fig


# ─── Chart 3: Yield ───────────────────────────────────────────────────────────
def build_yield_chart(defi_hist, eq_d, fundamentals, eq_snap,
                      hist_defi, hist_equity, asset_colours):
    fig = go.Figure()

    # DeFi
    for row in fundamentals:
        name   = row["name"]
        colour = _colour(asset_colours, name)
        if name in hist_defi and name in defi_hist:
            d = defi_hist[name]
            fig.add_trace(go.Scatter(
                x=d["dates"], y=d["holder_yield"], name=name,
                line=dict(color=colour, width=1.5),
                mode="lines", connectgaps=False,
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{name}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Holder Yield: %{{y:.2f}}%<extra></extra>"),
            ))
        else:
            fig.add_trace(go.Scatter(
                x=[row["date"]], y=[row["holder_yield"]], name=name,
                mode="markers",
                marker=dict(color=colour, size=8, symbol="circle",
                            line=dict(color="white", width=0.5)),
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{name}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Holder Yield (snapshot): %{{y:.2f}}%<extra></extra>"),
            ))

    # Equities
    for row in eq_snap:
        t      = row["ticker"]
        colour = _colour(asset_colours, t)
        if t in hist_equity and t in eq_d:
            rows   = eq_d[t]
            dates  = [r["date"]               for r in rows]
            yields = [r["div_yield_trailing"]  for r in rows]
            fig.add_trace(go.Scatter(
                x=dates, y=yields, name=t,
                line=dict(color=colour, width=1.5),
                mode="lines", connectgaps=False,
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{t}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Div Yield: %{{y:.3f}}%<extra></extra>"),
            ))
        else:
            fig.add_trace(go.Scatter(
                x=[row["date"]], y=[row["dividend_yield_pct"]], name=t,
                mode="markers",
                marker=dict(color=colour, size=8, symbol="diamond",
                            line=dict(color="white", width=0.5)),
                showlegend=False, visible=True,
                hovertemplate=(f"<b>{t}</b><br>%{{x|%b %d, %Y}}<br>"
                               f"Div Yield (snapshot): %{{y:.3f}}%<extra></extra>"),
            ))

    fig.update_layout(
        **LAYOUT_BASE,
        yaxis=dict(**YAXIS_BASE, title="Yield (%)", ticksuffix="%"),
        title=dict(
            text=("<b>Div / Holder Yield Over Time</b><br>"
                  "<span style='font-size:11px;color:#94a3b8'>"
                  "DeFi: Ann. Holder Revenue ÷ MCap  ·  "
                  "Equity: Trailing 12m dividends ÷ Price  ·  "
                  "COIN / growth equities pay no dividend"
                  "</span>"),
            x=0, xanchor="left", font=dict(size=16),
        ),
    )
    fig.update_layout(margin=dict(l=70, r=130, t=90, b=60))
    fig.add_hline(
        y=4.3,
        line_dash="dash", line_color="#6b6860", line_width=1,
        annotation_text="US 10yr ~4.3%",
        annotation_position="right",
        annotation_font_color="#6b6860",
        annotation_font_size=11,
    )
    return fig


# ─── Chart 4: Revenue Momentum ────────────────────────────────────────────────
def build_momentum_chart(fundamentals, eq_snap):
    items = []
    for row in fundamentals:
        m = row.get("revenue_momentum")
        items.append((row["name"], m * 100 if m is not None else None, "defi"))
    for row in eq_snap:
        m = row.get("revenue_momentum")
        items.append((row["ticker"], m * 100 if m is not None else None, "equity"))

    items.sort(key=lambda x: (x[1] is None, -(x[1] or 0)))

    fig = go.Figure()
    for name, mom_pct, _ in items:
        if mom_pct is None:
            color = _GREY
            y_val = 0.0
            htmpl = f"<b>{name}</b><br>Revenue Momentum: N/A<extra></extra>"
        elif mom_pct > 0:
            color = _GREEN
            y_val = mom_pct
            htmpl = f"<b>{name}</b><br>Revenue Momentum: +{mom_pct:.1f}%<extra></extra>"
        else:
            color = _RED
            y_val = mom_pct
            htmpl = f"<b>{name}</b><br>Revenue Momentum: {mom_pct:.1f}%<extra></extra>"

        fig.add_trace(go.Bar(
            x=[name], y=[y_val], name=name,
            marker_color=color,
            showlegend=False, visible=True,
            hovertemplate=htmpl,
        ))

    fig.update_layout(
        **LAYOUT_BAR,
        yaxis=dict(**YAXIS_BASE, title="Revenue Momentum (%)", ticksuffix="%"),
        title=dict(
            text=("<b>Revenue Momentum</b><br>"
                  "<span style='font-size:11px;color:#94a3b8'>"
                  "DeFi: (7d daily avg ÷ 30d daily avg) − 1  ·  "
                  "Equity: (most recent quarter ÷ prior quarter) − 1  ·  "
                  "Grey bar = data unavailable  ·  Sorted descending"
                  "</span>"),
            x=0, xanchor="left", font=dict(size=16),
        ),
    )
    return fig


# ─── Chart 5: Real Yield vs Earnings Yield ────────────────────────────────────
def build_real_yield_chart(fundamentals, eq_snap):
    items = []
    for row in fundamentals:
        ey = row.get("earnings_yield")
        if ey is not None:
            items.append((row["name"], ey, row.get("real_yield"), "defi"))
    for row in eq_snap:
        ey = row.get("earnings_yield")
        if ey is not None:
            items.append((row["ticker"], ey, row.get("real_yield"), "equity"))

    items.sort(key=lambda x: (x[2] is None, -(x[2] or 0)))

    fig = go.Figure()
    for name, ey, ry, _ in items:
        ry_color = (_GREEN if (ry or 0) > 0 else _RED) if ry is not None else _GREY
        ry_val   = ry if ry is not None else 0.0
        ry_htmpl = (f"<b>{name}</b><br>Real Yield: {ry:.1f}%<extra></extra>"
                    if ry is not None
                    else f"<b>{name}</b><br>Real Yield: N/A<extra></extra>")

        fig.add_trace(go.Bar(
            x=[name], y=[ey], name=f"{name}__ey",
            marker_color="#64748b",
            showlegend=False, visible=True,
            hovertemplate=f"<b>{name}</b><br>Earnings Yield: {ey:.1f}%<extra></extra>",
        ))
        fig.add_trace(go.Bar(
            x=[name], y=[ry_val], name=name,
            marker_color=ry_color,
            showlegend=False, visible=True,
            hovertemplate=ry_htmpl,
        ))

    fig.update_layout(
        **LAYOUT_BAR,
        barmode="group",
        yaxis=dict(**YAXIS_BASE, title="Yield (%)", ticksuffix="%"),
        title=dict(
            text=("<b>Real Yield vs Earnings Yield</b><br>"
                  "<span style='font-size:11px;color:#94a3b8'>"
                  "Grey: Earnings Yield (1 ÷ P/E × 100)  ·  "
                  "Green/Red: Real Yield (Earnings Yield − Supply Inflation)  ·  "
                  "Sorted by Real Yield descending"
                  "</span>"),
            x=0, xanchor="left", font=dict(size=16),
        ),
    )
    return fig


# ─── Sidebar HTML ─────────────────────────────────────────────────────────────
def build_sidebar_html(asset_meta):
    """
    Generate the inner HTML for the legend sidebar.
    Assets grouped as DeFi first, then Equities.
    Each item has a colour swatch, name, and type badge.
    """
    defi_items = [(n, m) for n, m in asset_meta.items() if m["type"] == "defi"]
    eq_items   = [(n, m) for n, m in asset_meta.items() if m["type"] == "equity"]

    def item_html(name, meta):
        colour    = meta["colour"]
        badge_cls = "badge-defi" if meta["type"] == "defi" else "badge-eq"
        badge_txt = "DeFi" if meta["type"] == "defi" else "Eq"
        m = meta.get("momentum")
        if m is not None and m > 0.05:
            mom_html = '<span class="mom-up">▲</span>'
        elif m is not None and m < -0.05:
            mom_html = '<span class="mom-dn">▼</span>'
        else:
            mom_html = '<span class="mom-flat">—</span>'
        return (
            f'<div class="legend-item active" data-name="{name}" '
            f'onclick="toggleAsset(this)">'
            f'<span class="swatch" style="background:{colour}"></span>'
            f'<span class="lname">{name}</span>'
            f'{mom_html}'
            f'<span class="badge {badge_cls}">{badge_txt}</span>'
            f'</div>'
        )

    parts = ['<div class="legend-group-hdr">DeFi Protocols</div>']
    for name, meta in defi_items:
        parts.append(item_html(name, meta))
    parts.append('<div class="legend-group-hdr" style="margin-top:10px">Equities</div>')
    for name, meta in eq_items:
        parts.append(item_html(name, meta))
    return "\n".join(parts)


# ─── HTML / CSS / JS ──────────────────────────────────────────────────────────

PAGE_CSS = """
*, *::before, *::after { box-sizing: border-box; }

/* ── Theme variables (Terminal = default) ── */
:root {
    --bg-page:         #141412;
    --bg-surface:      #1a1a17;
    --bg-surface2:     #252520;
    --border:          #2a2a26;
    --text:            #e8e6df;
    --text-muted:      #6b6b60;
    --btn-bg:          #1e1e1a;
    --btn-border:      #3a3a35;
    --btn-color:       #c8c6bf;
    --btn-hover-bg:    #252520;
    --btn-active-bg:   #e8e6df;
    --btn-active-fg:   #0c0c0a;
    --badge-defi-bg:   #1a3324;
    --badge-defi-fg:   #4ade80;
    --badge-eq-bg:     #1a2744;
    --badge-eq-fg:     #60a5fa;
    --section-border:  #2a2a26;
}
body.report {
    --bg-page:         #f8fafc;
    --bg-surface:      #ffffff;
    --bg-surface2:     #f8fafc;
    --border:          #e2e8f0;
    --text:            #0f172a;
    --text-muted:      #64748b;
    --btn-bg:          #ffffff;
    --btn-border:      #cbd5e1;
    --btn-color:       #374151;
    --btn-hover-bg:    #f1f5f9;
    --btn-active-bg:   #0f172a;
    --btn-active-fg:   #ffffff;
    --badge-defi-bg:   #dcfce7;
    --badge-defi-fg:   #166534;
    --badge-eq-bg:     #dbeafe;
    --badge-eq-fg:     #1e40af;
    --section-border:  #e2e8f0;
}

body {
    font-family: Inter, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg-page);
    margin: 0;
    padding: 0;
    color: var(--text);
    transition: background 0.2s, color 0.2s;
}
/* ── Two-column layout ── */
.page-header {
    background: var(--bg-surface);
    border-bottom: 1px solid var(--border);
    padding: 24px 32px 18px;
}
.page-header h1 { margin: 0 0 4px 0; font-size: 20px; font-weight: 700; color: var(--text); }
.page-header p  { margin: 0; font-size: 12px; color: var(--text-muted); }
.page-body {
    display: flex;
    align-items: flex-start;
    min-height: calc(100vh - 80px);
}
/* ── Charts column (left 75%) ── */
.charts-col {
    flex: 0 0 75%;
    min-width: 0;
    padding: 24px 20px 60px 28px;
}
/* ── Filter bar ── */
.filter-bar {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 16px;
    margin-bottom: 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 5px 12px;
    align-items: center;
}
.filter-group { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; }
.filter-label {
    font-size: 10px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.07em;
    white-space: nowrap;
    margin-right: 2px;
}
.filter-sep { width: 1px; height: 16px; background: var(--border); align-self: center; }
.fbtn {
    padding: 3px 9px;
    font-size: 11px;
    font-family: inherit;
    font-weight: 500;
    border: 1px solid var(--btn-border);
    border-radius: 4px;
    background: var(--btn-bg);
    color: var(--btn-color);
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    line-height: 1.5;
}
.fbtn:hover  { background: var(--btn-hover-bg); border-color: var(--text-muted); }
.fbtn.active { background: var(--btn-active-bg); color: var(--btn-active-fg); border-color: var(--btn-active-bg); }
/* Theme toggle button — floated to the right of the filter bar */
.theme-btn {
    margin-left: auto;
    font-size: 10px;
    letter-spacing: 0.04em;
}
/* ── Chart sections ── */
.section { margin-bottom: 40px; }
.section-header {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 6px;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--section-border);
}
.section-header .num {
    font-size: 10px; font-weight: 700; color: var(--text-muted);
    letter-spacing: 0.08em; text-transform: uppercase;
}
.section-header h2 { margin: 0; font-size: 15px; font-weight: 600; color: var(--text); }
.chart-wrap {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
}
/* ── Legend sidebar (right 25%) ── */
.legend-col {
    flex: 0 0 25%;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
    background: var(--bg-surface);
    border-left: 1px solid var(--border);
    padding: 16px 12px 32px 14px;
    display: flex;
    flex-direction: column;
}
.legend-col-hdr {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin-bottom: 10px;
    flex-shrink: 0;
}
.legend-group-hdr {
    font-size: 10px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin: 6px 0 3px 0;
}
.legend-items { flex: 1; overflow-y: auto; }
.legend-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 4px;
    border-radius: 4px;
    cursor: pointer;
    opacity: 0.35;
    transition: opacity 0.12s, background 0.15s;
    user-select: none;
}
.legend-item.active  { opacity: 1; }
.legend-item:hover   { background: var(--bg-surface2); }
.swatch {
    flex-shrink: 0;
    width: 12px;
    height: 12px;
    border-radius: 2px;
    display: inline-block;
    transition: background 0.2s;
}
.lname {
    font-size: 11px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
    min-width: 0;
}
.badge {
    flex-shrink: 0;
    font-size: 9px;
    font-weight: 600;
    padding: 1px 4px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.badge-defi { background: var(--badge-defi-bg); color: var(--badge-defi-fg); }
.badge-eq   { background: var(--badge-eq-bg);   color: var(--badge-eq-fg); }
.mom-up   { flex-shrink: 0; font-size: 10px; color: #4ade80; }
.mom-dn   { flex-shrink: 0; font-size: 10px; color: #f87171; }
.mom-flat { flex-shrink: 0; font-size: 10px; color: #555550; }
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeFi vs Equity — Valuation Charts</title>
<style>{css}</style>
{plotlyjs}
</head>
<body>

<div class="page-header">
  <h1>DeFi Protocol Screener</h1>
  <p>P/E, P/S, revenue and yield across {n_defi} DeFi protocols and {n_eq} tech &amp; fintech equities &nbsp;·&nbsp; Same frameworks equity investors use, applied to on-chain data &nbsp;·&nbsp; Updated {date} &nbsp;·&nbsp; DefiLlama &nbsp;·&nbsp; Yahoo Finance &nbsp;·&nbsp; CoinGecko</p>
</div>

<div class="page-body">

  <!-- ── Charts column ── -->
  <div class="charts-col">

    <div class="filter-bar">
      <div class="filter-group">
        <button class="fbtn active" onclick="filterAll(this)">Show All</button>
      </div>
      <div class="filter-sep"></div>
      <div class="filter-group">
        <span class="filter-label">DeFi</span>
        <button class="fbtn" onclick="filterDefi('DEX', this)">DEX</button>
        <button class="fbtn" onclick="filterDefi('Lending', this)">Lending</button>
        <button class="fbtn" onclick="filterDefi('Derivatives', this)">Derivatives</button>
        <button class="fbtn" onclick="filterDefi('Chain', this)">Chain</button>
        <button class="fbtn" onclick="filterDefi('Liquid Staking', this)">Liquid Staking</button>
        <button class="fbtn" onclick="filterDefi('Other', this)">Other</button>
      </div>
      <div class="filter-sep"></div>
      <div class="filter-group">
        <span class="filter-label">Equity</span>
        <button class="fbtn" onclick="filterEquity('Technology', this)">Technology</button>
        <button class="fbtn" onclick="filterEquity('Financial Services', this)">Fin. Services</button>
        <button class="fbtn" onclick="filterEquity('Communication Services', this)">Comm. Services</button>
      </div>
      <button class="fbtn theme-btn active" id="theme-btn" onclick="switchTheme(this)">Terminal / Report</button>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="num">Chart 1</span><h2>Revenue Over Time</h2>
      </div>
      <div class="chart-wrap">{chart1}</div>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="num">Chart 2</span><h2>P/E Ratio Over Time</h2>
      </div>
      <div class="chart-wrap">{chart2}</div>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="num">Chart 3</span><h2>Div / Holder Yield Over Time</h2>
      </div>
      <div class="chart-wrap">{chart3}</div>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="num">Chart 4</span><h2>Revenue Momentum</h2>
      </div>
      <div class="chart-wrap">{chart4}</div>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="num">Chart 5</span><h2>Real Yield vs Earnings Yield</h2>
      </div>
      <div class="chart-wrap">{chart5}</div>
    </div>

  </div><!-- end charts-col -->

  <!-- ── Legend sidebar ── -->
  <div class="legend-col" id="legend-col">
    <div class="legend-col-hdr">Assets</div>
    <div class="legend-items" id="legend-items">
{sidebar_html}
    </div>
  </div>

</div><!-- end page-body -->

<script>
// Asset metadata: name/ticker → {{type, category/sector, hasHistory, colour, colour_report}}
const ASSET_META = {asset_meta_json};

const CHART_IDS       = ["chart1", "chart2", "chart3", "chart4", "chart5"];
const LINE_CHART_IDS  = ["chart1", "chart2", "chart3"];

// Source of truth for which assets are currently visible.
let visibleSet = new Set(Object.keys(ASSET_META));

// ── Theme definitions ──────────────────────────────────────────────────────
const THEMES = {{
    terminal: {{
        plot_bgcolor:  '#0c0c0a',
        paper_bgcolor: '#141412',
        font_color:    '#e8e6df',
        gridcolor:     '#252520',
        zerolinecolor: '#252520',
        colour_key:    'colour',
    }},
    report: {{
        plot_bgcolor:  '#ffffff',
        paper_bgcolor: '#f8fafc',
        font_color:    '#0f172a',
        gridcolor:     '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        colour_key:    'colour_report',
    }},
}};
let activeTheme = 'terminal';

function updateCharts() {{
    CHART_IDS.forEach(function(cid) {{
        var gd = document.getElementById(cid);
        if (!gd || !gd.data) return;
        var vis = gd.data.map(function(trace) {{
            var name = trace.name.endsWith('__ey') ? trace.name.slice(0, -4) : trace.name;
            return visibleSet.has(name);
        }});
        Plotly.restyle(cid, {{visible: vis}});
    }});
}}

function updateSidebar() {{
    document.querySelectorAll('.legend-item').forEach(function(el) {{
        var name = el.dataset.name;
        el.classList.toggle('active', visibleSet.has(name));
    }});
}}

function activateBtn(el) {{
    // Exclude the theme toggle button from filter-button group
    document.querySelectorAll('.fbtn:not(.theme-btn)').forEach(function(b) {{
        b.classList.remove('active');
    }});
    if (el) el.classList.add('active');
}}

// Individual asset toggle from sidebar click
function toggleAsset(el) {{
    var name = el.dataset.name;
    if (visibleSet.has(name)) {{
        visibleSet.delete(name);
    }} else {{
        visibleSet.add(name);
    }}
    updateCharts();
    updateSidebar();
    activateBtn(null);
}}

function setVisible(predicate) {{
    visibleSet = new Set();
    Object.keys(ASSET_META).forEach(function(name) {{
        if (predicate(name, ASSET_META[name])) visibleSet.add(name);
    }});
    updateCharts();
    updateSidebar();
}}

function filterAll(el) {{
    setVisible(function() {{ return true; }});
    activateBtn(el);
}}

function filterDefi(cat, el) {{
    setVisible(function(name, meta) {{
        return meta.type === 'defi' && meta.category === cat;
    }});
    activateBtn(el);
}}

function filterEquity(sector, el) {{
    setVisible(function(name, meta) {{
        return meta.type === 'equity' && meta.sector === sector;
    }});
    activateBtn(el);
}}

// ── Theme toggle ───────────────────────────────────────────────────────────
function switchTheme(el) {{
    activeTheme = (activeTheme === 'terminal') ? 'report' : 'terminal';
    var theme = THEMES[activeTheme];

    // Relayout: chart background + grid colours + font colour (all charts)
    var layoutUpdate = {{
        plot_bgcolor:           theme.plot_bgcolor,
        paper_bgcolor:          theme.paper_bgcolor,
        'font.color':           theme.font_color,
        'xaxis.gridcolor':      theme.gridcolor,
        'xaxis.linecolor':      theme.gridcolor,
        'xaxis.zerolinecolor':  theme.zerolinecolor,
        'xaxis2.gridcolor':     theme.gridcolor,
        'xaxis2.linecolor':     theme.gridcolor,
        'xaxis2.zerolinecolor': theme.zerolinecolor,
        'yaxis.gridcolor':      theme.gridcolor,
        'yaxis.linecolor':      theme.gridcolor,
        'yaxis.zerolinecolor':  theme.zerolinecolor,
        'yaxis2.gridcolor':     theme.gridcolor,
        'yaxis2.linecolor':     theme.gridcolor,
        'yaxis2.zerolinecolor': theme.zerolinecolor,
    }};

    CHART_IDS.forEach(function(cid) {{
        var gd = document.getElementById(cid);
        if (!gd || !gd.data) return;
        Plotly.relayout(cid, layoutUpdate);
    }});

    // Restyle trace line + marker colours (line charts only — bar charts keep value colours)
    LINE_CHART_IDS.forEach(function(cid) {{
        var gd = document.getElementById(cid);
        if (!gd || !gd.data) return;
        var lineColors = gd.data.map(function(trace) {{
            var meta = ASSET_META[trace.name];
            return meta ? meta[theme.colour_key] : '#888888';
        }});
        Plotly.restyle(cid, {{'line.color': lineColors, 'marker.color': lineColors}});
    }});

    // Update chart1 subplot title annotation colours
    var titleColor = theme.font_color;
    Plotly.relayout('chart1', {{
        'annotations[0].font.color': titleColor,
        'annotations[1].font.color': titleColor,
    }});

    // Update reference line + annotation colours for charts 2 and 3
    var refColor = (activeTheme === 'terminal') ? '#6b6860' : '#9ca3af';
    Plotly.relayout('chart2', {{
        'shapes[0].line.color':      refColor,
        'annotations[0].font.color': refColor,
    }});
    Plotly.relayout('chart3', {{
        'shapes[0].line.color':      refColor,
        'annotations[0].font.color': refColor,
    }});

    // Swap CSS theme class on body
    document.body.classList.toggle('report', activeTheme === 'report');

    // Update sidebar swatches
    document.querySelectorAll('.legend-item').forEach(function(item) {{
        var name = item.dataset.name;
        var meta = ASSET_META[name];
        if (!meta) return;
        item.querySelector('.swatch').style.background = meta[theme.colour_key];
    }});

    // Reflect active/inactive state on the theme button itself
    el.classList.toggle('active', activeTheme === 'terminal');
}}
</script>

</body>
</html>
"""


def fig_to_div(fig, div_id):
    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        div_id=div_id,
        config={"displayModeBar": True, "responsive": True},
    )


def build_html(fig1, fig2, fig3, fig4, fig5, asset_meta, fundamentals, eq_snap, sidebar_html):
    plotlyjs = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
    return HTML_TEMPLATE.format(
        css=PAGE_CSS,
        plotlyjs=plotlyjs,
        n_defi=len(fundamentals),
        n_eq=len(eq_snap),
        date=date.today().strftime("%B %d, %Y"),
        chart1=fig_to_div(fig1, "chart1"),
        chart2=fig_to_div(fig2, "chart2"),
        chart3=fig_to_div(fig3, "chart3"),
        chart4=fig_to_div(fig4, "chart4"),
        chart5=fig_to_div(fig5, "chart5"),
        asset_meta_json=json.dumps(asset_meta, ensure_ascii=False),
        sidebar_html=sidebar_html,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Locate input files
    fund_path   = find_latest("??????_fundamentals.tsv")
    eq_s_path   = find_latest("??????_equities.tsv")
    defi_h_path = find_latest("??????_historical_defi.tsv")
    eq_q_path   = find_latest("??????_historical_equity.tsv")
    eq_d_path   = find_latest("??????_historical_equity_daily.tsv")

    for p in (fund_path, eq_s_path, defi_h_path, eq_q_path, eq_d_path):
        print(f"Reading {p.name}")

    # Load data
    fundamentals = load_fundamentals(fund_path)
    eq_snap      = load_equities_snapshot(eq_s_path)
    defi_hist    = load_defi_history(defi_h_path)
    eq_q         = load_equity_quarterly(eq_q_path)
    eq_d         = load_equity_daily(eq_d_path)

    # Daily P/E for equities
    eq_pe_daily = compute_daily_pe(eq_q, eq_d)

    # Dynamic history sets
    hist_defi   = set(defi_hist.keys())
    hist_equity = set(eq_d.keys())

    print(f"Assets: {len(fundamentals)} DeFi  +  {len(eq_snap)} equities")
    print(f"With history: {len(hist_defi)} DeFi  +  {len(hist_equity)} equity")

    # Assign one colour per asset (consistent across all 3 charts)
    asset_colours = assign_asset_colours(fundamentals, eq_snap)

    # Asset metadata for JS
    asset_meta  = build_asset_meta(fundamentals, eq_snap, hist_defi, hist_equity,
                                   asset_colours)
    sidebar_html = build_sidebar_html(asset_meta)

    # Build charts
    print("Building charts…")
    fig1 = build_revenue_chart(defi_hist, eq_q, fundamentals, eq_snap,
                               hist_defi, hist_equity, asset_colours)
    fig2 = build_pe_chart(defi_hist, eq_pe_daily, fundamentals, eq_snap,
                          hist_defi, hist_equity, asset_colours)
    fig3 = build_yield_chart(defi_hist, eq_d, fundamentals, eq_snap,
                             hist_defi, hist_equity, asset_colours)
    fig4 = build_momentum_chart(fundamentals, eq_snap)
    fig5 = build_real_yield_chart(fundamentals, eq_snap)

    html = build_html(fig1, fig2, fig3, fig4, fig5, asset_meta, fundamentals, eq_snap,
                      sidebar_html)

    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote → {HTML_OUT.name}  ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
