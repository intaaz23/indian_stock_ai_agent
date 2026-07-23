# Indian Stock AI Agent — Deep Dive Analysis

## Project Overview

This is a **5-stage automated stock shortlisting pipeline** for Indian equities. It combines free market data, web scraping, quantitative scoring, and a locally-run LLM to produce a final investment watchlist — entirely without paid data providers.

> **Disclaimer embedded in the project:** *"For educational/research use only. Not financial advice."*

---

## Architecture & Pipeline

```
NSE Equity Universe (archives.nseindia.com)
        │
        ▼
Step 1: Quantitative Scan
        nse_free_quant_agent.py — yfinance data
        │
        ▼
Step 2: Screener.in Enrichment
        screener_fundamentals_collector.py — HTML scraping + cache
        │
        ▼
Step 3: Rerank & Final Shortlist
        nse_free_quant_agent.py — merge + combined score
        │
        ▼
Step 4: Document Download
        download_screener_docs.py — annual reports, concalls, PDFs
        │
        ▼
Step 5: LLM Qualitative Analysis
        qualitative_llm_reader.py — Ollama local LLM
        │
        ▼
print_finallist.py — Final Investor Report PNG
```

---

## Module-by-Module Breakdown

### 1. `run_all_Version.py` — Orchestrator

The master pipeline runner. Accepts CLI arguments to control every stage:

| Argument | Default | Purpose |
|---|---|---|
| `--limit` | 0 (all) | Number of NSE stocks to scan |
| `--min-market-cap-cr` | ₹20,000 Cr | Market cap floor |
| `--screener-candidates` | 50 | Candidates sent to Screener enrichment |
| `--shortlist-top-n` | 10 | Final stocks for LLM analysis |
| `--skip-screener-collect` | false | Skip Screener scraping |
| `--skip-doc-download` | false | Skip PDF download |
| `--skip-qualitative` | false | Skip LLM analysis |

It runs each step as a **subprocess**, keeping stages independent and restartable.

---

### 2. `nse_free_quant_agent.py` — Quantitative Scoring Engine

This is the most complex module. It runs in **two modes**:

**Mode A: Full yfinance scan**
- Downloads the NSE equity list (~2,000+ stocks)
- Fetches price, PE, P/B, ROE, margins, growth, beta, dividend yield via `yfinance`
- Computes 11 investor-style sub-scores:

| Score | Investment Style |
|---|---|
| `buffett_munger_score` | Quality + moat |
| `graham_value_score` | Deep value |
| `lynch_garp_score` | Growth at reasonable price |
| `fisher_growth_score` | Long-term growth |
| `greenblatt_score` | Earnings yield + ROCE |
| `marks_risk_score` | Risk-adjusted return |
| `india_compounder_score` | Indian QGLP style |
| `quality_score` | ROE, margins, consistency |
| `growth_score` | Revenue & earnings growth |
| `valuation_score` | PE, P/B, dividend |
| `entry_score` | Price position vs 52-week range |

Each metric uses `score_range()` — a **linear interpolation** function between breakpoints
(bad → average → good → excellent → 100), avoiding hard scoring cliffs.

**Mode B: Rerank from cache**  
After Screener data is available, recalculates scores with an enhanced composite:

```
investor_master_score = 0.70 × base_quant_score + 0.30 × enhanced_screener_score
```

For **banks/NBFCs/insurance**, a separate scoring model is used:
- NIM (Net Interest Margin), Gross/Net NPA, CASA ratio replace ROCE and FCF
- Falls back to a generic model if these metrics are absent

**Red flags** are automatically appended:
- Promoter pledge > 20%
- Negative revenue growth
- Debt/equity > 200%
- Low Screener data completeness
- Sector-specific manual review required (bank/NBFC/insurance)

**Fair value estimation** uses a simple earnings yield / median PE approach, producing:
- `estimated_fair_value`, `strong_buy_below`, `accumulate_below`, `expensive_above`

---

### 3. `screener_fundamentals_collector.py` — Web Scraper

Scrapes `screener.in` company pages for:
- ROCE, 5Y sales/profit growth, promoter holding, FII/DII changes
- Promoter pledge %, quarterly sales/profit consistency
- Operating/free cash flow (5Y), capex, cash conversion
- Banking metrics: NIM, Gross/Net NPA, CASA, capital adequacy
- Current PE vs 5-year median PE

**Smart caching:** HTML pages are cached for 7 days (`cache/screener/html/`), so re-runs
don't hammer Screener's servers. A `--sleep 2-5` parameter paces requests politely.

**Sector overrides:** Manual mapping handles edge cases:
```python
SECTOR_OVERRIDES = {
    "COALINDIA": "commodity_cyclical",
    "HINDZINC":  "commodity_cyclical",
    "INDIANB":   "bank",
    "MUTHOOTFIN":"nbfc",
    "HEROMOTOCO":"auto",
    "GODFRYPHLP":"fmcg",
    ...
}
```

**Data completeness score:** Tracks what % of expected fields were actually found.
Used to cap scores and flag low-confidence stocks.

---

### 4. `download_screener_docs.py` — Document Downloader

For each shortlisted stock, fetches the Screener company page and scores every link
for relevance using a heuristic scoring system:
- **Prefers:** annual reports, concall transcripts, investor presentations, PDFs
- **Penalizes:** shareholder letters, AGM notices, non-financial links
- Extracts year from link text/URL, prefers the most recent 2–3 years
- Downloads up to 3 documents per company into `data/docs/SYMBOL/`
- Outputs `downloaded_docs_summary.csv`

The downloaded PDFs/TXTs are used by the LLM in Step 5.

---

### 5. `qualitative_llm_reader.py` — LLM Qualitative Analyst

Sends a structured prompt to a local Ollama LLM (`qwen2.5:7b` by default via
OpenAI-compatible API at `http://localhost:11434/v1`).

**Prompt instructs the LLM to evaluate 17 dimensions:**

1. Business model quality
2. Revenue drivers
3. Industry attractiveness
4. Competitive advantage / moat
5. Management quality
6. Capital allocation
7. Balance sheet & debt risk
8. Cash flow quality
9. Margin durability
10. Corporate governance red flags
11. Cyclicality risk
12. Regulatory risk
13. Customer / product concentration risk
14. Bull case
15. Bear case
16. Key things to verify manually
17. Final qualitative score (0–100)

**Key design decisions:**
- If documents are available → grounded analysis; LLM told not to invent figures
- If no documents → forced `confidence_level = Low`, must use "speculative" language
- LLM output is parsed as **strict JSON** (handles markdown fences, regex fallback)
- Each stock also gets: `qualitative_decision`, `estimated_fair_value`, price zones

**Scoring guide used in prompt:**
```
85–100: Excellent business, strong moat, clean governance, durable growth
70–84:  Good business, acceptable risks, worth deeper tracking
55–69:  Average business, needs caution
Below 55: Weak business or significant red flags
```

---

### 6. `print_finallist.py` — Report Generator

Reads `qualitative_llm_output.csv`, applies filters, sorts by `final_score`, and renders
a **PNG investor report** with:
- Price zone classification: Strong Buy / Accumulate / Watch / Expensive
- Upside % vs estimated fair value
- Top 20 stocks ranked

---

## Data Flow & Key Output Files

```
data/input/screener_fundamentals.csv     ← Screener-scraped fundamentals
data/output/nse_quant_output.csv         ← All scored NSE stocks (80+ columns)
data/output/qualitative_llm_input.csv    ← Top-N shortlist for LLM
data/output/qualitative_llm_output.csv   ← LLM qualitative results + final scores
data/docs/SYMBOL/                        ← Annual reports, concall PDFs per company
reports/qualitative/SYMBOL_report.md     ← Per-stock markdown reports
cache/screener/html/SYMBOL.html          ← Cached Screener pages (7-day TTL)
```

---

## Actual Results (from current output data — July 2026 run)

The pipeline has already run and produced results for ~60+ stocks.
Top-ranked stocks by `final_score`:

| Rank | Symbol | Sector | Quant Score | Qual Score | Zone |
|---|---|---|---|---|---|
| 1 | MAHABANK | PSU Bank | 92.57 | 70 | Strong Buy |
| 2 | HEROMOTOCO | Auto | 86.14 | 85 | Strong Buy |
| 3 | HINDZINC | Mining | 88.49 | 75 | Strong Buy |
| 4 | NMDC | Steel/Mining | 88.35 | 72 | Strong Buy |
| 5 | COALINDIA | Energy | 87.15 | 75 | Strong Buy |
| 6 | MUTHOOTFIN | NBFC | 86.72 | 75 | Strong Buy |
| 7 | LUPIN | Pharma | 85.99 | 75 | Strong Buy |

---

## Design Strengths

- **Zero paid data** — uses NSE free feeds, yfinance, Screener (public pages), Ollama local LLM
- **Two-pass scoring** — yfinance quick scan first, Screener enrichment only for top candidates (efficient)
- **Sector-aware scoring** — banks/NBFCs/insurance get NIM/NPA/CASA models, not ROCE/FCF
- **Linear interpolation scoring** — avoids cliff-edge jumps in rankings
- **Hallucination controls** — LLM is explicitly instructed not to invent facts when no documents are provided
- **Caching** — Screener HTML cached for 7 days, avoids hammering the server
- **Modular pipeline** — any stage can be skipped via `--skip-*` flags
- **Red flag system** — automatic detection of high pledge, high D/E, low data quality

---

## Design Limitations / Risks

- **yfinance reliability** — unofficial API, can break; missing data fields return `None` and get default/neutral scores
- **Screener scraping** — depends on page structure; site changes can break parsers
- **FCF proxy** — uses investing cash flows as FCF approximation (not true levered FCF), labeled `investing_cash_flow_proxy`
- **LLM quality** — `qwen2.5:7b` is a small model; qualitative analysis may be generic without strong documents
- **No backtesting** — scores are not validated against actual stock returns
- **Banking sector scoring** — falls back to generic model when NIM/NPA data is absent from Screener
- **Market cap filter** — default ₹20,000 Cr filters out small/mid-cap opportunities

---

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.x |
| Market data | yfinance, NSE CSV feed |
| Web scraping | requests, BeautifulSoup4 |
| Data processing | pandas |
| PDF reading | pypdf |
| LLM interface | openai (OpenAI-compatible client → Ollama) |
| Local LLM | Ollama + qwen2.5:7b |
| Config | python-dotenv (.env file) |
| Visualisation | matplotlib |

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Install Ollama and pull model
# https://ollama.com/download
ollama pull qwen2.5:7b

# Create .env
echo "OPENAI_API_KEY=ollama" > .env
echo "OPENAI_BASE_URL=http://localhost:11434/v1" >> .env
echo "OPENAI_MODEL=qwen2.5:7b" >> .env

# Run full pipeline (top 10, market cap ≥ ₹20,000 Cr)
python run_all_Version.py

# Quick test (300 stocks, top 5)
python run_all_Version.py --limit 300 --shortlist-top-n 5

# Quant only (no downloads, no LLM)
python run_all_Version.py --skip-doc-download --skip-qualitative
```

---

*Analysis generated: 2026-07-23*
