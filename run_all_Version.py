import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

SRC_DIR = ROOT / "src"
DATA_DIR = ROOT / "data"
INPUT_DIR = DATA_DIR / "input"   # stores screener_fundamentals.csv
OUTPUT_DIR = DATA_DIR / "output"
DOCS_DIR = DATA_DIR / "docs"
REPORTS_DIR = ROOT / "reports" / "qualitative"


def run_command(command):
    print("\nRunning:")
    print(" ".join(command))
    print("-" * 80)

    result = subprocess.run(command, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed: {' '.join(command)}")


def main():
    parser = argparse.ArgumentParser(
        description="Run full Indian Stock AI Agent pipeline."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Number of NSE stocks to process. Use 0 for all.",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Sleep between yfinance requests.",
    )

    parser.add_argument(
        "--screener-sleep",
        type=float,
        default=3.0,
        help="Sleep between Screener.in requests. Keep 2-5 seconds to be polite.",
    )

    parser.add_argument(
        "--min-market-cap-cr",
        type=float,
        default=20000,
        help="Minimum market cap in crore. Default 20000.",
    )

    parser.add_argument(
        "--min-quant-score",
        type=float,
        default=65,
        help="Minimum quant score for final shortlist.",
    )

    parser.add_argument(
        "--screener-candidates",
        type=int,
        default=50,
        help=(
            "Number of candidates sent to Screener enrichment after the initial "
            "yfinance scan. Should be larger than --shortlist-top-n so that the "
            "reranking step has enough candidates to choose from."
        ),
    )

    parser.add_argument(
        "--shortlist-top-n",
        type=int,
        default=10,
        help="Final top N stocks sent to qualitative LLM analysis.",
    )

    parser.add_argument(
        "--skip-screener-collect",
        action="store_true",
        help="Skip the Screener fundamentals collection step.",
    )

    parser.add_argument(
        "--skip-doc-download",
        action="store_true",
        help="Skip Screener document download step.",
    )

    parser.add_argument(
        "--skip-qualitative",
        action="store_true",
        help="Skip qualitative LLM analysis step.",
    )

    parser.add_argument(
        "--llm-sleep",
        type=float,
        default=None,
        help=(
            "Sleep between LLM API calls in seconds. "
            "If not set, uses a provider-aware default: Ollama=1, Groq=3, Gemini=5. "
            "Increase this if you hit rate limit errors on the cloud free tiers."
        ),
    )

    parser.add_argument(
        "--skip-report",
        action="store_true",
        help="Skip the final PNG report generation step.",
    )

    parser.add_argument(
        "--resume-qualitative",
        action="store_true",
        help=(
            "Resume qualitative LLM step: skip stocks already successfully analyzed "
            "in the existing output CSV. Use this after a rate-limit error to continue "
            "where you left off without re-spending tokens on completed stocks."
        ),
    )

    args = parser.parse_args()

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    quant_output = OUTPUT_DIR / "nse_quant_output.csv"
    screener_fundamentals = INPUT_DIR / "screener_fundamentals.csv"
    qualitative_input = OUTPUT_DIR / "qualitative_llm_input.csv"
    qualitative_output = OUTPUT_DIR / "qualitative_llm_output.csv"
    docs_summary = OUTPUT_DIR / "downloaded_docs_summary.csv"
    final_report_png = OUTPUT_DIR / "final_investor_report.png"

    python_exe = sys.executable

    # Step 1: Quant analysis (yfinance only, no Screener merge yet).
    # Use --screener-candidates as the shortlist size so there are enough
    # candidates for Screener enrichment to select from.
    run_command([
        python_exe,
        str(SRC_DIR / "nse_free_quant_agent.py"),
        "--limit",
        str(args.limit),
        "--sleep",
        str(args.sleep),
        "--output",
        str(quant_output),
        "--shortlist-output",
        str(qualitative_input),
        "--shortlist-top-n",
        str(args.screener_candidates),
        "--min-quant-score",
        str(args.min_quant_score),
        "--min-market-cap-cr",
        str(args.min_market_cap_cr),
        "--disable-screener-merge",
    ])

    # Step 2: Collect Screener fundamentals for the initial candidate shortlist.
    if not args.skip_screener_collect:
        run_command([
            python_exe,
            str(SRC_DIR / "screener_fundamentals_collector.py"),
            "--input",
            str(qualitative_input),
            "--output",
            str(screener_fundamentals),
            "--limit",
            str(args.screener_candidates),
            "--sleep",
            str(args.screener_sleep),
            "--overwrite",
        ])

    # Step 3: Rerank using Screener data and produce final shortlist.
    run_command([
        python_exe,
        str(SRC_DIR / "nse_free_quant_agent.py"),
        "--rerank-from-cache",
        "--quant-cache-input",
        str(quant_output),
        "--screener-fundamentals",
        str(screener_fundamentals),
        "--output",
        str(quant_output),
        "--shortlist-output",
        str(qualitative_input),
        "--shortlist-top-n",
        str(args.shortlist_top_n),
        "--min-quant-score",
        str(args.min_quant_score),
        "--min-market-cap-cr",
        str(args.min_market_cap_cr),
    ] + (["--disable-screener-merge"] if args.skip_screener_collect else []))

    # Step 4: Download documents from Screener for the final shortlist.
    if not args.skip_doc_download:
        run_command([
            python_exe,
            str(SRC_DIR / "download_screener_docs.py"),
            "--input",
            str(qualitative_input),
            "--output-dir",
            str(DOCS_DIR),
            "--limit",
            str(args.shortlist_top_n),
            "--sleep",
            "3",
            "--summary-output",
            str(docs_summary),
        ])

    # Step 5: Qualitative LLM analysis.
    if not args.skip_qualitative:
        qualitative_cmd = [
            python_exe,
            str(SRC_DIR / "qualitative_llm_reader.py"),
            "--input",
            str(qualitative_input),
            "--output",
            str(qualitative_output),
            "--docs-dir",
            str(DOCS_DIR),
            "--reports-dir",
            str(REPORTS_DIR),
            "--limit",
            str(args.shortlist_top_n),
        ]
        # Pass explicit sleep only when the user set it; otherwise let
        # qualitative_llm_reader pick the provider-aware default from .env.
        if args.llm_sleep is not None:
            qualitative_cmd += ["--sleep", str(args.llm_sleep)]
        if args.resume_qualitative:
            qualitative_cmd += ["--resume"]
        run_command(qualitative_cmd)

    # Step 6: Generate final PNG investor report.
    if not args.skip_qualitative and not args.skip_report:
        run_command([
            python_exe,
            str(SRC_DIR / "print_finallist.py"),
            "--input",
            str(qualitative_output),
            "--output",
            str(final_report_png),
            "--top-n",
            str(args.shortlist_top_n),
        ])

    print("\nPipeline completed successfully.")
    print(f"Quant output:          {quant_output}")
    print(f"Screener fundamentals: {screener_fundamentals}")
    print(f"Qualitative input:     {qualitative_input}")
    print(f"Qualitative output:    {qualitative_output}")
    print(f"Reports folder:        {REPORTS_DIR}")
    if not args.skip_qualitative and not args.skip_report:
        print(f"Final PNG report:      {final_report_png}")


if __name__ == "__main__":
    main()