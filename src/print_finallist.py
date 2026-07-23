from pathlib import Path
import argparse
import pandas as pd
import matplotlib.pyplot as plt

# =========================
# Configuration
# =========================
BASE_DIR = Path(r"C:\Intaaz_Work\GitHubCopilot\indian_stock_ai_agent\data\output")
INPUT_CSV = BASE_DIR / "qualitative_llm_output.csv"
OUTPUT_PNG = BASE_DIR / "final_investor_report.png"

MIN_CONFIDENCE = 0.0
MIN_DATA_COMPLETENESS = 0.0
EXCLUDE_DECISIONS = set()
TOP_N = 20


def to_float(x):
    try:
        if pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


def pick_zone(price, strong_buy_below, accumulate_below, expensive_above):
    if price is None:
        return "Unknown"
    if strong_buy_below is not None and price <= strong_buy_below:
        return "Strong Buy"
    if accumulate_below is not None and price <= accumulate_below:
        return "Accumulate"
    if expensive_above is not None and price >= expensive_above:
        return "Expensive"
    return "Watch"


def main():
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"Input CSV not found: {INPUT_CSV}")

    df = pd.read_csv(INPUT_CSV)

    numeric_cols = [
        "final_score", "confidence_level", "data_completeness_score",
        "price", "market_cap_cr", "roe_percent_screener", "roe_percent",
        "roce_percent", "debt_to_equity_percent", "current_pe", "median_pe_5y",
        "strong_buy_below", "accumulate_below", "expensive_above",
        "estimated_fair_value", "quant_score", "qualitative_score",
        "sales_growth_5y", "promoter_holding", "free_cash_flow_5y"
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "final_decision" in df.columns:
        df["final_decision"] = df["final_decision"].fillna("").astype(str).str.strip()

    # filter
    filtered = df.copy()
    if "confidence_level" in filtered.columns:
        filtered = filtered[filtered["confidence_level"].fillna(0) >= MIN_CONFIDENCE]
    if "data_completeness_score" in filtered.columns:
        filtered = filtered[filtered["data_completeness_score"].fillna(0) >= MIN_DATA_COMPLETENESS]
    if "final_decision" in filtered.columns and len(EXCLUDE_DECISIONS) > 0:
        filtered = filtered[~filtered["final_decision"].isin(EXCLUDE_DECISIONS)]

    # fallback
    if len(filtered) == 0:
        print("⚠️ No rows after filtering. Falling back to full dataset.")
        filtered = df.copy()

    # sort
    sort_cols = [c for c in ["final_score", "confidence_level", "data_completeness_score"] if c in filtered.columns]
    if sort_cols:
        filtered = filtered.sort_values(sort_cols, ascending=False)

    top = filtered.head(TOP_N).copy()

    # zone
    top["zone"] = top.apply(
        lambda r: pick_zone(
            to_float(r.get("price")),
            to_float(r.get("strong_buy_below")),
            to_float(r.get("accumulate_below")),
            to_float(r.get("expensive_above")),
        ),
        axis=1
    )

    # Upside %
    def calc_upside(row):
        p = to_float(row.get("price"))
        fv = to_float(row.get("estimated_fair_value"))
        if p is None or fv is None or p == 0:
            return None
        return ((fv - p) / p) * 100

    top["upside_pct"] = top.apply(calc_upside, axis=1)

    # NOTE: Company column removed intentionally
    selected_cols = [
        "symbol", "sector", "price", "market_cap_cr",
        "final_score",
        "quant_score", "qualitative_score",
        "roe_percent_screener" if "roe_percent_screener" in top.columns else "roe_percent",
        "roce_percent", "debt_to_equity_percent",
        "current_pe", "promoter_holding", "sales_growth_5y",
        "free_cash_flow_5y",   # <-- added
        "upside_pct", "zone"
    ]
    selected_cols = [c for c in selected_cols if c in top.columns]

    report_df = top[selected_cols].copy()

    # format numeric columns
    for c in report_df.columns:
        if pd.api.types.is_numeric_dtype(report_df[c]):
            if c == "upside_pct":
                report_df[c] = report_df[c].map(lambda x: "" if pd.isna(x) else f"{x:.1f}%")
            else:
                report_df[c] = report_df[c].map(lambda x: "" if pd.isna(x) else f"{x:.2f}")

    report_df = report_df.rename(columns={
        "symbol": "Symbol",
        "sector": "Sector",
        "price": "Price",
        "market_cap_cr": "MCap(Cr)",
        "final_score": "FinalScore",
        "quant_score": "Quant",
        "qualitative_score": "Qual",
        "roe_percent_screener": "ROE%",
        "roe_percent": "ROE%",
        "roce_percent": "ROCE%",
        "debt_to_equity_percent": "D/E%",
        "current_pe": "PE",
        "promoter_holding": "Promoter%",
        "sales_growth_5y": "SalesGr5Y%",
        "free_cash_flow_5y": "FCF5Y",
        "upside_pct": "Upside%",
        "zone": "Zone"
    })

    # better sizing for clarity
    rows, cols = report_df.shape
    fig_w = max(20, cols * 1.35)
    fig_h = max(8, rows * 0.52 + 2.5)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")

    title = f"Indian Investor Final List (Top {len(report_df)} of {len(filtered)} filtered | Total input: {len(df)})"
    plt.title(title, fontsize=16, pad=14, weight="bold")

    table = ax.table(
        cellText=report_df.values,
        colLabels=report_df.columns,
        loc="center",
        cellLoc="center",
    )

    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1, 1.55)

    # styling
    for (r, c), cell in table.get_celld().items():
        if r == 0:
            cell.set_text_props(weight="bold", color="white")
            cell.set_facecolor("#1f4e78")
        else:
            cell.set_facecolor("#f7fbff" if r % 2 == 0 else "#ffffff")

    # color Zone
    if "Zone" in report_df.columns:
        z_idx = list(report_df.columns).index("Zone")
        for r in range(1, rows + 1):
            z = str(report_df.iloc[r - 1, z_idx]).lower()
            cell = table[(r, z_idx)]
            if "strong buy" in z:
                cell.set_facecolor("#c6efce")
            elif "accumulate" in z:
                cell.set_facecolor("#ffeb9c")
            elif "expensive" in z:
                cell.set_facecolor("#ffc7ce")

    # color Upside%
    if "Upside%" in report_df.columns:
        u_idx = list(report_df.columns).index("Upside%")
        for r in range(1, rows + 1):
            txt = str(report_df.iloc[r - 1, u_idx]).replace("%", "").strip()
            try:
                val = float(txt)
                cell = table[(r, u_idx)]
                if val >= 20:
                    cell.set_facecolor("#c6efce")  # strong upside
                elif val < 0:
                    cell.set_facecolor("#ffc7ce")  # downside risk
            except:
                pass

    OUTPUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(OUTPUT_PNG, dpi=320, bbox_inches="tight")  # higher dpi for less blur
    plt.close()

    print(f" PNG report generated: {OUTPUT_PNG}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate final investor report PNG from qualitative LLM output."
    )
    parser.add_argument(
        "--input",
        default=str(INPUT_CSV),
        help="Path to qualitative_llm_output.csv.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PNG),
        help="Path to save the output PNG report.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=TOP_N,
        help="Number of top stocks to include in the report.",
    )
    cli = parser.parse_args()
    INPUT_CSV = Path(cli.input)
    OUTPUT_PNG = Path(cli.output)
    TOP_N = cli.top_n
    main()