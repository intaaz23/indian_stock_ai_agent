import os
import re
import json
import time
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


# -----------------------------
# Configuration
# -----------------------------

DEFAULT_DOCS_DIR = "data/docs"
DEFAULT_REPORTS_DIR = "reports/qualitative"
DEFAULT_INPUT_FILE = "data/output/qualitative_llm_input.csv"
DEFAULT_OUTPUT_FILE = "data/output/qualitative_llm_output.csv"

MAX_DOCUMENT_CHARS = 9_000
# MAX_CHARS_PER_FILE is chosen so that at least 2-3 documents can contribute
# meaningful content before hitting MAX_DOCUMENT_CHARS (9000 / 3000 = 3x).
# These values are tuned for Groq free tier which has an 8000 TPM hard limit
# per request. Indian financial documents tokenise at ~2.4 chars/token, so
# 9000 chars ≈ 3750 tokens for documents + ~1650 for the prompt template
# = ~5400 tokens total — comfortably under the 8000 TPM ceiling.
MAX_CHARS_PER_FILE = 3_000
SLEEP_BETWEEN_CALLS = 1.0


# -----------------------------
# Utility Functions
# -----------------------------

def clean_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_float(value, default=None):
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_str(value, default=""):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    return str(value)


def extract_json_from_text(text: str) -> Dict:
    """
    Tries to parse JSON from LLM response.
    Handles plain JSON or JSON inside markdown fences.
    """
    if not text:
        raise ValueError("Empty LLM response")

    text = text.strip()

    # Remove markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text.strip()).strip()

    # Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Extract first JSON object
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("Could not parse JSON from LLM response")


def truncate_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    head = text[: int(max_chars * 0.65)]
    tail = text[-int(max_chars * 0.35):]

    return (
        head
        + "\n\n...[DOCUMENT TRUNCATED FOR TOKEN LIMIT]...\n\n"
        + tail
    )


# -----------------------------
# Document Reader
# -----------------------------

def read_txt_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        try:
            return path.read_text(encoding="latin-1", errors="ignore")
        except Exception:
            return ""


def read_pdf_file(path: Path) -> str:
    if PdfReader is None:
        return ""

    try:
        reader = PdfReader(str(path))
        pages = []

        for i, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(f"\n\n--- Page {i + 1} ---\n{page_text}")
            except Exception:
                continue

        return "\n".join(pages)

    except Exception:
        return ""


def load_company_documents(symbol: str, docs_dir: str) -> Tuple[str, List[str]]:
    """
    Loads .txt, .md, and .pdf files from data/docs/SYMBOL/.
    """
    company_dir = Path(docs_dir) / symbol

    if not company_dir.exists():
        return "", []

    supported_extensions = {".txt", ".md", ".pdf"}
    files = [
        path for path in company_dir.iterdir()
        if path.is_file() and path.suffix.lower() in supported_extensions
    ]

    all_parts = []
    used_files = []

    for file_path in sorted(files):
        suffix = file_path.suffix.lower()

        if suffix in {".txt", ".md"}:
            text = read_txt_file(file_path)
        elif suffix == ".pdf":
            text = read_pdf_file(file_path)
        else:
            text = ""

        text = clean_text(text)

        if not text:
            continue

        text = truncate_text(text, MAX_CHARS_PER_FILE)

        all_parts.append(
            f"\n\n==============================\n"
            f"DOCUMENT: {file_path.name}\n"
            f"==============================\n"
            f"{text}"
        )

        used_files.append(str(file_path))

    combined = clean_text("\n".join(all_parts))
    combined = truncate_text(combined, MAX_DOCUMENT_CHARS)

    return combined, used_files


# -----------------------------
# Prompt Builder
# -----------------------------

def build_qualitative_prompt(row: Dict, document_text: str, used_files: List[str]) -> str:
    symbol = safe_str(row.get("symbol"))
    company_name = safe_str(row.get("company_name")) or safe_str(row.get("nse_company_name"))
    sector = safe_str(row.get("sector"))
    industry = safe_str(row.get("industry"))

    has_documents = bool(used_files)

    if has_documents:
        doc_instruction = (
            "Analyze the company primarily from the documents provided below. "
            "Cite only information present in the documents or widely known, "
            "publicly verifiable facts (e.g., known management names from official "
            "company filings or regulatory disclosures). "
            "Do NOT invent specific financial figures, contract values, or "
            "governance details that are not grounded in the provided text."
        )
        doc_section = document_text
    else:
        doc_instruction = (
            "NO company documents were provided. "
            "Your analysis must be speculative and based only on the raw financial "
            "metrics above and general sector/industry knowledge. "
            "You MUST set confidence_level to 'Low'. "
            "Use the word 'speculative' in business_model_summary, management_quality, "
            "and reasoning_summary. "
            "Do NOT invent management names, contract details, or specific historical facts."
        )
        doc_section = "No documents provided."

    prompt = f"""
You are a senior Indian equity research analyst.

Your job:
Analyze the qualitative strength of the company using the raw financial metrics
and any documents provided. Your qualitative_score must reflect business quality,
management quality, moat, and governance — NOT the pre-computed quant scores.

Important rules:
1. Do not give investment advice.
2. {doc_instruction}
3. Do NOT base qualitative_score on the quant_score or any pre-computed score
   already shown below. The quantitative scores are provided as context only.
   Score business quality independently.
4. Focus on business model, management quality, moat, risks, governance, and
   earnings durability.
5. The final output must be valid JSON only.
6. Do not wrap JSON in markdown.
7. Do not include any text outside JSON.

Company:
- Symbol: {symbol}
- Company name: {company_name}
- Sector: {sector}
- Industry: {industry}

Raw financial metrics (for context — do NOT echo these as qualitative findings):
- Price: {row.get("price")}
- Market cap crore: {row.get("market_cap_cr")}
- Trailing P/E: {row.get("trailing_pe")}
- Price to book: {row.get("price_to_book")}
- ROE %: {row.get("roe_percent")}
- Debt/equity %: {row.get("debt_to_equity_percent")}
- Revenue growth %: {row.get("revenue_growth_percent")}
- Earnings growth %: {row.get("earnings_growth_percent")}
- Profit margin %: {row.get("profit_margin_percent")}
- Operating margin %: {row.get("operating_margin_percent")}
- Red flags from quant analysis: {row.get("red_flags")}

Documents used:
{json.dumps(used_files, indent=2)}

Document text:
{doc_section}

Analyze using this framework:

1. Business model quality
2. Revenue drivers
3. Industry attractiveness
4. Competitive advantage / moat
5. Management quality
6. Capital allocation
7. Balance sheet and debt risk
8. Cash flow quality
9. Margin durability
10. Corporate governance red flags
11. Cyclicality risk
12. Regulatory risk
13. Customer concentration or product concentration risk
14. Bull case
15. Bear case
16. Key things to verify manually
17. Final qualitative score from 0 to 100

Scoring guide:
- 85 to 100: Excellent business, strong moat, clean governance, durable growth
- 70 to 84: Good business, acceptable risks, worth deeper tracking
- 55 to 69: Average business, needs caution
- 40 to 54: Weak business or major uncertainty
- Below 40: Avoid due to serious qualitative concerns

Return valid JSON exactly in this schema:

{{
  "symbol": "{symbol}",
  "company_name": "{company_name}",
  "qualitative_score": 0,
  "qualitative_rating": "Excellent / Good / Average / Weak / Avoid",
  "confidence_level": "High / Medium / Low",
  "business_model_summary": "",
  "revenue_drivers": [],
  "industry_view": "",
  "competitive_advantages": [],
  "management_quality": "",
  "capital_allocation": "",
  "balance_sheet_view": "",
  "cash_flow_quality": "",
  "margin_quality": "",
  "governance_red_flags": [],
  "business_risks": [],
  "bull_case": [],
  "bear_case": [],
  "manual_verification_required": [],
  "qualitative_decision": "Proceed to final ranking / Watchlist only / Avoid",
  "reasoning_summary": ""
}}
"""
    return prompt.strip()


# -----------------------------
# LLM Client
# Supports: Ollama (local), Groq Cloud, Google Gemini Flash, OpenAI
# All three use the OpenAI-compatible API — only .env changes required.
# -----------------------------

def detect_provider(base_url: str) -> str:
    """Returns a human-readable provider name from the base URL."""
    if not base_url:
        return "OpenAI"
    if "11434" in base_url or "ollama" in base_url.lower():
        return "Ollama (local)"
    if "groq.com" in base_url:
        return "Groq Cloud"
    if "googleapis.com" in base_url:
        return "Google Gemini"
    return f"Custom ({base_url})"


def create_llm_client() -> Tuple[OpenAI, str]:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY", "ollama")
    model = os.getenv("OPENAI_MODEL", "qwen2.5:7b")
    base_url = os.getenv("OPENAI_BASE_URL")

    if base_url:
        client = OpenAI(api_key=api_key, base_url=base_url)
    else:
        client = OpenAI(api_key=api_key)

    provider = detect_provider(base_url or "")
    print(f"LLM provider : {provider}")
    print(f"LLM model    : {model}")

    return client, model


def recommended_sleep_for_provider(base_url: str) -> float:
    """
    Returns a safe default sleep (seconds) between LLM calls based on provider.
    These are conservative defaults for free-tier rate limits:
      - Ollama: no limit, 1s is fine
      - Groq  : ~30 RPM on free tier  → 3s between calls
      - Gemini: 15 RPM on free tier   → 5s between calls
    Can be overridden via LLM_SLEEP env var or --sleep CLI arg.
    """
    env_override = os.getenv("LLM_SLEEP")
    if env_override:
        try:
            return float(env_override)
        except ValueError:
            pass

    if not base_url:
        return 1.0
    if "groq.com" in base_url:
        return 3.0
    if "googleapis.com" in base_url:
        return 5.0
    return 1.0


def call_llm(client: OpenAI, model: str, prompt: str) -> Dict:
    response = client.chat.completions.create(
    model=model,
    temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a careful Indian equity research analyst. "
                    "Return only valid JSON. "
                    "Do NOT hallucinate or invent facts. "
                    "Only state what is grounded in the provided documents or widely "
                    "known, publicly verifiable facts about the company. "
                    "When documents are absent or incomplete, explicitly mark confidence "
                    "as Low and flag all key assessments as speculative."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    content = response.choices[0].message.content
    return extract_json_from_text(content)


# -----------------------------
# Report Writer
# -----------------------------

def write_markdown_report(report_data: Dict, reports_dir: str):
    Path(reports_dir).mkdir(parents=True, exist_ok=True)

    symbol = report_data.get("symbol", "UNKNOWN")
    company_name = report_data.get("company_name", "")

    path = Path(reports_dir) / f"{symbol}_qualitative_report.md"

    def bullet_list(items):
        if not items:
            return "- None noted\n"
        if isinstance(items, str):
            return f"- {items}\n"
        return "\n".join([f"- {item}" for item in items]) + "\n"

    content = f"""# Qualitative Research Report: {symbol}

Company: {company_name}

## Final View

- Qualitative score: {report_data.get("qualitative_score")}
- Rating: {report_data.get("qualitative_rating")}
- Confidence: {report_data.get("confidence_level")}
- Decision: {report_data.get("qualitative_decision")}

## Business Model Summary

{report_data.get("business_model_summary", "")}

## Revenue Drivers

{bullet_list(report_data.get("revenue_drivers"))}

## Industry View

{report_data.get("industry_view", "")}

## Competitive Advantages

{bullet_list(report_data.get("competitive_advantages"))}

## Management Quality

{report_data.get("management_quality", "")}

## Capital Allocation

{report_data.get("capital_allocation", "")}

## Balance Sheet View

{report_data.get("balance_sheet_view", "")}

## Cash Flow Quality

{report_data.get("cash_flow_quality", "")}

## Margin Quality

{report_data.get("margin_quality", "")}

## Governance Red Flags

{bullet_list(report_data.get("governance_red_flags"))}

## Business Risks

{bullet_list(report_data.get("business_risks"))}

## Bull Case

{bullet_list(report_data.get("bull_case"))}

## Bear Case

{bullet_list(report_data.get("bear_case"))}

## Manual Verification Required

{bullet_list(report_data.get("manual_verification_required"))}

## Reasoning Summary

{report_data.get("reasoning_summary", "")}

---

Disclaimer: This report is generated for research and education only. It is not financial advice.
"""

    path.write_text(content, encoding="utf-8")


# -----------------------------
# Final Score Combiner
# -----------------------------

def calculate_final_score(
    quant_score: Optional[float],
    qualitative_score: Optional[float],
    confidence_level: Optional[str] = None,
    has_documents: bool = False,
) -> Optional[float]:
    """
    Combines quant and qualitative scores.

    The qualitative weight is scaled by confidence and document availability:
    - High confidence with documents: 35% qualitative weight
    - Medium confidence with documents: 25%
    - Low confidence (or no documents): 10%

    This prevents a speculative, document-free LLM score from having
    the same influence as a well-evidenced, high-confidence analysis.
    """
    if quant_score is None or qualitative_score is None:
        return None

    if not has_documents or confidence_level == "Low":
        qual_weight = 0.10
    elif confidence_level == "Medium":
        qual_weight = 0.25
    elif confidence_level == "High":
        qual_weight = 0.35
    else:
        # Unknown or None confidence_level: treat conservatively as Medium.
        qual_weight = 0.25

    quant_weight = 1.0 - qual_weight
    final_score = quant_score * quant_weight + qualitative_score * qual_weight
    return round(final_score, 2)


def final_decision(row: Dict) -> str:
    quant_score = safe_float(row.get("quant_score"))
    qualitative_score = safe_float(row.get("qualitative_score"))
    valuation_score = safe_float(row.get("valuation_score"))
    red_flags = safe_str(row.get("red_flags"))
    governance_red_flags = safe_str(row.get("governance_red_flags"))

    final_score = safe_float(row.get("final_score"))

    if qualitative_score is not None and qualitative_score < 45:
        return "AVOID - WEAK QUALITATIVE PROFILE"

    if governance_red_flags and governance_red_flags not in ["[]", ""]:
        if "serious" in governance_red_flags.lower() or "fraud" in governance_red_flags.lower():
            return "AVOID - GOVERNANCE CONCERN"

    if red_flags and "MULTIPLE" in safe_str(row.get("quant_zone")).upper():
        return "AVOID - QUANT RED FLAGS"

    if final_score is None:
        return "INSUFFICIENT DATA"

    if final_score >= 80 and valuation_score and valuation_score >= 60:
        return "HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS"

    if final_score >= 72:
        return "GOOD WATCHLIST - WAIT FOR RIGHT VALUATION"

    if final_score >= 62:
        return "TRACK ONLY - NEED MORE CONFIRMATION"

    return "AVOID / LOW PRIORITY"


# -----------------------------
# Main Pipeline
# -----------------------------

def run_qualitative_pipeline(
    input_file: str,
    output_file: str,
    docs_dir: str,
    reports_dir: str,
    limit: Optional[int],
    sleep_seconds: Optional[float],
):
    load_dotenv()
    client, model = create_llm_client()

    # If caller did not provide an explicit sleep, use the provider-aware default.
    if sleep_seconds is None:
        sleep_seconds = recommended_sleep_for_provider(
            os.getenv("OPENAI_BASE_URL", "")
        )
        print(f"LLM sleep    : {sleep_seconds}s (provider default — override with --sleep or LLM_SLEEP)")

    df = pd.read_csv(input_file)

    if limit:
        df = df.head(limit)

    results = []

    print(f"Loaded {len(df)} shortlisted stocks from {input_file}")
    print(f"Using model: {model}")

    for idx, row in df.iterrows():
        row_dict = row.to_dict()

        symbol = safe_str(row_dict.get("symbol")).strip()

        print(f"\n[{idx + 1}/{len(df)}] Qualitative analysis for {symbol}...")

        document_text, used_files = load_company_documents(symbol, docs_dir)

        if used_files:
            print(f"  Loaded {len(used_files)} document(s).")
        else:
            print("  No documents found. Running low-confidence qualitative check.")

        prompt = build_qualitative_prompt(row_dict, document_text, used_files)

        try:
            llm_result = call_llm(client, model, prompt)

            qualitative_score = safe_float(llm_result.get("qualitative_score"))
            quant_score = safe_float(row_dict.get("quant_score"))
            confidence_level = safe_str(llm_result.get("confidence_level"))

            final_score = calculate_final_score(
                quant_score,
                qualitative_score,
                confidence_level=confidence_level,
                has_documents=bool(used_files),
            )

            combined = {
                **row_dict,
                **llm_result,
                "documents_used": "; ".join(used_files),
                "has_documents": bool(used_files),
                "final_score": final_score,
            }

            # Convert list fields to JSON strings for CSV safety
            for key, value in list(combined.items()):
                if isinstance(value, list) or isinstance(value, dict):
                    combined[key] = json.dumps(value, ensure_ascii=False)

            combined["final_decision"] = final_decision(combined)

            results.append(combined)

            write_markdown_report(llm_result, reports_dir)

            print(
                f"  Score: {combined.get('qualitative_score')} | "
                f"Rating: {combined.get('qualitative_rating')} | "
                f"Final Score: {combined.get('final_score')} | "
                f"Decision: {combined.get('final_decision')}"
            )

        except Exception as e:
            print(f"  Error analyzing {symbol}: {e}")

            failed = {
                **row_dict,
                "qualitative_score": None,
                "qualitative_rating": "Error",
                "confidence_level": "Low",
                "final_score": None,
                "final_decision": "ERROR - MANUAL REVIEW REQUIRED",
                "error": str(e),
            }

            results.append(failed)

        time.sleep(sleep_seconds)

    output_df = pd.DataFrame(results)

    if not output_df.empty and "final_score" in output_df.columns:
        output_df = output_df.sort_values(
            by=["final_score", "quant_score"],
            ascending=False,
            na_position="last",
        )
    sort_columns = [
        col for col in [
            "final_score",
            "qualitative_score",
            "quant_score",
        ]
        if col in output_df.columns
    ]

    for col in sort_columns:
        output_df[col] = pd.to_numeric(output_df[col], errors="coerce")

    if sort_columns:
        output_df = output_df.sort_values(
            by=sort_columns,
            ascending=False,
            na_position="last",
        )
    output_df.to_csv(output_file, index=False)

    print(f"\nQualitative output saved to: {output_file}")
    print(f"Markdown reports saved to: {reports_dir}")

    print("\nTop final results:")
    display_df = output_df.copy()

    for col in ["final_score", "qualitative_score", "quant_score"]:
        if col in display_df.columns:
            display_df[col] = pd.to_numeric(display_df[col], errors="coerce")

    sort_columns = [
        col for col in [
            "final_score",
            "qualitative_score",
            "quant_score",
        ]
        if col in display_df.columns
    ]

    if sort_columns:
        display_df = display_df.sort_values(
            by=sort_columns,
            ascending=False,
            na_position="last",
        )

    print("\nTop 20 stocks after qualitative analysis:")

    for _, row in display_df.head(20).iterrows():
        print(
            f"{row.get('symbol')} | "
            f"Quant: {row.get('quant_score')} | "
            f"Qual: {row.get('qualitative_score')} | "
            f"Final: {row.get('final_score')} | "
            f"{row.get('final_decision')}"
        )

def main():
    parser = argparse.ArgumentParser(
        description="Qualitative LLM Reader for Indian Stock Analysis"
    )

    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_FILE,
        help="Input CSV from quantitative screener.",
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help="Output CSV with qualitative analysis.",
    )

    parser.add_argument(
        "--docs-dir",
        default=DEFAULT_DOCS_DIR,
        help="Folder containing company documents by symbol.",
    )

    parser.add_argument(
        "--reports-dir",
        default=DEFAULT_REPORTS_DIR,
        help="Folder to save markdown qualitative reports.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of shortlisted stocks to analyze. Use 0 for all.",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=None,
        help=(
            "Sleep between LLM calls in seconds. "
            "If not set, uses a provider-aware default (Ollama=1, Groq=3, Gemini=5). "
            "Can also be set via LLM_SLEEP env var in .env."
        ),
    )

    args = parser.parse_args()

    limit = None if args.limit == 0 else args.limit

    run_qualitative_pipeline(
        input_file=args.input,
        output_file=args.output,
        docs_dir=args.docs_dir,
        reports_dir=args.reports_dir,
        limit=limit,
        sleep_seconds=args.sleep,
    )


if __name__ == "__main__":
    main()