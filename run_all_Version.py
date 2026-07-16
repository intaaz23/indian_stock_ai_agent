import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

SRC_DIR = ROOT / "src"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
DOCS_DIR = DATA_DIR / "docs"
REPORTS_DIR = ROOT / "reports" / "qualitative"


def run_command(command):
    print("\nRunning:")
    print(" ".join(command))
    print("-" * 80)

    result = subprocess.run(command)

    if result.returncode != 0:
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
        help="Sleep between yfinance/Screener requests.",
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
        help="Minimum quant score for shortlist.",
    )

    parser.add_argument(
        "--shortlist-top-n",
        type=int,
        default=10,
        help="Top N stocks to send to qualitative analysis.",
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

    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    quant_output = OUTPUT_DIR / "nse_quant_output.csv"
    qualitative_input = OUTPUT_DIR / "qualitative_llm_input.csv"
    qualitative_output = OUTPUT_DIR / "qualitative_llm_output.csv"
    docs_summary = OUTPUT_DIR / "downloaded_docs_summary.csv"

    python_exe = sys.executable

    # Step 1: Quant analysis
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
        str(args.shortlist_top_n),
        "--min-quant-score",
        str(args.min_quant_score),
        "--min-market-cap-cr",
        str(args.min_market_cap_cr),
    ])

    # Step 2: Download documents from Screener
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

    # Step 3: Qualitative LLM analysis
    if not args.skip_qualitative:
        run_command([
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
        ])

    print("\nPipeline completed successfully.")
    print(f"Quant output: {quant_output}")
    print(f"Qualitative input: {qualitative_input}")
    print(f"Qualitative output: {qualitative_output}")
    print(f"Reports folder: {REPORTS_DIR}")


if __name__ == "__main__":
    main()