import argparse
import math
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup


# -----------------------------
# Paths / Config
# -----------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_INPUT_FILE = PROJECT_ROOT / "data" / "output" / "qualitative_llm_input.csv"
DEFAULT_OUTPUT_FILE = PROJECT_ROOT / "data" / "input" / "screener_fundamentals.csv"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "cache" / "screener" / "html"

SCREENER_BASE_URL = "https://www.screener.in"
# 7 days keeps data within a single quarterly reporting cycle.
CACHE_DAYS = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SECTOR_OVERRIDES = {
    "COALINDIA": "commodity_cyclical",
    "HINDZINC": "commodity_cyclical",
    "NMDC": "commodity_cyclical",
    "VEDL": "commodity_cyclical",

    "INDIANB": "bank",
    "MAHABANK": "bank",
    "KARURVYSYA": "bank",

    "MUTHOOTFIN": "nbfc",

    "HEROMOTOCO": "auto",
    "GODFRYPHLP": "fmcg",
}

# -----------------------------
# Utility Functions
# -----------------------------

def clean_symbol(symbol: str) -> str:
    if symbol is None:
        return ""
    symbol = str(symbol).strip().upper()
    symbol = symbol.replace(".NS", "")
    return symbol


def normalize_label(text: str) -> str:
    if not text:
        return ""
    text = text.replace("+", " ")
    text = text.replace(":", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def parse_number(value) -> Optional[float]:
    """
    Converts Screener text values into float.

    Handles:
    - 12.5%
    - 1,234
    - -45
    - (45)
    - ₹ 1,000 Cr.
    - —
    """
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    bad_values = {"-", "—", "–", "nan", "none", "null", "nil"}
    if text.lower() in bad_values:
        return None

    text = text.replace("\u2212", "-")
    text = text.replace(",", "")
    text = text.replace("%", "")
    text = text.replace("₹", "")
    text = text.replace("Cr.", "")
    text = text.replace("Cr", "")
    text = text.strip()

    is_negative_parentheses = text.startswith("(") and text.endswith(")")
    if is_negative_parentheses:
        text = text[1:-1]

    match = re.search(r"-?\d+(\.\d+)?", text)
    if not match:
        return None

    try:
        number = float(match.group(0))
        if is_negative_parentheses:
            number = -number
        if math.isnan(number):
            return None
        return number
    except Exception:
        return None


def safe_round(value, digits=2):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def last_numeric(values: List) -> Optional[float]:
    nums = [parse_number(v) for v in values]
    nums = [v for v in nums if v is not None]
    if not nums:
        return None
    return nums[-1]


def last_n_numeric(values: List, n: int = 5) -> List[float]:
    nums = [parse_number(v) for v in values]
    nums = [v for v in nums if v is not None]
    return nums[-n:]


def pct_growth(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    return ((current - previous) / abs(previous)) * 100


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get(SCREENER_BASE_URL, timeout=20)
    except Exception:
        pass

    return session


# -----------------------------
# Screener Fetch + Cache
# -----------------------------

def cache_file_path(symbol: str, cache_dir: Path) -> Path:
    return cache_dir / f"{symbol}.html"


def is_cache_fresh(path: Path, cache_days: int) -> bool:
    if not path.exists():
        return False

    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - modified <= timedelta(days=cache_days)


def fetch_screener_html(
    session: requests.Session,
    symbol: str,
    cache_dir: Path,
    cache_days: int,
    refresh: bool,
    sleep_seconds: float,
) -> Tuple[str, str, bool]:
    """
    Returns:
    - html
    - url used
    - used_cache
    """
    symbol = clean_symbol(symbol)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_file_path(symbol, cache_dir)

    if not refresh and is_cache_fresh(cache_path, cache_days):
        return cache_path.read_text(encoding="utf-8", errors="ignore"), "cache", True

    urls = [
        f"{SCREENER_BASE_URL}/company/{symbol}/consolidated/",
        f"{SCREENER_BASE_URL}/company/{symbol}/",
    ]

    for url in urls:
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 200 and "Screener" in response.text:
                cache_path.write_text(response.text, encoding="utf-8", errors="ignore")
                time.sleep(sleep_seconds)
                return response.text, url, False
        except Exception:
            continue

    return "", "", False


# -----------------------------
# HTML Parsing Helpers
# -----------------------------

def parse_top_ratios(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """
    Parses top ratio cards like:
    Market Cap, Current Price, Stock P/E, ROCE, ROE, etc.
    """
    ratios = {}

    top = soup.select_one("#top-ratios")
    if not top:
        return ratios

    for li in top.select("li"):
        name_el = li.select_one(".name")
        value_el = li.select_one(".number") or li.select_one(".value")

        if not name_el:
            spans = li.find_all("span")
            if spans:
                name_el = spans[0]
            if len(spans) > 1 and not value_el:
                value_el = spans[-1]

        if not name_el:
            continue

        name = normalize_label(name_el.get_text(" ", strip=True))
        value_text = value_el.get_text(" ", strip=True) if value_el else li.get_text(" ", strip=True)

        ratios[name] = parse_number(value_text)

    return ratios


def find_section(soup: BeautifulSoup, section_id: str):
    section = soup.find(id=section_id)
    if section:
        return section

    # fallback by heading text
    for heading in soup.find_all(["h2", "h3"]):
        if section_id.replace("-", " ") in heading.get_text(" ", strip=True).lower():
            return heading.find_parent("section") or heading.parent

    return None


def parse_table_rows(section) -> Dict[str, List[str]]:
    """
    Parses the first table inside a section into:
    {
      "sales": ["100", "120", ...],
      "net profit": [...]
    }
    """
    rows = {}

    if section is None:
        return rows

    table = section.find("table")
    if table is None:
        return rows

    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue

        label = normalize_label(cells[0].get_text(" ", strip=True))
        values = [cell.get_text(" ", strip=True) for cell in cells[1:]]

        if label:
            rows[label] = values

    return rows


def find_row(rows: Dict[str, List[str]], possible_names: List[str]) -> List[str]:
    """
    Finds a row from parsed Screener table rows.

    Priority:
    1. Exact normalized match
    2. Partial contains match
    """
    normalized_names = [normalize_label(name) for name in possible_names]

    # Exact match first
    for key, values in rows.items():
        normalized_key = normalize_label(key)
        if normalized_key in normalized_names:
            return values

    # Partial match fallback
    for key, values in rows.items():
        normalized_key = normalize_label(key)
        for name in normalized_names:
            if name in normalized_key or normalized_key in name:
                return values

    return []


def extract_growth_value(soup: BeautifulSoup, block_title: str, period: str = "5 years") -> Optional[float]:
    """
    Extracts values from blocks like:
    Compounded Sales Growth
    10 Years: ...
    5 Years: ...
    3 Years: ...
    TTM: ...
    """
    title_regex = re.compile(re.escape(block_title), re.IGNORECASE)

    # Method 1: heading followed by table
    for text_node in soup.find_all(string=title_regex):
        parent = text_node.parent
        table = parent.find_next("table") if parent else None

        if table:
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) >= 2:
                    label = normalize_label(cells[0].get_text(" ", strip=True))
                    if period.lower() in label:
                        return parse_number(cells[1].get_text(" ", strip=True))

    # Method 2: regex on full page text
    full_text = soup.get_text(" ", strip=True)
    pattern = (
        re.escape(block_title)
        + r".{0,500}?"
        + re.escape(period)
        + r"\s*:?\s*(-?\d+(\.\d+)?)\s*%"
    )
    match = re.search(pattern, full_text, flags=re.IGNORECASE)
    if match:
        return parse_number(match.group(1))

    return None


# -----------------------------
# Field Extraction
# -----------------------------

def extract_shareholding(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    section = find_section(soup, "shareholding")
    rows = parse_table_rows(section)

    promoters = find_row(rows, ["promoters", "promoter"])
    fii = find_row(rows, ["fiis", "fii"])
    dii = find_row(rows, ["diis", "dii"])
    pledge = find_row(rows, ["pledged", "pledge"])

    promoter_values = last_n_numeric(promoters, 8)
    fii_values = last_n_numeric(fii, 8)
    dii_values = last_n_numeric(dii, 8)
    pledge_values = last_n_numeric(pledge, 8)

    def change_4q(values):
        if len(values) >= 5:
            return values[-1] - values[-5]
        return None

    return {
        "promoter_holding": safe_round(promoter_values[-1] if promoter_values else None),
        "promoter_holding_change_4q": safe_round(change_4q(promoter_values)),
        "promoter_pledge_percent": safe_round(pledge_values[-1] if pledge_values else None),
        "fii_holding": safe_round(fii_values[-1] if fii_values else None),
        "dii_holding": safe_round(dii_values[-1] if dii_values else None),
        "fii_holding_change_4q": safe_round(change_4q(fii_values)),
        "dii_holding_change_4q": safe_round(change_4q(dii_values)),
    }

def extract_cash_flow_history(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """
    Extracts 5-year operating cash flow, capex/free-cash-flow proxy, and cash conversion.

    Preferred method:
    - FCF = Cash from Operating Activity - abs(Fixed assets purchased)

    Fallback method:
    - If Screener does not expose 'Fixed assets purchased',
      use Cash from Investing Activity as a rough proxy:
      FCF proxy = CFO + Cash from Investing Activity

    Note:
    - Investing cash flow includes investments/acquisitions, so fallback is not pure capex.
    - For banks/NBFCs, FCF is generally not meaningful.
    """
    cash_section = find_section(soup, "cash-flow")
    cash_rows = parse_table_rows(cash_section)

    profit_section = find_section(soup, "profit-loss")
    profit_rows = parse_table_rows(profit_section)

    cfo_values = find_row(
        cash_rows,
        [
            "cash from operating activity",
            "cash from operations",
            "operating activity",
            "operating cash flow",
            "cash flow from operating activities",
        ],
    )

    capex_values = find_row(
        cash_rows,
        [
            "fixed assets purchased",
            "purchase of fixed assets",
            "capital expenditure",
            "capex",
            "fixed assets",
            "purchase of property plant and equipment",
            "purchase of ppe",
        ],
    )

    investing_values = find_row(
        cash_rows,
        [
            "cash from investing activity",
            "cash from investing activities",
            "investing activity",
            "investing activities",
            "net cash from investing activity",
        ],
    )

    net_profit_values = find_row(
        profit_rows,
        [
            "net profit",
            "profit after tax",
            "pat",
        ],
    )

    cfo_5y = last_n_numeric(cfo_values, 5)
    capex_5y = last_n_numeric(capex_values, 5)
    investing_5y = last_n_numeric(investing_values, 5)
    net_profit_5y = last_n_numeric(net_profit_values, 5)

    cfo_sum = sum(cfo_5y) if cfo_5y else None
    net_profit_sum = sum(net_profit_5y) if net_profit_5y else None

    capex_sum = None
    free_cash_flow_sum = None
    fcf_method = None

    # Preferred method: direct capex row
    if cfo_sum is not None and capex_5y:
        capex_sum = sum(abs(x) for x in capex_5y)
        free_cash_flow_sum = cfo_sum - capex_sum
        fcf_method = "direct_capex"

    # Fallback method: use investing cash flow as a conservative proxy.
    # Investing CF includes acquisitions and financial investments beyond pure capex,
    # so we apply a 60% discount to avoid overstating the maintenance capex burden.
    elif cfo_sum is not None and investing_5y:
        investing_sum = sum(investing_5y)
        # Investing CF is usually negative; take 60% of the outflow as capex proxy.
        capex_estimate = abs(investing_sum) * 0.60
        free_cash_flow_sum = cfo_sum - capex_estimate
        capex_sum = capex_estimate
        fcf_method = "investing_cash_flow_proxy_discounted"

    cash_conversion = None
    if cfo_sum is not None and net_profit_sum not in [None, 0]:
        cash_conversion = cfo_sum / net_profit_sum

    return {
        "operating_cash_flow_5y": safe_round(cfo_sum),
        "capex_5y": safe_round(capex_sum),
        "free_cash_flow_5y": safe_round(free_cash_flow_sum),
        "cash_conversion_5y": safe_round(cash_conversion, 3),
        "fcf_calculation_method": fcf_method,
    }

def extract_quarterly_trend(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    quarter_section = find_section(soup, "quarters")
    rows = parse_table_rows(quarter_section)

    sales_values = last_n_numeric(find_row(rows, ["sales", "revenue"]), 12)
    profit_values = last_n_numeric(find_row(rows, ["net profit", "profit after tax"]), 12)

    def latest_yoy_growth(values):
        if len(values) >= 5:
            return pct_growth(values[-1], values[-5])
        return None

    def consistency_score(values):
        """
        Checks last 4 possible YoY comparisons.
        Returns % of positive YoY growth readings.
        """
        if len(values) < 8:
            return None

        comparisons = []
        for i in range(len(values) - 4, len(values)):
            current = values[i]
            previous = values[i - 4]
            growth = pct_growth(current, previous)
            if growth is not None:
                comparisons.append(growth > 0)

        if not comparisons:
            return None

        return sum(comparisons) / len(comparisons) * 100

    return {
        "latest_quarter_sales_growth": safe_round(latest_yoy_growth(sales_values)),
        "latest_quarter_profit_growth": safe_round(latest_yoy_growth(profit_values)),
        "quarterly_sales_consistency": safe_round(consistency_score(sales_values)),
        "quarterly_profit_consistency": safe_round(consistency_score(profit_values)),
    }


def extract_valuation_history(soup: BeautifulSoup, top_ratios: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    current_pe = (
        top_ratios.get("stock p/e")
        or top_ratios.get("p/e")
        or top_ratios.get("current pe")
    )

    ratios_section = find_section(soup, "ratios")
    rows = parse_table_rows(ratios_section)

    stock_pe_values = find_row(
        rows,
        [
            "stock p/e",
            "p/e",
            "price to earning",
        ],
    )

    pe_history = last_n_numeric(stock_pe_values, 5)

    median_pe_5y = median(pe_history) if pe_history else None

    price_to_median_pe = None
    if current_pe is not None and median_pe_5y not in [None, 0]:
        price_to_median_pe = current_pe / median_pe_5y

    return {
        "current_pe": safe_round(current_pe),
        "median_pe_5y": safe_round(median_pe_5y),
        "price_to_median_pe": safe_round(price_to_median_pe, 3),
    }


def extract_bank_metrics(soup: BeautifulSoup, top_ratios: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """
    Extracts banking-specific metrics from Screener for bank/NBFC/insurance stocks.

    Screener surfaces NIM, Gross NPA, Net NPA, and CASA in the top-ratios block
    for financial companies. CAR is extracted from the annual ratios table when
    present.

    Returns None for each field if Screener does not expose it for the company.
    """
    nim = (
        top_ratios.get("net interest margin")
        or top_ratios.get("nim")
        or top_ratios.get("net interest margin %")
        or top_ratios.get("net interest margin(%)")
    )

    gross_npa = (
        top_ratios.get("gross npa")
        or top_ratios.get("gnpa")
        or top_ratios.get("gross npa %")
        or top_ratios.get("gross npa(%)")
    )

    net_npa = (
        top_ratios.get("net npa")
        or top_ratios.get("nnpa")
        or top_ratios.get("net npa %")
        or top_ratios.get("net npa(%)")
    )

    casa = (
        top_ratios.get("casa")
        or top_ratios.get("casa %")
        or top_ratios.get("casa ratio")
    )

    # CAR is less commonly surfaced in top-ratios; try the ratios table too.
    car = (
        top_ratios.get("car")
        or top_ratios.get("capital adequacy ratio")
        or top_ratios.get("capital adequacy ratio %")
    )
    if car is None:
        ratios_section = find_section(soup, "ratios")
        ratios_rows = parse_table_rows(ratios_section)
        car_values = find_row(
            ratios_rows,
            ["capital adequacy ratio", "car", "crar"],
        )
        car = last_numeric(car_values)

    return {
        "net_interest_margin": safe_round(nim),
        "gross_npa_percent": safe_round(gross_npa),
        "net_npa_percent": safe_round(net_npa),
        "casa_ratio": safe_round(casa),
        "capital_adequacy_ratio": safe_round(car),
    }


def classify_sector_group(row_data: Dict, page_text: str) -> str:
    symbol = clean_symbol(row_data.get("symbol"))

    if symbol in SECTOR_OVERRIDES:
        return SECTOR_OVERRIDES[symbol]

    text_parts = [
        str(row_data.get("sector", "")),
        str(row_data.get("industry", "")),
        page_text[:3000],
    ]

    text = " ".join(text_parts).lower()

    if any(word in text for word in ["bank", "banking"]):
        return "bank"

    if any(word in text for word in ["nbfc", "finance", "financing", "housing finance"]):
        return "nbfc"

    if "insurance" in text:
        return "insurance"

    if any(word in text for word in ["steel", "metal", "mining", "coal", "oil", "gas", "commodity"]):
        return "commodity_cyclical"

    if any(word in text for word in ["shipping", "port", "logistics", "transport"]):
        return "shipping_logistics"

    if any(word in text for word in ["hotel", "travel", "tourism", "restaurant"]):
        return "hotel_travel"

    if any(word in text for word in ["real estate", "realty", "developer"]):
        return "real_estate"

    if any(word in text for word in ["pharma", "pharmaceutical", "drug", "healthcare"]):
        return "pharma"

    if any(word in text for word in ["it services", "software", "technology", "information technology"]):
        return "it_services"

    if any(word in text for word in ["fmcg", "consumer goods", "personal care", "foods"]):
        return "fmcg"

    if any(word in text for word in ["power", "electricity", "utility", "transmission"]):
        return "utility"

    return "general_non_financial"


def calculate_data_completeness(row: Dict) -> float:
    important_fields = [
        "roce_percent",
        "sales_growth_5y",
        "profit_growth_5y",
        "promoter_holding",
        "promoter_pledge_percent",
        "fii_holding",
        "dii_holding",
        "free_cash_flow_5y",
        "cash_conversion_5y",
        "current_pe",
        "median_pe_5y",
        "latest_quarter_sales_growth",
        "latest_quarter_profit_growth",
    ]

    available = 0

    for field in important_fields:
        value = row.get(field)
        if value is not None and value != "":
            available += 1

    return round(available / len(important_fields) * 100, 2)


def extract_company_name(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip()
    return ""


def collect_for_symbol(
    session: requests.Session,
    input_row: Dict,
    cache_dir: Path,
    cache_days: int,
    refresh: bool,
    sleep_seconds: float,
) -> Dict:
    symbol = clean_symbol(input_row.get("symbol"))

    base_result = {
        "symbol": symbol,
        "company_name_screener": None,
        "screener_url": None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "used_cache": False,
        "status": "ok",

        "roce_percent": None,
        "roe_percent_screener": None,
        "sales_growth_5y": None,
        "profit_growth_5y": None,

        "promoter_holding": None,
        "promoter_holding_change_4q": None,
        "promoter_pledge_percent": None,
        "fii_holding": None,
        "dii_holding": None,
        "fii_holding_change_4q": None,
        "dii_holding_change_4q": None,

        "operating_cash_flow_5y": None,
        "capex_5y": None,
        "free_cash_flow_5y": None,
        "cash_conversion_5y": None,
        "fcf_calculation_method": None,

        "current_pe": None,
        "median_pe_5y": None,
        "price_to_median_pe": None,

        "latest_quarter_sales_growth": None,
        "latest_quarter_profit_growth": None,
        "quarterly_sales_consistency": None,
        "quarterly_profit_consistency": None,

        # Bank / NBFC / Insurance specific metrics (None for non-financial companies)
        "net_interest_margin": None,
        "gross_npa_percent": None,
        "net_npa_percent": None,
        "casa_ratio": None,
        "capital_adequacy_ratio": None,

        "data_completeness_score": None,
        "sector_scoring_group": None,
    }

    if not symbol:
        base_result["status"] = "missing symbol"
        return base_result

    html, url, used_cache = fetch_screener_html(
        session=session,
        symbol=symbol,
        cache_dir=cache_dir,
        cache_days=cache_days,
        refresh=refresh,
        sleep_seconds=sleep_seconds,
    )

    base_result["screener_url"] = url
    base_result["used_cache"] = used_cache

    if not html:
        base_result["status"] = "screener page not found"
        base_result["data_completeness_score"] = 0
        return base_result

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    top_ratios = parse_top_ratios(soup)

    # Core ratios
    base_result["company_name_screener"] = extract_company_name(soup)
    base_result["roce_percent"] = safe_round(
        top_ratios.get("roce")
        or top_ratios.get("roce %")
    )
    base_result["roe_percent_screener"] = safe_round(
        top_ratios.get("roe")
        or top_ratios.get("roe %")
    )

    # Growth
    base_result["sales_growth_5y"] = safe_round(
        extract_growth_value(soup, "Compounded Sales Growth", "5 Years")
    )
    base_result["profit_growth_5y"] = safe_round(
        extract_growth_value(soup, "Compounded Profit Growth", "5 Years")
    )

    # Shareholding
    base_result.update(extract_shareholding(soup))

    # Cash flow history
    base_result.update(extract_cash_flow_history(soup))

    # Valuation history
    base_result.update(extract_valuation_history(soup, top_ratios))

    # Quarterly trend
    base_result.update(extract_quarterly_trend(soup))

    # Sector group (needed before bank metrics extraction)
    base_result["sector_scoring_group"] = classify_sector_group(input_row, page_text)

    # Bank / NBFC / Insurance specific metrics
    financial_groups = {"bank", "nbfc", "insurance"}
    if base_result["sector_scoring_group"] in financial_groups:
        base_result.update(extract_bank_metrics(soup, top_ratios))

    # Completeness
    base_result["data_completeness_score"] = calculate_data_completeness(base_result)

    return base_result


# -----------------------------
# Output Merge
# -----------------------------

def update_output_csv(new_df: pd.DataFrame, output_file: Path, overwrite: bool):
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists() and not overwrite:
        old_df = pd.read_csv(output_file)

        if "symbol" in old_df.columns:
            processed_symbols = set(new_df["symbol"].astype(str).str.upper())
            old_df = old_df[
                ~old_df["symbol"].astype(str).str.upper().isin(processed_symbols)
            ]

            final_df = pd.concat([old_df, new_df], ignore_index=True)
        else:
            final_df = new_df
    else:
        final_df = new_df

    final_df = final_df.sort_values(by=["symbol"], ascending=True)
    final_df.to_csv(output_file, index=False)


# -----------------------------
# Main
# -----------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Collect structured Screener fundamentals and update screener_fundamentals.csv"
    )

    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT_FILE),
        help="Input CSV containing symbol column.",
    )

    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help="Output CSV path for structured Screener fundamentals.",
    )

    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Folder for cached Screener HTML pages.",
    )

    parser.add_argument(
        "--cache-days",
        type=int,
        default=CACHE_DAYS,
        help="Refresh Screener cache after this many days. Default 7.",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force refresh Screener pages even if cache is fresh.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of symbols. Use 0 for all.",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=3.0,
        help="Sleep seconds between Screener requests.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output CSV instead of updating existing rows.",
    )

    args = parser.parse_args()

    input_file = Path(args.input)
    output_file = Path(args.output)
    cache_dir = Path(args.cache_dir)

    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    df = pd.read_csv(input_file)

    if "symbol" not in df.columns:
        raise ValueError("Input CSV must contain a 'symbol' column.")

    df["symbol"] = df["symbol"].apply(clean_symbol)
    df = df[df["symbol"] != ""].copy()
    df = df.drop_duplicates(subset=["symbol"])

    if args.limit and args.limit > 0:
        df = df.head(args.limit)

    print(f"Input file: {input_file}")
    print(f"Symbols to process: {len(df)}")
    print(f"Output file: {output_file}")
    print(f"Cache dir: {cache_dir}")
    print(f"Cache refresh rule: {args.cache_days} days")

    session = create_session()

    results = []

    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        symbol = clean_symbol(row_dict.get("symbol"))

        print(f"\n[{len(results) + 1}/{len(df)}] Collecting Screener fundamentals for {symbol}...")

        try:
            result = collect_for_symbol(
                session=session,
                input_row=row_dict,
                cache_dir=cache_dir,
                cache_days=args.cache_days,
                refresh=args.refresh,
                sleep_seconds=args.sleep,
            )

            print(
                f"  Status: {result.get('status')} | "
                f"Cache: {result.get('used_cache')} | "
                f"ROCE: {result.get('roce_percent')} | "
                f"Sales 5Y: {result.get('sales_growth_5y')} | "
                f"Profit 5Y: {result.get('profit_growth_5y')} | "
                f"Completeness: {result.get('data_completeness_score')}"
            )

            results.append(result)

        except Exception as e:
            print(f"  Error: {e}")
            results.append({
                "symbol": symbol,
                "status": f"error: {e}",
                "data_completeness_score": 0,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            })

        time.sleep(args.sleep)

    new_df = pd.DataFrame(results)

    update_output_csv(
        new_df=new_df,
        output_file=output_file,
        overwrite=args.overwrite,
    )

    print(f"\nDone. Updated: {output_file}")


if __name__ == "__main__":
    main()