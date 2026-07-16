from pathlib import Path
import argparse
import time
import math
from io import StringIO
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict

import requests
import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
DEFAULT_SCREENER_FUNDAMENTALS_FILE = PROJECT_ROOT / "data" / "input" / "screener_fundamentals.csv"
DEFAULT_QUANT_CACHE_FILE = DEFAULT_OUTPUT_DIR / "nse_quant_output.csv"


NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


# -----------------------------
# Utility Functions
# -----------------------------

def safe_float(value, default=None):
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_divide(a, b, default=None):
    try:
        if a is None or b is None or b == 0:
            return default
        return a / b
    except Exception:
        return default


def clamp(value, low=0, high=100):
    if value is None:
        return None
    return max(low, min(high, value))


def score_range(value, excellent, good, average, bad, higher_is_better=True, missing_score=50):
    """
    Converts any financial metric into a 0-100 score.
    """
    if value is None:
        return missing_score

    if higher_is_better:
        if value >= excellent:
            return 100
        if value >= good:
            return 80
        if value >= average:
            return 60
        if value >= bad:
            return 40
        return 20
    else:
        if value <= excellent:
            return 100
        if value <= good:
            return 80
        if value <= average:
            return 60
        if value <= bad:
            return 40
        return 20


def rupees_to_crore(value):
    """
    yfinance market cap is usually in rupees for Indian stocks.
    1 crore = 10,000,000.
    """
    if value is None:
        return None
    return value / 10_000_000


def percent_from_decimal(value):
    """
    yfinance sometimes returns ROE, margins, growth as decimal.
    Example: 0.15 means 15%.
    """
    if value is None:
        return None
    return value * 100

def clean_symbol_for_merge(symbol: str) -> str:
    if symbol is None:
        return ""
    symbol = str(symbol).strip().upper()
    symbol = symbol.replace(".NS", "")
    return symbol


def score_promoter_holding(value):
    """
    Promoter holding scoring for Indian companies.

    This is a rough alignment score:
    - 45% to 75% is usually strong promoter skin-in-the-game
    - very low holding needs review
    - extremely high holding is okay but reduces public float
    """
    value = safe_float(value)

    if value is None:
        return 50

    if 45 <= value <= 75:
        return 100
    if 35 <= value < 45:
        return 80
    if 25 <= value < 35:
        return 65
    if 15 <= value < 25:
        return 45
    if value < 15:
        return 25

    # Above 75 can be okay but public float is lower
    return 70


def score_promoter_pledge(value):
    """
    Promoter pledge scoring.

    Important:
    - Missing pledge is not same as zero pledge.
    - Missing = neutral/caution score.
    """
    value = safe_float(value)

    if value is None:
        return 50

    if value == 0:
        return 100
    if value <= 5:
        return 80
    if value <= 20:
        return 45
    if value <= 50:
        return 20
    return 5


def score_institutional_trend(fii_change, dii_change):
    """
    Scores combined FII/DII change over last 4 quarters.
    This is only a supporting signal, not a buy/sell signal.
    """
    fii_change = safe_float(fii_change)
    dii_change = safe_float(dii_change)

    if fii_change is None and dii_change is None:
        return 50

    total_change = (fii_change or 0) + (dii_change or 0)

    if total_change >= 4:
        return 100
    if total_change >= 2:
        return 85
    if total_change >= 0:
        return 70
    if total_change >= -2:
        return 50
    if total_change >= -5:
        return 30
    return 15


def score_quarterly_consistency(value):
    value = safe_float(value)

    if value is None:
        return 50

    if value >= 100:
        return 100
    if value >= 75:
        return 80
    if value >= 50:
        return 60
    if value >= 25:
        return 40
    return 20


def score_cash_flow_history(row):
    """
    Scores 5Y cash flow quality from Screener.

    For banks/NBFCs/insurance, normal FCF is not meaningful,
    so return neutral score.
    """
    sector_group = str(row.get("sector_scoring_group", "")).lower()

    if sector_group in ["bank", "nbfc", "insurance"]:
        return 50

    free_cash_flow_5y = safe_float(row.get("free_cash_flow_5y"))
    cash_conversion_5y = safe_float(row.get("cash_conversion_5y"))

    fcf_score = 50

    if free_cash_flow_5y is not None:
        if free_cash_flow_5y > 0:
            fcf_score = 80
        else:
            fcf_score = 25

    cash_conversion_score = score_range(
        cash_conversion_5y,
        excellent=1.2,
        good=1.0,
        average=0.75,
        bad=0.5,
        higher_is_better=True,
        missing_score=50,
    )

    return round(fcf_score * 0.45 + cash_conversion_score * 0.55, 2)


def append_red_flag(existing_flags, new_flag):
    existing_flags = "" if pd.isna(existing_flags) else str(existing_flags).strip()

    if not new_flag:
        return existing_flags

    if not existing_flags:
        return new_flag

    if new_flag.lower() in existing_flags.lower():
        return existing_flags

    return existing_flags + "; " + new_flag


def calculate_enhanced_screener_score(row):
    """
    Calculates enhanced Screener-based score.

    For general companies:
    - ROCE
    - 5Y growth
    - promoter quality
    - FII/DII trend
    - cash flow
    - quarterly trend
    - data completeness

    For banks/NBFCs/insurance:
    - avoid using FCF as strong signal
    - use more neutral weighting until proper bank/NBFC model is added
    """
    sector_group = str(row.get("sector_scoring_group", "")).lower()

    roce_score = score_range(
        safe_float(row.get("roce_percent")),
        excellent=25,
        good=18,
        average=12,
        bad=8,
        higher_is_better=True,
        missing_score=50,
    )

    sales_5y_score = score_range(
        safe_float(row.get("sales_growth_5y")),
        excellent=18,
        good=12,
        average=7,
        bad=3,
        higher_is_better=True,
        missing_score=50,
    )

    profit_5y_score = score_range(
        safe_float(row.get("profit_growth_5y")),
        excellent=20,
        good=14,
        average=8,
        bad=3,
        higher_is_better=True,
        missing_score=50,
    )

    long_term_growth_score = round(
        sales_5y_score * 0.45 + profit_5y_score * 0.55,
        2,
    )

    promoter_holding_score = score_promoter_holding(row.get("promoter_holding"))
    pledge_score = score_promoter_pledge(row.get("promoter_pledge_percent"))

    promoter_quality_score = round(
        promoter_holding_score * 0.55 + pledge_score * 0.45,
        2,
    )

    institutional_trend_score = score_institutional_trend(
        row.get("fii_holding_change_4q"),
        row.get("dii_holding_change_4q"),
    )

    cash_flow_history_score = score_cash_flow_history(row)

    latest_sales_growth_score = score_range(
        safe_float(row.get("latest_quarter_sales_growth")),
        excellent=20,
        good=10,
        average=0,
        bad=-10,
        higher_is_better=True,
        missing_score=50,
    )

    latest_profit_growth_score = score_range(
        safe_float(row.get("latest_quarter_profit_growth")),
        excellent=20,
        good=10,
        average=0,
        bad=-10,
        higher_is_better=True,
        missing_score=50,
    )

    sales_consistency_score = score_quarterly_consistency(
        row.get("quarterly_sales_consistency")
    )

    profit_consistency_score = score_quarterly_consistency(
        row.get("quarterly_profit_consistency")
    )

    quarterly_trend_score = round(
        latest_sales_growth_score * 0.25
        + latest_profit_growth_score * 0.35
        + sales_consistency_score * 0.20
        + profit_consistency_score * 0.20,
        2,
    )

    data_confidence_score = score_range(
        safe_float(row.get("data_completeness_score")),
        excellent=85,
        good=70,
        average=55,
        bad=40,
        higher_is_better=True,
        missing_score=40,
    )

    financial_groups = ["bank", "nbfc", "insurance"]

    if sector_group in financial_groups:
        # Until separate bank/NBFC model is added, use conservative weights.
        enhanced_score = (
            long_term_growth_score * 0.25
            + promoter_quality_score * 0.20
            + institutional_trend_score * 0.15
            + quarterly_trend_score * 0.25
            + data_confidence_score * 0.15
        )
    else:
        enhanced_score = (
            roce_score * 0.20
            + long_term_growth_score * 0.25
            + promoter_quality_score * 0.15
            + institutional_trend_score * 0.10
            + cash_flow_history_score * 0.15
            + quarterly_trend_score * 0.10
            + data_confidence_score * 0.05
        )

    return round(enhanced_score, 2)


def merge_screener_fundamentals(results_df: pd.DataFrame, screener_file: str) -> pd.DataFrame:
    """
    Merges data/input/screener_fundamentals.csv into quant results
    and recalculates enhanced quant score.

    Keeps original yfinance score as base_quant_score.
    Updates quant_score and investor_master_score to enhanced combined score.
    """
    screener_path = Path(screener_file)

    results_df["symbol"] = results_df["symbol"].apply(clean_symbol_for_merge)

    if "base_quant_score" not in results_df.columns:
        results_df["base_quant_score"] = results_df["quant_score"]
    else:
        results_df["base_quant_score"] = results_df["base_quant_score"].fillna(
        results_df["quant_score"]
    )
    results_df["enhanced_screener_score"] = None
    results_df["screener_data_available"] = False

    if not screener_path.exists():
        print(f"\nScreener fundamentals file not found: {screener_path}")
        print("Continuing with yfinance-only quant scores.")
        return results_df

    screener_df = pd.read_csv(screener_path)

    if screener_df.empty or "symbol" not in screener_df.columns:
        print(f"\nScreener fundamentals file is empty or missing symbol column: {screener_path}")
        print("Continuing with yfinance-only quant scores.")
        return results_df

    screener_df["symbol"] = screener_df["symbol"].apply(clean_symbol_for_merge)
    screener_df = screener_df.drop_duplicates(subset=["symbol"], keep="last")

    print(f"\nMerging Screener fundamentals from: {screener_path}")
    print(f"Screener rows available: {len(screener_df)}")

    results_df = results_df.merge(
        screener_df,
        on="symbol",
        how="left",
        suffixes=("", "_screener"),
    )

    results_df["screener_data_available"] = results_df["status"].notna()

    results_df["enhanced_screener_score"] = results_df.apply(
        lambda row: calculate_enhanced_screener_score(row)
        if row.get("screener_data_available")
        else None,
        axis=1,
    )

    def combined_score(row):
        base_score = safe_float(row.get("base_quant_score"))
        screener_score = safe_float(row.get("enhanced_screener_score"))
        data_completeness = safe_float(row.get("data_completeness_score"))
        pledge = safe_float(row.get("promoter_pledge_percent"))

        if base_score is None:
            return None

        if screener_score is None:
            return round(base_score, 2)

        # Combine yfinance score with structured Screener score.
        score = base_score * 0.70 + screener_score * 0.30

        # Conservative caps.
        if data_completeness is not None and data_completeness < 50:
            score = min(score, 70)

        if pledge is not None and pledge > 20:
            score = min(score, 65)

        if pledge is not None and pledge > 50:
            score = min(score, 55)

        return round(score, 2)

    results_df["investor_master_score"] = results_df.apply(combined_score, axis=1)
    results_df["quant_score"] = results_df["investor_master_score"]

    # Append important Screener red flags.
    for idx, row in results_df.iterrows():
        flags = row.get("red_flags", "")

        pledge = safe_float(row.get("promoter_pledge_percent"))
        data_completeness = safe_float(row.get("data_completeness_score"))
        sector_group = str(row.get("sector_scoring_group", "")).lower()

        if pledge is not None and pledge > 20:
            flags = append_red_flag(flags, "Promoter pledge above 20%")

        if pledge is not None and pledge > 50:
            flags = append_red_flag(flags, "Promoter pledge above 50%")

        if data_completeness is not None and data_completeness < 50:
            flags = append_red_flag(flags, "Low Screener data completeness")

        if sector_group in ["bank", "nbfc", "insurance"]:
            flags = append_red_flag(
                flags,
                f"Sector-specific manual review required: {sector_group}",
            )

        results_df.at[idx, "red_flags"] = flags

    matched_count = int(results_df["screener_data_available"].sum())
    print(f"Screener rows matched with quant output: {matched_count}")

    return results_df

def save_shortlist_outputs(
    results_df: pd.DataFrame,
    output_file: str,
    shortlist_file: str,
    shortlist_top_n: int,
    min_quant_score: float,
    min_market_cap_cr: float,
):
    """
    Saves full quant output and shortlist output from an existing results dataframe.
    Used by both:
    - normal full yfinance pipeline
    - rerank-from-cache pipeline
    """


    # Filter by minimum market cap
    if min_market_cap_cr and min_market_cap_cr > 0:
        before_count = len(results_df)

        results_df = results_df[
            results_df["market_cap_cr"].fillna(0) >= min_market_cap_cr
        ].copy()

        after_count = len(results_df)

        print(f"\nMarket cap filter applied: >= ₹{min_market_cap_cr:,.0f} Cr")
        print(f"Stocks before filter: {before_count}, after filter: {after_count}")

    if results_df.empty:
        print("\nNo stocks available after market cap filter.")
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(shortlist_file).parent.mkdir(parents=True, exist_ok=True)
        results_df.to_csv(output_file, index=False)
        pd.DataFrame().to_csv(shortlist_file, index=False)
        return results_df

    results_df = results_df.sort_values(
        by=[
            "investor_master_score",
            "entry_score",
            "quality_score",
            "growth_score",
            "valuation_score",
        ],
        ascending=False,
    )

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(shortlist_file).parent.mkdir(parents=True, exist_ok=True)

    results_df.to_csv(output_file, index=False)
    print(f"\nFull quantitative output saved to: {output_file}")

    shortlist_df = results_df[
        (results_df["quant_score"] >= min_quant_score)
        & (~results_df["quant_zone"].str.contains("AVOID", na=False))
    ].copy()

    shortlist_df = shortlist_df.head(shortlist_top_n)

    qualitative_columns = [
        "symbol",
        "yahoo_symbol",
        "company_name",
        "nse_company_name",
        "isin",
        "sector",
        "industry",
        "price",
        "market_cap_cr",
        "trailing_pe",
        "forward_pe",
        "price_to_book",
        "roe_percent",
        "debt_to_equity_percent",
        "revenue_growth_percent",
        "earnings_growth_percent",
        "profit_margin_percent",
        "operating_margin_percent",
        "dividend_yield_percent",
        "cash_conversion_ratio",
        "beta",
        "fifty_two_week_high",
        "fifty_two_week_low",
        "fifty_two_week_position",

        "quality_score",
        "growth_score",
        "valuation_score",
        "size_liquidity_score",

        "buffett_munger_score",
        "graham_value_score",
        "lynch_garp_score",
        "fisher_growth_score",
        "greenblatt_score",
        "marks_risk_score",
        "india_compounder_score",
        "entry_score",

        "base_quant_score",
        "enhanced_screener_score",
        "quant_score",
        "investor_master_score",

        "roce_percent",
        "roe_percent_screener",
        "sales_growth_5y",
        "profit_growth_5y",
        "promoter_holding",
        "promoter_holding_change_4q",
        "promoter_pledge_percent",
        "fii_holding",
        "dii_holding",
        "fii_holding_change_4q",
        "dii_holding_change_4q",
        "operating_cash_flow_5y",
        "capex_5y",
        "free_cash_flow_5y",
        "cash_conversion_5y",
        "fcf_calculation_method",
        "current_pe",
        "median_pe_5y",
        "price_to_median_pe",
        "latest_quarter_sales_growth",
        "latest_quarter_profit_growth",
        "quarterly_sales_consistency",
        "quarterly_profit_consistency",
        "data_completeness_score",
        "sector_scoring_group",
        "screener_data_available",

        "estimated_fair_value",
        "strong_buy_below",
        "accumulate_below",
        "expensive_above",

        "quant_zone",
        "investor_style_match",
        "red_flags",
    ]

    qualitative_columns = [
        col for col in qualitative_columns
        if col in shortlist_df.columns
    ]

    shortlist_df = shortlist_df[qualitative_columns]
    shortlist_df.to_csv(shortlist_file, index=False)

    print(f"Shortlist for Qualitative LLM Reader saved to: {shortlist_file}")

    print("\nTop shortlisted stocks:")
    if shortlist_df.empty:
        print("No stocks matched the shortlist criteria.")
    else:
        for _, row in shortlist_df.head(20).iterrows():
            print(
                f"{row['symbol']} | "
                f"Score: {row['quant_score']} | "
                f"Base: {row.get('base_quant_score')} | "
                f"Screener: {row.get('enhanced_screener_score')} | "
                f"Zone: {row['quant_zone']} | "
                f"Price: {row['price']} | "
                f"Fair Value: {row['estimated_fair_value']}"
            )

    return results_df

def rerank_from_cache(
    quant_cache_file: str,
    screener_fundamentals_file: str,
    output_file: str,
    shortlist_file: str,
    shortlist_top_n: int,
    min_quant_score: float,
    min_market_cap_cr: float,
    use_screener_fundamentals: bool,
):
    """
    Reranks existing nse_quant_output.csv without fetching yfinance again.
    This is useful after Screener fundamentals are collected.

    Flow:
    - read cached quant output
    - merge Screener fundamentals
    - recalculate enhanced score
    - save final shortlist
    """

    quant_cache_path = Path(quant_cache_file)

    if not quant_cache_path.exists():
        raise FileNotFoundError(
            f"Quant cache file not found: {quant_cache_path}. "
            "Run full quant scan first."
        )

    print(f"\nLoading cached quant output from: {quant_cache_path}")
    results_df = pd.read_csv(quant_cache_path)

    if results_df.empty:
        raise ValueError(f"Cached quant output is empty: {quant_cache_path}")

    if "symbol" not in results_df.columns:
        raise ValueError("Cached quant output must contain a 'symbol' column.")

    if "quant_score" not in results_df.columns:
        raise ValueError("Cached quant output must contain a 'quant_score' column.")

    # Important:
    # Use original base score if already present.
    # This avoids compounding enhanced score on repeated reruns.
    if "base_quant_score" not in results_df.columns:
        results_df["base_quant_score"] = results_df["quant_score"]

    if use_screener_fundamentals:
        results_df = merge_screener_fundamentals(
            results_df=results_df,
            screener_file=screener_fundamentals_file,
        )
    else:
        print("\nScreener merge disabled. Reranking from cached quant only.")
        results_df["enhanced_screener_score"] = None
        results_df["screener_data_available"] = False
        results_df["investor_master_score"] = results_df["base_quant_score"]
        results_df["quant_score"] = results_df["base_quant_score"]

    return save_shortlist_outputs(
        results_df=results_df,
        output_file=output_file,
        shortlist_file=shortlist_file,
        shortlist_top_n=shortlist_top_n,
        min_quant_score=min_quant_score,
        min_market_cap_cr=min_market_cap_cr,
    )

# -----------------------------
# Data Classes
# -----------------------------

@dataclass
class StockMetrics:
    symbol: str
    yahoo_symbol: str
    company_name: Optional[str] = None
    nse_company_name: Optional[str] = None
    isin: Optional[str] = None

    sector: Optional[str] = None
    industry: Optional[str] = None

    price: Optional[float] = None
    market_cap: Optional[float] = None
    market_cap_cr: Optional[float] = None

    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None

    roe_percent: Optional[float] = None
    debt_to_equity_percent: Optional[float] = None

    revenue_growth_percent: Optional[float] = None
    earnings_growth_percent: Optional[float] = None

    profit_margin_percent: Optional[float] = None
    operating_margin_percent: Optional[float] = None
    dividend_yield_percent: Optional[float] = None

    beta: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None

    total_revenue: Optional[float] = None
    net_income: Optional[float] = None
    operating_cashflow: Optional[float] = None
    free_cashflow: Optional[float] = None
    cash_conversion_ratio: Optional[float] = None

    eps_ttm_estimated: Optional[float] = None

    error: Optional[str] = None


@dataclass
class QuantResult:
    symbol: str
    yahoo_symbol: str
    company_name: Optional[str]
    nse_company_name: Optional[str]
    isin: Optional[str]

    sector: Optional[str]
    industry: Optional[str]

    price: Optional[float]
    market_cap_cr: Optional[float]

    trailing_pe: Optional[float]
    forward_pe: Optional[float]
    price_to_book: Optional[float]
    roe_percent: Optional[float]
    debt_to_equity_percent: Optional[float]
    revenue_growth_percent: Optional[float]
    earnings_growth_percent: Optional[float]
    profit_margin_percent: Optional[float]
    operating_margin_percent: Optional[float]
    dividend_yield_percent: Optional[float]
    cash_conversion_ratio: Optional[float]
    beta: Optional[float]
    fifty_two_week_high: Optional[float]
    fifty_two_week_low: Optional[float]
    fifty_two_week_position: Optional[float]

    quality_score: float
    growth_score: float
    valuation_score: float
    size_liquidity_score: float

    buffett_munger_score: float
    graham_value_score: float
    lynch_garp_score: float
    fisher_growth_score: float
    greenblatt_score: float
    marks_risk_score: float
    india_compounder_score: float
    entry_score: float

    quant_score: float
    investor_master_score: float

    estimated_fair_value: Optional[float]
    strong_buy_below: Optional[float]
    accumulate_below: Optional[float]
    expensive_above: Optional[float]

    quant_zone: str
    red_flags: str
    investor_style_match: str
    notes: str

# -----------------------------
# NSE Stock Universe Fetcher
# -----------------------------

def fetch_nse_equity_list() -> pd.DataFrame:
    """
    Fetches official NSE equity list for free.
    Keeps only EQ series.
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/csv,application/csv,text/plain,*/*",
    }

    response = requests.get(NSE_EQUITY_LIST_URL, headers=headers, timeout=30)
    response.raise_for_status()

    df = pd.read_csv(StringIO(response.text))

    df = df[df[" SERIES"] == "EQ"].copy() if " SERIES" in df.columns else df[df["SERIES"] == "EQ"].copy()

    # Handle NSE column spacing variations
    clean_columns = {col: col.strip() for col in df.columns}
    df = df.rename(columns=clean_columns)

    df = df.rename(columns={
        "SYMBOL": "symbol",
        "NAME OF COMPANY": "nse_company_name",
        "ISIN NUMBER": "isin",
        "DATE OF LISTING": "listing_date",
        "FACE VALUE": "face_value",
    })

    return df[["symbol", "nse_company_name", "isin", "listing_date", "face_value"]]


# -----------------------------
# yfinance Data Fetcher
# -----------------------------

def fetch_yfinance_metrics(symbol: str, nse_company_name: str = None, isin: str = None) -> StockMetrics:
    """
    Fetches free available data from yfinance.

    Limitation:
    yfinance data may be incomplete, delayed, or missing for many NSE stocks.
    """
    yahoo_symbol = f"{symbol}.NS"

    metrics = StockMetrics(
        symbol=symbol,
        yahoo_symbol=yahoo_symbol,
        nse_company_name=nse_company_name,
        isin=isin,
    )

    try:
        ticker = yf.Ticker(yahoo_symbol)

        try:
            info = ticker.get_info()
        except Exception:
            info = {}

        try:
            fast_info = ticker.fast_info
        except Exception:
            fast_info = {}

        price = (
            safe_float(info.get("currentPrice"))
            or safe_float(info.get("regularMarketPrice"))
            or safe_float(getattr(fast_info, "last_price", None))
        )

        market_cap = safe_float(info.get("marketCap"))

        total_revenue = safe_float(info.get("totalRevenue"))
        net_income = safe_float(info.get("netIncomeToCommon"))
        operating_cashflow = safe_float(info.get("operatingCashflow"))
        free_cashflow = safe_float(info.get("freeCashflow"))

        trailing_pe = safe_float(info.get("trailingPE"))
        eps_ttm_estimated = None

        if price and trailing_pe and trailing_pe > 0:
            eps_ttm_estimated = price / trailing_pe

        cash_conversion_ratio = safe_divide(operating_cashflow, net_income)

        metrics.company_name = info.get("longName") or info.get("shortName")
        metrics.sector = info.get("sector")
        metrics.industry = info.get("industry")

        metrics.price = price
        metrics.market_cap = market_cap
        metrics.market_cap_cr = rupees_to_crore(market_cap)

        metrics.trailing_pe = trailing_pe
        metrics.forward_pe = safe_float(info.get("forwardPE"))
        metrics.price_to_book = safe_float(info.get("priceToBook"))

        metrics.roe_percent = percent_from_decimal(safe_float(info.get("returnOnEquity")))
        metrics.debt_to_equity_percent = safe_float(info.get("debtToEquity"))

        metrics.revenue_growth_percent = percent_from_decimal(safe_float(info.get("revenueGrowth")))
        metrics.earnings_growth_percent = percent_from_decimal(safe_float(info.get("earningsGrowth")))

        metrics.profit_margin_percent = percent_from_decimal(safe_float(info.get("profitMargins")))
        metrics.operating_margin_percent = percent_from_decimal(safe_float(info.get("operatingMargins")))
        metrics.dividend_yield_percent = percent_from_decimal(safe_float(info.get("dividendYield")))

        metrics.beta = safe_float(info.get("beta"))
        metrics.fifty_two_week_high = safe_float(info.get("fiftyTwoWeekHigh"))
        metrics.fifty_two_week_low = safe_float(info.get("fiftyTwoWeekLow"))

        metrics.total_revenue = total_revenue
        metrics.net_income = net_income
        metrics.operating_cashflow = operating_cashflow
        metrics.free_cashflow = free_cashflow
        metrics.cash_conversion_ratio = cash_conversion_ratio
        metrics.eps_ttm_estimated = eps_ttm_estimated

        return metrics

    except Exception as e:
        metrics.error = str(e)
        return metrics


# -----------------------------
# Quantitative Analyzer
# -----------------------------

class QuantAnalyzer:
    """
    Investor-style quantitative analyzer.

    Inspired by:
    - Buffett/Munger: quality, moat proxy, low debt
    - Graham/Klarman: value and margin of safety
    - Lynch: growth at reasonable price
    - Fisher: growth quality
    - Greenblatt: quality + cheapness
    - Marks: risk control
    - Indian QGLP: quality, growth, longevity, price
    """

    def __init__(self, metrics: StockMetrics):
        self.m = metrics

    # -----------------------------
    # Basic Helpers
    # -----------------------------

    def fifty_two_week_position(self):
        """
        Returns where current price sits in 52-week range.
        0.0 = near 52-week low
        1.0 = near 52-week high
        """
        price = self.m.price
        high = self.m.fifty_two_week_high
        low = self.m.fifty_two_week_low

        if not price or not high or not low or high <= low:
            return None

        return round((price - low) / (high - low), 4)

    def peg_ratio(self):
        """
        Peter Lynch style PEG proxy:
        P/E divided by earnings growth.
        """
        pe = self.m.trailing_pe
        growth = self.m.earnings_growth_percent

        if pe is None or pe <= 0 or growth is None or growth <= 0:
            return None

        return pe / growth

    def estimated_fair_value(self):
        """
        Simple fair value using EPS and fair P/E.

        This is a rough mechanical estimate only.
        Final decision must use qualitative review.
        """
        price = self.m.price
        trailing_pe = self.m.trailing_pe
        earnings_growth = self.m.earnings_growth_percent

        if not price or not trailing_pe or trailing_pe <= 0:
            return None

        eps = price / trailing_pe

        if earnings_growth is None:
            fair_pe = 18
        else:
            # Graham/Lynch style fair P/E proxy.
            fair_pe = 8.5 + 2 * max(0, min(earnings_growth, 20))
            fair_pe = clamp(fair_pe, 10, 35)

        fair_value = eps * fair_pe
        return round(fair_value, 2)

    # -----------------------------
    # Base Factor Scores
    # -----------------------------

    def quality_score(self):
        roe_score = score_range(
            self.m.roe_percent,
            excellent=22,
            good=16,
            average=12,
            bad=8,
            higher_is_better=True,
            missing_score=50,
        )

        debt_score = score_range(
            self.m.debt_to_equity_percent,
            excellent=10,
            good=50,
            average=100,
            bad=200,
            higher_is_better=False,
            missing_score=50,
        )

        profit_margin_score = score_range(
            self.m.profit_margin_percent,
            excellent=20,
            good=14,
            average=8,
            bad=4,
            higher_is_better=True,
            missing_score=50,
        )

        operating_margin_score = score_range(
            self.m.operating_margin_percent,
            excellent=25,
            good=18,
            average=12,
            bad=6,
            higher_is_better=True,
            missing_score=50,
        )

        cash_score = score_range(
            self.m.cash_conversion_ratio,
            excellent=1.2,
            good=1.0,
            average=0.75,
            bad=0.5,
            higher_is_better=True,
            missing_score=50,
        )

        score = (
            roe_score * 0.30
            + debt_score * 0.25
            + profit_margin_score * 0.20
            + operating_margin_score * 0.15
            + cash_score * 0.10
        )

        return round(score, 2)

    def growth_score(self):
        revenue_score = score_range(
            self.m.revenue_growth_percent,
            excellent=20,
            good=12,
            average=8,
            bad=4,
            higher_is_better=True,
            missing_score=50,
        )

        earnings_score = score_range(
            self.m.earnings_growth_percent,
            excellent=25,
            good=15,
            average=8,
            bad=3,
            higher_is_better=True,
            missing_score=50,
        )

        score = revenue_score * 0.45 + earnings_score * 0.55
        return round(score, 2)

    def valuation_score(self):
        pe_score = score_range(
            self.m.trailing_pe,
            excellent=12,
            good=20,
            average=35,
            bad=55,
            higher_is_better=False,
            missing_score=40,
        )

        pb_score = score_range(
            self.m.price_to_book,
            excellent=1.5,
            good=3,
            average=6,
            bad=10,
            higher_is_better=False,
            missing_score=50,
        )

        # High ROE businesses can justify somewhat higher P/B.
        if self.m.roe_percent and self.m.roe_percent >= 20 and self.m.price_to_book:
            pb_score = min(100, pb_score + 10)

        score = pe_score * 0.65 + pb_score * 0.35
        return round(score, 2)

    def size_liquidity_score(self):
        market_cap_cr = self.m.market_cap_cr

        score = score_range(
            market_cap_cr,
            excellent=50_000,
            good=10_000,
            average=2_000,
            bad=500,
            higher_is_better=True,
            missing_score=30,
        )

        return round(score, 2)

    # -----------------------------
    # Investor Style Scores
    # -----------------------------

    def buffett_munger_score(self):
        """
        Wonderful company at fair price:
        high ROE, low debt, strong margins, cash conversion, not crazy valuation.
        """
        quality = self.quality_score()
        valuation = self.valuation_score()

        moat_proxy = score_range(
            self.m.operating_margin_percent,
            excellent=28,
            good=20,
            average=14,
            bad=8,
            higher_is_better=True,
            missing_score=50,
        )

        score = quality * 0.55 + moat_proxy * 0.25 + valuation * 0.20
        return round(score, 2)

    def graham_value_score(self):
        """
        Benjamin Graham / Seth Klarman style:
        low valuation, balance-sheet protection, lower downside risk.
        """
        pe_score = score_range(
            self.m.trailing_pe,
            excellent=10,
            good=15,
            average=22,
            bad=35,
            higher_is_better=False,
            missing_score=40,
        )

        pb_score = score_range(
            self.m.price_to_book,
            excellent=1.2,
            good=2.0,
            average=3.5,
            bad=6.0,
            higher_is_better=False,
            missing_score=50,
        )

        debt_score = score_range(
            self.m.debt_to_equity_percent,
            excellent=10,
            good=50,
            average=100,
            bad=200,
            higher_is_better=False,
            missing_score=50,
        )

        dividend_score = score_range(
            self.m.dividend_yield_percent,
            excellent=3,
            good=1.5,
            average=0.5,
            bad=0,
            higher_is_better=True,
            missing_score=50,
        )

        score = pe_score * 0.35 + pb_score * 0.25 + debt_score * 0.25 + dividend_score * 0.15
        return round(score, 2)

    def lynch_garp_score(self):
        """
        Peter Lynch GARP:
        earnings growth should be good and P/E should not be excessive.
        PEG below 1 is excellent, below 1.5 is good.
        """
        growth = self.growth_score()
        peg = self.peg_ratio()

        peg_score = score_range(
            peg,
            excellent=0.75,
            good=1.25,
            average=2.0,
            bad=3.0,
            higher_is_better=False,
            missing_score=50,
        )

        pe_score = score_range(
            self.m.trailing_pe,
            excellent=15,
            good=25,
            average=40,
            bad=60,
            higher_is_better=False,
            missing_score=45,
        )

        score = growth * 0.45 + peg_score * 0.35 + pe_score * 0.20
        return round(score, 2)

    def fisher_growth_score(self):
        """
        Philip Fisher:
        high-quality growth with good margins.
        """
        revenue_score = score_range(
            self.m.revenue_growth_percent,
            excellent=22,
            good=15,
            average=10,
            bad=5,
            higher_is_better=True,
            missing_score=50,
        )

        earnings_score = score_range(
            self.m.earnings_growth_percent,
            excellent=25,
            good=18,
            average=12,
            bad=5,
            higher_is_better=True,
            missing_score=50,
        )

        margin_score = score_range(
            self.m.operating_margin_percent,
            excellent=25,
            good=18,
            average=12,
            bad=6,
            higher_is_better=True,
            missing_score=50,
        )

        roe_score = score_range(
            self.m.roe_percent,
            excellent=22,
            good=16,
            average=12,
            bad=8,
            higher_is_better=True,
            missing_score=50,
        )

        score = revenue_score * 0.25 + earnings_score * 0.35 + margin_score * 0.20 + roe_score * 0.20
        return round(score, 2)

    def greenblatt_score(self):
        """
        Joel Greenblatt proxy:
        quality + cheapness.
        We do not have ROCE/EVEBIT from free data, so use ROE/margins + PE/PB.
        """
        quality = self.quality_score()
        value = self.valuation_score()

        score = quality * 0.50 + value * 0.50
        return round(score, 2)

    def marks_risk_score(self):
        """
        Howard Marks style:
        avoid permanent loss of capital.
        Higher score = lower risk.
        """
        debt_score = score_range(
            self.m.debt_to_equity_percent,
            excellent=10,
            good=50,
            average=100,
            bad=200,
            higher_is_better=False,
            missing_score=50,
        )

        beta_score = score_range(
            self.m.beta,
            excellent=0.7,
            good=1.0,
            average=1.3,
            bad=1.7,
            higher_is_better=False,
            missing_score=50,
        )

        size_score = self.size_liquidity_score()

        red_flag_count = len(self.red_flags())
        red_flag_score = 100 if red_flag_count == 0 else max(20, 100 - red_flag_count * 20)

        score = debt_score * 0.30 + beta_score * 0.20 + size_score * 0.20 + red_flag_score * 0.30
        return round(score, 2)

    def india_compounder_score(self):
        """
        Indian QGLP / compounder style:
        quality, growth, longevity proxy, and price.
        """
        quality = self.quality_score()
        growth = self.growth_score()
        valuation = self.valuation_score()
        size = self.size_liquidity_score()

        score = quality * 0.35 + growth * 0.30 + size * 0.15 + valuation * 0.20
        return round(score, 2)

    def entry_score(self):
        """
        Buy/entry attractiveness:
        combines fair value margin, valuation, and 52-week price position.
        """
        price = self.m.price
        fair_value = self.estimated_fair_value()
        valuation = self.valuation_score()
        position = self.fifty_two_week_position()

        fair_value_score = 50

        if price and fair_value and fair_value > 0:
            discount = (fair_value - price) / fair_value

            if discount >= 0.35:
                fair_value_score = 100
            elif discount >= 0.20:
                fair_value_score = 80
            elif discount >= 0.05:
                fair_value_score = 65
            elif discount >= -0.10:
                fair_value_score = 50
            elif discount >= -0.25:
                fair_value_score = 35
            else:
                fair_value_score = 20

        position_score = score_range(
            position,
            excellent=0.25,
            good=0.45,
            average=0.65,
            bad=0.85,
            higher_is_better=False,
            missing_score=50,
        )

        score = fair_value_score * 0.50 + valuation * 0.30 + position_score * 0.20
        return round(score, 2)

    # -----------------------------
    # Red Flags and Style Match
    # -----------------------------

    def red_flags(self):
        flags = []

        if self.m.price is None:
            flags.append("Price missing")

        if self.m.market_cap_cr is None:
            flags.append("Market cap missing")
        elif self.m.market_cap_cr < 500:
            flags.append("Very small market cap below ₹500 crore")

        if self.m.trailing_pe is None:
            flags.append("P/E missing")
        elif self.m.trailing_pe <= 0:
            flags.append("Negative or invalid P/E")
        elif self.m.trailing_pe > 80:
            flags.append("Very expensive P/E above 80")

        if self.m.price_to_book is not None and self.m.price_to_book > 15:
            flags.append("Very expensive P/B above 15")

        if self.m.debt_to_equity_percent is not None and self.m.debt_to_equity_percent > 200:
            flags.append("Debt/equity above 200%")

        if self.m.roe_percent is not None and self.m.roe_percent < 8:
            flags.append("Weak ROE below 8%")

        if self.m.revenue_growth_percent is not None and self.m.revenue_growth_percent < 0:
            flags.append("Negative revenue growth")

        if self.m.earnings_growth_percent is not None and self.m.earnings_growth_percent < 0:
            flags.append("Negative earnings growth")

        if self.m.profit_margin_percent is not None and self.m.profit_margin_percent < 3:
            flags.append("Very low profit margin")

        if self.m.cash_conversion_ratio is not None and self.m.cash_conversion_ratio < 0.5:
            flags.append("Weak cash conversion")

        if self.m.beta is not None and self.m.beta > 1.8:
            flags.append("High beta / high volatility")

        return flags

    def investor_style_match(self, scores: Dict[str, float]):
        matches = []

        if scores["buffett_munger_score"] >= 75:
            matches.append("Buffett-Munger Quality")
        if scores["graham_value_score"] >= 75:
            matches.append("Graham/Klarman Value")
        if scores["lynch_garp_score"] >= 75:
            matches.append("Peter Lynch GARP")
        if scores["fisher_growth_score"] >= 75:
            matches.append("Philip Fisher Growth")
        if scores["greenblatt_score"] >= 75:
            matches.append("Greenblatt Quality+Value")
        if scores["india_compounder_score"] >= 75:
            matches.append("Indian QGLP Compounder")
        if scores["entry_score"] >= 75:
            matches.append("Attractive Entry Zone")

        if not matches:
            return "No strong investor-style match"

        return "; ".join(matches)

    # -----------------------------
    # Final Analysis
    # -----------------------------

    def analyze(self) -> QuantResult:
        quality = self.quality_score()
        growth = self.growth_score()
        valuation = self.valuation_score()
        size_liquidity = self.size_liquidity_score()

        buffett_munger = self.buffett_munger_score()
        graham_value = self.graham_value_score()
        lynch_garp = self.lynch_garp_score()
        fisher_growth = self.fisher_growth_score()
        greenblatt = self.greenblatt_score()
        marks_risk = self.marks_risk_score()
        india_compounder = self.india_compounder_score()
        entry = self.entry_score()

        # Master score combines proven investor frameworks.
        investor_master_score = (
            quality * 0.18
            + growth * 0.12
            + valuation * 0.12
            + entry * 0.16
            + buffett_munger * 0.12
            + lynch_garp * 0.08
            + fisher_growth * 0.08
            + greenblatt * 0.06
            + marks_risk * 0.04
            + india_compounder * 0.04
        )

        investor_master_score = round(investor_master_score, 2)

        # Keep quant_score same as investor_master_score for downstream LLM compatibility.
        quant_score = investor_master_score

        fair_value = self.estimated_fair_value()

        strong_buy_below = None
        accumulate_below = None
        expensive_above = None

        if fair_value:
            if quality >= 75:
                margin_of_safety = 0.20
            elif quality >= 60:
                margin_of_safety = 0.30
            else:
                margin_of_safety = 0.40

            strong_buy_below = round(fair_value * (1 - margin_of_safety), 2)
            accumulate_below = round(fair_value * (1 - margin_of_safety / 2), 2)
            expensive_above = round(fair_value * 1.20, 2)

        red_flags = self.red_flags()
        red_flag_text = "; ".join(red_flags) if red_flags else ""

        scores = {
            "buffett_munger_score": buffett_munger,
            "graham_value_score": graham_value,
            "lynch_garp_score": lynch_garp,
            "fisher_growth_score": fisher_growth,
            "greenblatt_score": greenblatt,
            "marks_risk_score": marks_risk,
            "india_compounder_score": india_compounder,
            "entry_score": entry,
        }

        style_match = self.investor_style_match(scores)

        price = self.m.price

        if red_flags and len(red_flags) >= 4:
            zone = "AVOID - MULTIPLE RED FLAGS"
        elif investor_master_score >= 80 and entry >= 70 and quality >= 70:
            if fair_value and price and strong_buy_below and price <= strong_buy_below:
                zone = "TOP CANDIDATE - STRONG BUY ZONE FOR QUALITATIVE CHECK"
            elif fair_value and price and accumulate_below and price <= accumulate_below:
                zone = "TOP CANDIDATE - ACCUMULATE ZONE FOR QUALITATIVE CHECK"
            else:
                zone = "TOP QUALITY WATCHLIST - WAIT FOR ENTRY"
        elif investor_master_score >= 72 and quality >= 65:
            zone = "HIGH PRIORITY QUALITATIVE CHECK"
        elif investor_master_score >= 65:
            zone = "WATCHLIST - QUALITATIVE CHECK OPTIONAL"
        elif investor_master_score >= 55:
            zone = "AVERAGE - TRACK ONLY"
        else:
            zone = "AVOID - LOW INVESTOR MASTER SCORE"

        notes = (
            "Investor-style free-data prototype. Uses yfinance/NSE data only. "
            "Validate using Screener, NSE/BSE filings, annual reports, concalls, promoter pledge, "
            "shareholding pattern, auditor notes, and sector-specific ratios before real investing. "
            "Banks, NBFCs and insurance companies need separate models."
        )

        return QuantResult(
            symbol=self.m.symbol,
            yahoo_symbol=self.m.yahoo_symbol,
            company_name=self.m.company_name,
            nse_company_name=self.m.nse_company_name,
            isin=self.m.isin,

            sector=self.m.sector,
            industry=self.m.industry,

            price=self.m.price,
            market_cap_cr=round(self.m.market_cap_cr, 2) if self.m.market_cap_cr else None,

            trailing_pe=self.m.trailing_pe,
            forward_pe=self.m.forward_pe,
            price_to_book=self.m.price_to_book,
            roe_percent=self.m.roe_percent,
            debt_to_equity_percent=self.m.debt_to_equity_percent,
            revenue_growth_percent=self.m.revenue_growth_percent,
            earnings_growth_percent=self.m.earnings_growth_percent,
            profit_margin_percent=self.m.profit_margin_percent,
            operating_margin_percent=self.m.operating_margin_percent,
            dividend_yield_percent=self.m.dividend_yield_percent,
            cash_conversion_ratio=self.m.cash_conversion_ratio,
            beta=self.m.beta,
            fifty_two_week_high=self.m.fifty_two_week_high,
            fifty_two_week_low=self.m.fifty_two_week_low,
            fifty_two_week_position=self.fifty_two_week_position(),

            quality_score=quality,
            growth_score=growth,
            valuation_score=valuation,
            size_liquidity_score=size_liquidity,

            buffett_munger_score=buffett_munger,
            graham_value_score=graham_value,
            lynch_garp_score=lynch_garp,
            fisher_growth_score=fisher_growth,
            greenblatt_score=greenblatt,
            marks_risk_score=marks_risk,
            india_compounder_score=india_compounder,
            entry_score=entry,

            quant_score=quant_score,
            investor_master_score=investor_master_score,

            estimated_fair_value=fair_value,
            strong_buy_below=strong_buy_below,
            accumulate_below=accumulate_below,
            expensive_above=expensive_above,

            quant_zone=zone,
            red_flags=red_flag_text,
            investor_style_match=style_match,
            notes=notes,
        )

# -----------------------------
# Main Pipeline
# -----------------------------

def run_pipeline(
    limit: Optional[int],
    sleep_seconds: float,
    output_file: str,
    shortlist_file: str,
    shortlist_top_n: int,
    min_quant_score: float,
    min_market_cap_cr: float,
    screener_fundamentals_file: str,
    use_screener_fundamentals: bool,
):
    print("Fetching NSE equity list...")
    universe = fetch_nse_equity_list()

    if limit:
        universe = universe.head(limit).reset_index(drop=True)
    else:
        universe = universe.reset_index(drop=True)

    print(f"Total stocks to process: {len(universe)}")

    all_results: List[Dict] = []

    for index, row in universe.iterrows():
        symbol = row["symbol"]
        nse_company_name = row.get("nse_company_name")
        isin = row.get("isin")

        print(f"[{index + 1}/{len(universe)}] Fetching {symbol}.NS...")

        metrics = fetch_yfinance_metrics(
            symbol=symbol,
            nse_company_name=nse_company_name,
            isin=isin,
        )

        if metrics.error:
            print(f"  Error: {metrics.error}")

        analyzer = QuantAnalyzer(metrics)
        result = analyzer.analyze()

        all_results.append(asdict(result))

        time.sleep(sleep_seconds)

    results_df = pd.DataFrame(all_results)

    if use_screener_fundamentals:
        results_df = merge_screener_fundamentals(
            results_df=results_df,
            screener_file=screener_fundamentals_file,
        )
    else:
        results_df["base_quant_score"] = results_df["quant_score"]
        results_df["enhanced_screener_score"] = None
        results_df["screener_data_available"] = False

    # Filter by minimum market cap
    if min_market_cap_cr and min_market_cap_cr > 0:
        before_count = len(results_df)

        results_df = results_df[
            results_df["market_cap_cr"].fillna(0) >= min_market_cap_cr
        ].copy()

        after_count = len(results_df)

        print(f"\nMarket cap filter applied: >= ₹{min_market_cap_cr:,.0f} Cr")
        print(f"Stocks before filter: {before_count}, after filter: {after_count}")

    if results_df.empty:
        print("\nNo stocks available after market cap filter.")
        results_df.to_csv(output_file, index=False)
        pd.DataFrame().to_csv(shortlist_file, index=False)
        return

    results_df = results_df.sort_values(
        by=[
            "investor_master_score",
            "entry_score",
            "quality_score",
            "growth_score",
            "valuation_score",
        ],
        ascending=False,
    )

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(shortlist_file).parent.mkdir(parents=True, exist_ok=True)

    results_df.to_csv(output_file, index=False)
    print(f"\nFull quantitative output saved to: {output_file}")

    shortlist_df = results_df[
        (results_df["quant_score"] >= min_quant_score)
        & (~results_df["quant_zone"].str.contains("AVOID", na=False))
    ].copy()

    shortlist_df = shortlist_df.head(shortlist_top_n)

    qualitative_columns = [
        "symbol",
        "yahoo_symbol",
        "company_name",
        "nse_company_name",
        "isin",
        "sector",
        "industry",
        "price",
        "market_cap_cr",
        "trailing_pe",
        "forward_pe",
        "price_to_book",
        "roe_percent",
        "debt_to_equity_percent",
        "revenue_growth_percent",
        "earnings_growth_percent",
        "profit_margin_percent",
        "operating_margin_percent",
        "dividend_yield_percent",
        "cash_conversion_ratio",
        "beta",
        "fifty_two_week_high",
        "fifty_two_week_low",
        "fifty_two_week_position",

        "quality_score",
        "growth_score",
        "valuation_score",
        "size_liquidity_score",

        "buffett_munger_score",
        "graham_value_score",
        "lynch_garp_score",
        "fisher_growth_score",
        "greenblatt_score",
        "marks_risk_score",
        "india_compounder_score",
        "entry_score",

        "base_quant_score",
        "enhanced_screener_score",
        "quant_score",
        "investor_master_score",

        "roce_percent",
        "roe_percent_screener",
        "sales_growth_5y",
        "profit_growth_5y",
        "promoter_holding",
        "promoter_holding_change_4q",
        "promoter_pledge_percent",
        "fii_holding",
        "dii_holding",
        "fii_holding_change_4q",
        "dii_holding_change_4q",
        "operating_cash_flow_5y",
        "capex_5y",
        "free_cash_flow_5y",
        "cash_conversion_5y",
        "fcf_calculation_method",
        "current_pe",
        "median_pe_5y",
        "price_to_median_pe",
        "latest_quarter_sales_growth",
        "latest_quarter_profit_growth",
        "quarterly_sales_consistency",
        "quarterly_profit_consistency",
        "data_completeness_score",
        "sector_scoring_group",
        "screener_data_available",

        "estimated_fair_value",
        "strong_buy_below",
        "accumulate_below",
        "expensive_above",

        "quant_zone",
        "investor_style_match",
        "red_flags",
    ]

    qualitative_columns = [
        col for col in qualitative_columns
        if col in shortlist_df.columns
    ]

    shortlist_df = shortlist_df[qualitative_columns]
    shortlist_df.to_csv(shortlist_file, index=False)

    print(f"Shortlist for Qualitative LLM Reader saved to: {shortlist_file}")

    print("\nTop shortlisted stocks:")
    if shortlist_df.empty:
        print("No stocks matched the shortlist criteria.")
    else:
        for _, row in shortlist_df.head(20).iterrows():
            print(
                f"{row['symbol']} | "
                f"Score: {row['quant_score']} | "
                f"Zone: {row['quant_zone']} | "
                f"Price: {row['price']} | "
                f"Fair Value: {row['estimated_fair_value']}"
            )

def main():
    parser = argparse.ArgumentParser(
        description="Free NSE Quantitative Fundamental Analysis Agent"
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of NSE stocks to test. Use 50 first. Use 0 for all stocks.",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Sleep seconds between yfinance calls to avoid rate limits.",
    )

    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR / "nse_quant_output.csv"),
        help="Full quantitative output CSV file.",
    )

    parser.add_argument(
        "--shortlist-output",
        default=str(DEFAULT_OUTPUT_DIR / "qualitative_llm_input.csv"),
        help="Shortlisted CSV file for Qualitative LLM Reader.",
    )

    parser.add_argument(
        "--shortlist-top-n",
        type=int,
        default=10,
        help="Maximum number of shortlisted stocks for qualitative analysis.",
    )

    parser.add_argument(
        "--min-quant-score",
        type=float,
        default=65,
        help="Minimum quant score required for qualitative shortlist.",
    )

    parser.add_argument(
        "--min-market-cap-cr",
        type=float,
        default=20000,
        help="Minimum market cap in crore. Default is 20000 Cr.",
    )

    parser.add_argument(
        "--screener-fundamentals",
        default=str(DEFAULT_SCREENER_FUNDAMENTALS_FILE),
        help="Path to data/input/screener_fundamentals.csv.",
    )

    parser.add_argument(
        "--disable-screener-merge",
        action="store_true",
        help="Disable merging Screener fundamentals into quant scoring.",
    )

    parser.add_argument(
        "--rerank-from-cache",
        action="store_true",
        help="Skip yfinance fetching and rerank from cached nse_quant_output.csv.",
    )

    parser.add_argument(
        "--quant-cache-input",
        default=str(DEFAULT_QUANT_CACHE_FILE),
        help="Cached quant output CSV to use with --rerank-from-cache.",
    )

    args = parser.parse_args()

    limit = None if args.limit == 0 else args.limit

    if args.rerank_from_cache:
        rerank_from_cache(
            quant_cache_file=args.quant_cache_input,
            screener_fundamentals_file=args.screener_fundamentals,
            output_file=args.output,
            shortlist_file=args.shortlist_output,
            shortlist_top_n=args.shortlist_top_n,
            min_quant_score=args.min_quant_score,
            min_market_cap_cr=args.min_market_cap_cr,
            use_screener_fundamentals=not args.disable_screener_merge,
        )
        return

    run_pipeline(
        limit=limit,
        sleep_seconds=args.sleep,
        output_file=args.output,
        shortlist_file=args.shortlist_output,
        shortlist_top_n=args.shortlist_top_n,
        min_quant_score=args.min_quant_score,
        min_market_cap_cr=args.min_market_cap_cr,
        screener_fundamentals_file=args.screener_fundamentals,
        use_screener_fundamentals=not args.disable_screener_merge,
    )


if __name__ == "__main__":
    main()