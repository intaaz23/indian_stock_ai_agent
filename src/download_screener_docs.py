import re
import time
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.screener.in"
DEFAULT_INPUT = "qualitative_llm_input.csv"
DEFAULT_OUTPUT_DIR = "data/docs"


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
}


# -----------------------------
# Utility functions
# -----------------------------

def clean_symbol(symbol: str) -> str:
    symbol = str(symbol).strip().upper()
    symbol = symbol.replace(".NS", "")
    return symbol


def extract_year(text: str):
    """
    Extracts likely year from link text or URL.
    Example:
    - Financial Year 2025
    - annual-report-2024
    """
    if not text:
        return None

    years = re.findall(r"\b(20[0-3][0-9])\b", text)
    if not years:
        return None

    return max(int(y) for y in years)


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    # First request helps Screener/NSE/BSE redirects by setting cookies/session.
    try:
        session.get(BASE_URL, timeout=20)
    except Exception:
        pass

    return session


def get_company_page(session: requests.Session, symbol: str) -> str:
    """
    Fetch Screener company page.
    Tries standalone and consolidated pages.
    """
    urls = [
        f"{BASE_URL}/company/{symbol}/",
        f"{BASE_URL}/company/{symbol}/consolidated/",
    ]

    for url in urls:
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 200 and "Screener" in response.text:
                return response.text
        except Exception:
            continue

    return ""


# -----------------------------
# Link extraction and scoring
# -----------------------------

def extract_links_from_page(html: str):
    """
    Extract all links from Screener page.

    We capture:
    - anchor text
    - parent text
    - href
    - full URL
    - year

    Parent text is important because Screener links may show short text like:
    'PDF', 'from bse', 'from nse', etc.
    """
    soup = BeautifulSoup(html, "html.parser")

    links = []

    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = a["href"].strip()

        if not href:
            continue

        parent = a.find_parent(["li", "tr", "div", "p"])
        parent_text = ""
        if parent:
            parent_text = " ".join(parent.get_text(" ", strip=True).split())

        full_url = urljoin(BASE_URL, href)

        combined_text = f"{text} {parent_text} {href} {full_url}"

        links.append({
            "text": text,
            "parent_text": parent_text,
            "combined_text": combined_text,
            "url": full_url,
            "href": href,
            "year": extract_year(combined_text),
        })

    return links


def score_annual_report_link(link):
    """
    Higher score = better annual report candidate.

    Prefer:
    - actual annual report links
    - Financial Year links
    - latest year
    - PDF/download links

    Penalize:
    - letters
    - AGM notices
    - web-link notices
    - newspaper notices
    - regulation announcements
    - old annual reports
    """
    combined = link["combined_text"].lower()
    url = link["url"].lower()

    score = 0

    # Strong positive signals
    if "annual report" in combined:
        score += 140

    if "financial year" in combined:
        score += 130

    if "annual-report" in combined or "annual_reports" in combined:
        score += 120

    if "annual" in combined and "report" in combined:
        score += 100

    if ".pdf" in url or "pdf" in combined:
        score += 40

    if "from bse" in combined:
        score += 25

    if "from nse" in combined:
        score += 25

    if "download" in combined:
        score += 15

    # Prefer latest year strongly — high weight so recency dominates over text keywords
    current_year = datetime.now().year
    year = link.get("year")
    if year:
        score += (year - 2000) * 20
        # Progressive penalty for every year older than (current_year - 1)
        if year < current_year - 1:
            score -= (current_year - 1 - year) * 60

    # Penalize non-annual-report documents
    negative_words = [
        "letter",
        "weblink",
        "web-link",
        "web link",
        "notice",
        "agm notice",
        "postal ballot",
        "outcome",
        "newspaper",
        "intimation",
        "regulation 30",
        "regulation 47",
        "book closure",
        "record date",
        "scrutinizer",
        "voting",
        "evoting",
        "e-voting",
        "proceedings",
        "advertisement",
    ]

    for word in negative_words:
        if word in combined:
            score -= 90

    return score


def score_presentation_link(link):
    """
    Higher score = better investor presentation candidate.
    """
    combined = link["combined_text"].lower()
    url = link["url"].lower()

    score = 0

    if "investor presentation" in combined:
        score += 140

    if "presentation" in combined:
        score += 100

    if "ppt" in combined:
        score += 70

    if "results presentation" in combined:
        score += 80

    if ".pdf" in url or "pdf" in combined:
        score += 35

    if "from bse" in combined or "from nse" in combined:
        score += 20

    current_year = datetime.now().year
    year = link.get("year")
    if year:
        score += (year - 2000) * 20
        if year < current_year - 1:
            score -= (current_year - 1 - year) * 60

    negative_words = [
        "transcript",
        "concall",
        "conference call",
        "annual report",
        "shareholding",
        "newspaper",
        "notice",
        "agm",
    ]

    for word in negative_words:
        if word in combined:
            score -= 80

    return score


def score_concall_link(link):
    """
    Higher score = better concall/transcript candidate.
    Prefer transcripts over audio/meet announcements.
    """
    combined = link["combined_text"].lower()
    url = link["url"].lower()

    score = 0

    if "transcript" in combined:
        score += 150

    if "concall" in combined:
        score += 130

    if "conference call" in combined:
        score += 120

    if "earnings call" in combined:
        score += 110

    if "call transcript" in combined:
        score += 140

    if "analyst" in combined and "meet" in combined:
        score += 60

    if ".pdf" in url or "pdf" in combined:
        score += 30

    if "from bse" in combined or "from nse" in combined:
        score += 20

    current_year = datetime.now().year
    year = link.get("year")
    if year:
        score += (year - 2000) * 20
        if year < current_year - 1:
            score -= (current_year - 1 - year) * 60

    negative_words = [
        "annual report",
        "shareholding",
        "newspaper",
        "postal ballot",
        "agm",
        "notice",
    ]

    for word in negative_words:
        if word in combined:
            score -= 80

    return score


def categorize_links(links):
    """
    Categorize and sort candidate links by quality score.
    """
    annual_candidates = []
    presentation_candidates = []
    concall_candidates = []

    for link in links:
        combined = link["combined_text"].lower()

        # Annual reports
        if (
            "annual report" in combined
            or "annual-report" in combined
            or "annual_reports" in combined
            or "financial year" in combined
            or ("annual" in combined and "report" in combined)
        ):
            annual_candidates.append(link)

        # Investor presentations
        if (
            "presentation" in combined
            or "investor presentation" in combined
            or "investor-presentation" in combined
            or "ppt" in combined
            or "results presentation" in combined
        ):
            presentation_candidates.append(link)

        # Concalls / transcripts
        if (
            "concall" in combined
            or "conference call" in combined
            or "transcript" in combined
            or "earnings call" in combined
            or "call transcript" in combined
            or ("analyst" in combined and "meet" in combined)
        ):
            concall_candidates.append(link)

    annual_candidates = sorted(
        annual_candidates,
        key=score_annual_report_link,
        reverse=True,
    )

    presentation_candidates = sorted(
        presentation_candidates,
        key=score_presentation_link,
        reverse=True,
    )

    concall_candidates = sorted(
        concall_candidates,
        key=score_concall_link,
        reverse=True,
    )

    return {
        "annual_report": annual_candidates,
        "investor_presentation": presentation_candidates,
        "concall": concall_candidates,
    }


def choose_latest_link(candidates):
    """
    Among the top-3 highest-scored candidates, prefer the one with the latest year.
    This ensures a clearly better keyword-matched link is not overridden, while
    still breaking ties in favour of the most recent document.
    """
    if not candidates:
        return None

    # candidates are already sorted by score descending
    top_cluster = candidates[:3]

    # Among the top cluster, prefer latest year
    with_year = [c for c in top_cluster if c.get("year")]
    if with_year:
        return max(with_year, key=lambda c: c["year"])

    return top_cluster[0]


# -----------------------------
# Download handling
# -----------------------------

def looks_like_pdf(response: requests.Response, url: str) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()

    return (
        "application/pdf" in content_type
        or url.lower().endswith(".pdf")
        or response.content[:4] == b"%PDF"
    )


def html_to_text(content: bytes) -> str:
    try:
        html = content.decode("utf-8", errors="ignore")
    except Exception:
        html = str(content)

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def download_document(session, url: str, output_path_without_ext: Path):
    """
    Downloads PDF if possible.
    If URL returns HTML, saves readable text as .txt.
    """
    try:
        response = session.get(url, timeout=60, allow_redirects=True)
        response.raise_for_status()

        if looks_like_pdf(response, response.url):
            output_path = output_path_without_ext.with_suffix(".pdf")
            output_path.write_bytes(response.content)
            return str(output_path), "pdf"

        text = html_to_text(response.content)

        if not text:
            return None, "empty"

        output_path = output_path_without_ext.with_suffix(".txt")
        output_path.write_text(text, encoding="utf-8", errors="ignore")
        return str(output_path), "txt"

    except Exception as e:
        return None, f"error: {e}"


def write_debug_links(company_dir: Path, categories: dict):
    """
    Saves ranked candidate links for manual verification.
    """
    debug_path = company_dir / "screener_links_found.txt"

    with debug_path.open("w", encoding="utf-8") as f:
        for category, category_links in categories.items():
            f.write(f"\n\n## {category}\n")

            for item in category_links[:25]:
                if category == "annual_report":
                    score = score_annual_report_link(item)
                elif category == "investor_presentation":
                    score = score_presentation_link(item)
                else:
                    score = score_concall_link(item)

                f.write(
                    f"score={score} | "
                    f"year={item.get('year')} | "
                    f"text={item.get('text')} | "
                    f"parent={item.get('parent_text')} | "
                    f"url={item.get('url')}\n"
                )


def _existing_doc_is_fresh(path_stem: Path, max_age_days: int) -> bool:
    """Return True if a .pdf or .txt file at path_stem exists and is < max_age_days old."""
    if max_age_days <= 0:
        return False
    for suffix in (".pdf", ".txt"):
        candidate = path_stem.with_suffix(suffix)
        if candidate.exists():
            age_days = (datetime.now().timestamp() - candidate.stat().st_mtime) / 86400
            if age_days < max_age_days:
                return True
    return False


def download_docs_for_symbol(
    session,
    symbol: str,
    output_dir: str,
    sleep: float,
    skip_fresh_days: int = 0,
):
    symbol = clean_symbol(symbol)

    company_dir = Path(output_dir) / symbol
    company_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nProcessing {symbol}...")

    # Skip the entire symbol if all three documents are already fresh.
    if skip_fresh_days > 0:
        doc_stems = [
            company_dir / "annual_report_latest",
            company_dir / "investor_presentation_latest",
            company_dir / "latest_concall",
        ]
        if all(_existing_doc_is_fresh(s, skip_fresh_days) for s in doc_stems):
            print(
                f"  All documents are fresh (< {skip_fresh_days} days old), "
                f"skipping {symbol}."
            )
            result: dict = {"symbol": symbol, "status": "skipped_fresh"}
            for key, stem in zip(
                ["annual_report", "investor_presentation", "concall"], doc_stems
            ):
                result[f"{key}_status"] = "skipped_fresh"
                for suffix in (".pdf", ".txt"):
                    if stem.with_suffix(suffix).exists():
                        result[key] = str(stem.with_suffix(suffix))
                        break
                else:
                    result[key] = ""
            return result

    html = get_company_page(session, symbol)

    if not html:
        print(f"  Could not fetch Screener page for {symbol}")
        return {
            "symbol": symbol,
            "annual_report": "",
            "investor_presentation": "",
            "concall": "",
            "status": "company page not found",
        }

    links = extract_links_from_page(html)
    categories = categorize_links(links)

    write_debug_links(company_dir, categories)

    annual_link = choose_latest_link(categories["annual_report"])
    presentation_link = choose_latest_link(categories["investor_presentation"])
    concall_link = choose_latest_link(categories["concall"])

    result = {
        "symbol": symbol,
        "annual_report": "",
        "investor_presentation": "",
        "concall": "",
        "status": "ok",
    }

    # Annual report
    if annual_link:
        score = score_annual_report_link(annual_link)
        print(
            f"  Annual report selected: "
            f"score={score}, year={annual_link.get('year')}, "
            f"text={annual_link.get('text') or annual_link.get('parent_text')}"
        )

        path, status = download_document(
            session,
            annual_link["url"],
            company_dir / "annual_report_latest",
        )

        result["annual_report"] = path or ""
        result["annual_report_status"] = status
        time.sleep(sleep)
    else:
        print("  Annual report not found on Screener page.")
        result["annual_report_status"] = "not found"

    # Investor presentation
    if presentation_link:
        score = score_presentation_link(presentation_link)
        print(
            f"  Investor presentation selected: "
            f"score={score}, year={presentation_link.get('year')}, "
            f"text={presentation_link.get('text') or presentation_link.get('parent_text')}"
        )

        path, status = download_document(
            session,
            presentation_link["url"],
            company_dir / "investor_presentation_latest",
        )

        result["investor_presentation"] = path or ""
        result["investor_presentation_status"] = status
        time.sleep(sleep)
    else:
        print("  Investor presentation not found on Screener page.")
        result["investor_presentation_status"] = "not found"

    # Concall / transcript
    if concall_link:
        score = score_concall_link(concall_link)
        print(
            f"  Concall/transcript selected: "
            f"score={score}, year={concall_link.get('year')}, "
            f"text={concall_link.get('text') or concall_link.get('parent_text')}"
        )

        path, status = download_document(
            session,
            concall_link["url"],
            company_dir / "latest_concall",
        )

        result["concall"] = path or ""
        result["concall_status"] = status
        time.sleep(sleep)
    else:
        print("  Concall/transcript not found on Screener page.")
        result["concall_status"] = "not found"

    # Manual notes placeholder
    notes_path = company_dir / "notes.txt"
    if not notes_path.exists():
        notes_path.write_text(
            f"Manual notes for {symbol}\n\n"
            "Add business model notes, risks, management observations, "
            "industry notes, and manual verification here.\n",
            encoding="utf-8",
        )

    return result


# -----------------------------
# Main
# -----------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download latest annual report, investor presentation, and concall/transcript "
            "documents from public Screener links."
        )
    )

    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Input CSV file, usually qualitative_llm_input.csv",
    )

    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Output folder, default data/docs",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of stocks from input CSV to process.",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=3.0,
        help="Delay between requests in seconds. Keep 2-5 seconds to be polite.",
    )

    parser.add_argument(
        "--summary-output",
        default="downloaded_docs_summary.csv",
        help="CSV summary of downloaded documents.",
    )

    parser.add_argument(
        "--skip-fresh-days",
        type=int,
        default=0,
        help=(
            "Skip re-downloading documents for a symbol if all its docs already exist "
            "and were downloaded within this many days. 0 = always re-download (default)."
        ),
    )

    args = parser.parse_args()

    df = pd.read_csv(args.input)

    if "symbol" not in df.columns:
        raise ValueError("Input CSV must contain a 'symbol' column.")

    df = df.head(args.limit)

    session = create_session()

    results = []

    print(f"Processing top {len(df)} stocks from {args.input}")

    for _, row in df.iterrows():
        symbol = clean_symbol(row["symbol"])

        result = download_docs_for_symbol(
            session=session,
            symbol=symbol,
            output_dir=args.output_dir,
            sleep=args.sleep,
            skip_fresh_days=args.skip_fresh_days,
        )

        results.append(result)

        time.sleep(args.sleep)

    summary_df = pd.DataFrame(results)
    summary_df.to_csv(args.summary_output, index=False)

    print(f"\nDone. Summary saved to: {args.summary_output}")
    print(f"Documents saved under: {args.output_dir}")


if __name__ == "__main__":
    main()