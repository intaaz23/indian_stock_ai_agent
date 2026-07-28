from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import math

import pandas as pd
from flask import Flask, render_template, request, abort

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]   # repo root
DATA_DIR = PROJECT_ROOT / "data" / "output"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

# ----------------------------
# Data loading helpers
# ----------------------------

def _safe_read_csv(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _first_existing_file(candidates: List[Path]) -> Path | None:
    for c in candidates:
        if c.exists():
            return c
    return None


def load_universe_df() -> pd.DataFrame:
    """
    Load latest output CSV from /data.
    Priority:
      1) explicit known file names
      2) newest *.csv in data folder
    """
    candidates = [
        DATA_DIR / "nse_quant_output.csv",
        DATA_DIR / "qualitative_llm_output.csv",
        DATA_DIR / "qualitative_llm_input.csv",
    ]

    for c in candidates:
        if c.exists():
            print(f"[DATA] Using CSV: {c}")
            return _safe_read_csv(c)

    csv_files = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if csv_files:
        print(f"[DATA] Using newest CSV fallback: {csv_files[0]}")
        return _safe_read_csv(csv_files[0])

    print(f"[DATA] No CSV found in: {DATA_DIR}")
    return pd.DataFrame()

def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        if isinstance(v, str):
            v = v.replace(",", "").replace("₹", "").strip()
            if v == "":
                return None
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except Exception:
        return None


def _pick(row: pd.Series, keys: List[str], default=None):
    for k in keys:
        if k in row and pd.notna(row[k]):
            return row[k]
    return default


def normalize_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty:
        return []

    def norm(s: str) -> str:
        return "".join(ch for ch in str(s).lower() if ch.isalnum())

    original_cols = list(df.columns)
    norm_to_original = {norm(c): c for c in original_cols}

    def get_val(row: pd.Series, aliases: List[str], default=None):
        for a in aliases:
            key = norm(a)
            if key in norm_to_original:
                v = row.get(norm_to_original[key], default)
                if pd.notna(v):
                    return v
        return default

    rows: List[Dict[str, Any]] = []
    for i, row in df.iterrows():
        symbol = get_val(row, ["symbol", "ticker", "stock", "nse_symbol", "tradingsymbol", "yfinance_ticker"])
        company = get_val(row, ["company_name", "company", "name", "long_name"])
        sector = get_val(row, ["sector", "industry", "sector_name"])

        cmp_val = get_val(row, ["cmp", "current_price", "price", "close", "ltp"])
        market_cap = get_val(row, ["market_cap_cr", "market_cap", "marketcap", "mcap", "mcap_cr"])
        pe = get_val(row, ["pe", "pe_ratio", "peratio", "trailing_pe", "price_to_earnings"])
        roe = get_val(row, ["roe", "roe_pct", "return_on_equity", "returnonequity", "roepercent", "roe_ttm", "roe%"])
        roce = get_val(row, ["roce", "roce_percent", "roce_pct", "return_on_capital_employed", "returnoncapitalemployed", "roce%"])
        de = get_val(row, ["de", "d_e", "de_ratio", "debtequity", "debt_to_equity", "debt_to_equity_percent",
    "debt/equity", "debtequityratio", "d/e"])
        opm = get_val(row, ["opm", "opm_percent", "opm_pct", "operating_margin", "operating_margin_percent",
    "operatingprofitmargin", "operatingmargin", "opm%"])
        sales_cagr = get_val(row, ["sales_cagr", "sales_cagr_5y", "sales_growth_5y", "salesgrowth5y", "salescagr"])
        profit_cagr = get_val(row, ["profit_cagr", "profit_cagr_5y", "profit_growth_5y", "profitgrowth5y", "pat_cagr"])

        ai_score = get_val(row, ["ai_score", "score", "final_score", "total_score", "composite_score", "quant_score"])
        verdict = get_val(row, ["verdict", "signal", "rating", "recommendation"], None)

        if symbol is None or str(symbol).strip() == "":
            continue

        score_f = _to_float(ai_score)

        # Derive verdict if missing
        if verdict is None or str(verdict).strip() == "" or str(verdict).lower() == "nan":
            if score_f is None:
                verdict_s = "Watch"
            elif score_f >= 75:
                verdict_s = "Buy"
            elif score_f >= 55:
                verdict_s = "Watch"
            else:
                verdict_s = "Avoid"
        else:
            verdict_s = str(verdict).strip()

        rows.append({
            "rank": i + 1,
            "symbol": str(symbol).replace(".NS", "").strip(),
            "company_name": str(company).strip() if company is not None else "",
            "sector": str(sector).strip() if sector is not None else "",
            "cmp": _to_float(cmp_val),
            "market_cap": _to_float(market_cap),
            "pe": _to_float(pe),
            "roe": _to_float(roe),
            "roce": _to_float(roce),
            "de": _to_float(de),
            "opm": _to_float(opm),
            "sales_cagr": _to_float(sales_cagr),
            "profit_cagr": _to_float(profit_cagr),
            "ai_score": score_f,
            "verdict": verdict_s,
        })

    print(f"[DATA] columns in CSV: {original_cols}")
    print(f"[DATA] normalize_rows -> {len(rows)} rows")
    if rows:
        print(f"[DATA] sample mapped row: {rows[0]}")
    return rows


def apply_filters(rows: List[Dict[str, Any]], args) -> List[Dict[str, Any]]:
    sector = (args.get("sector") or "").strip().lower()
    mcap_min = _to_float(args.get("mcap_min"))  # optional if not available in rows
    pe_max = _to_float(args.get("pe_max"))
    roe_min = _to_float(args.get("roe_min"))
    de_max = _to_float(args.get("de_max"))
    q = (args.get("q") or "").strip().lower()

    filtered = []
    for r in rows:
        # sector
        if sector and (r.get("sector") or "").lower() != sector:
            continue
        # PE
        if pe_max is not None:
            pe = r.get("pe")
            if pe is None or pe > pe_max:
                continue
        # ROE
        if roe_min is not None:
            roe = r.get("roe")
            if roe is None or roe < roe_min:
                continue
        # D/E
        if de_max is not None:
            de = r.get("de")
            if de is None or de > de_max:
                continue
        # quick q
        if q:
            combined = f"{r.get('symbol','')} {r.get('company_name','')}".lower()
            if q not in combined:
                continue
        # mcap_min intentionally skipped unless you add mapped market cap field
        _ = mcap_min
        filtered.append(r)

    # default sort by ai_score desc (None last)
    filtered.sort(key=lambda x: (x["ai_score"] is None, -(x["ai_score"] or -1e9)))
    for i, r in enumerate(filtered, 1):
        r["rank"] = i
    return filtered


def build_stock_detail(symbol: str, rows: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    target = None
    for r in rows:
        if (r.get("symbol") or "").lower() == symbol.lower():
            target = r
            break
    if not target:
        return None

    score = target.get("ai_score")
    confidence = "Medium"
    if score is not None:
        if score >= 80:
            confidence = "High"
        elif score >= 60:
            confidence = "Medium"
        else:
            confidence = "Low"

    stock = {
        "symbol": target.get("symbol"),
        "company_name": target.get("company_name"),
        "sector": target.get("sector"),
        "cmp": target.get("cmp"),
        "updated_at": "Latest run",
        "verdict": target.get("verdict") or "Watch",
        "confidence": confidence,
        "pe": target.get("pe"),
        "roe": target.get("roe"),
        "roce": None,
        "de": target.get("de"),
        "opm": None,
        "sales_cagr": None,
        "profit_cagr": None,
        "fcf_trend": "N/A",
        "ai_positives": [
            "Strong relative score vs screened universe" if score else "Score available from latest run",
            "Fundamental metrics consolidated in one view",
            "Can be compared with peers from same sector",
        ],
        "ai_risks": [
            "Model output depends on source data quality",
            "Single-score decisions can miss qualitative factors",
            "Always validate with latest filings/news",
        ],
    }
    return stock


def build_peers(target_symbol: str, rows: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    target = next((r for r in rows if (r.get("symbol") or "").lower() == target_symbol.lower()), None)
    if not target:
        return []

    sector = (target.get("sector") or "").lower()
    peers = [r for r in rows if (r.get("symbol") or "").lower() != target_symbol.lower()]

    if sector:
        peers = [p for p in peers if (p.get("sector") or "").lower() == sector]

    peers = sorted(peers, key=lambda x: (x["ai_score"] is None, -(x["ai_score"] or -1e9)))
    return peers[:limit]


# ----------------------------
# Routes
# ----------------------------

@app.route("/")
def index():
    df = load_universe_df()
    print(f"[DATA] DataFrame shape: {df.shape}")
    print(f"[DATA] Columns: {list(df.columns)}")

    rows = normalize_rows(df)
    sectors = sorted({r["sector"] for r in rows if r.get("sector")})
    rows = apply_filters(rows, request.args)

    # last_updated set below (next step)
    last_updated = None
    csv_files = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if csv_files:
        from datetime import datetime
        last_updated = datetime.fromtimestamp(csv_files[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    print(f"[DATA] Rows after filters: {len(rows)}")
    if rows:
        print(f"[DATA] First row sample: {rows[0]}")

    return render_template(
        "index.html",
        rows=rows,
        sectors=sectors,
        last_updated=last_updated,
    )

@app.route("/stock/<symbol>")
def stock_detail(symbol: str):
    df = load_universe_df()
    rows = normalize_rows(df)

    stock = build_stock_detail(symbol, rows)
    if not stock:
        abort(404, description=f"Stock '{symbol}' not found in latest output")

    peers = build_peers(symbol, rows)
    return render_template("stock_detail.html", stock=stock, peers=peers)


@app.errorhandler(404)
def not_found(e):
    return (
        render_template(
            "base.html",
        ),
        404,
    )

@app.template_filter("num2")
def num2(v):
    try:
        if v is None or v == "":
            return "-"
        return f"{float(v):,.2f}"
    except Exception:
        return "-"

if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=5000, debug=True)