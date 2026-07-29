# Indian Stock AI Agent

A Python-based Indian stock shortlisting system using:

- NSE equity universe
- yfinance free financial data
- investor-style quantitative scoring
- Screener document downloader
- local Ollama LLM qualitative analysis

## Purpose

This project is for educational and research use only.  
It is not financial advice.

The system helps shortlist Indian stocks for further manual analysis.

## Pipeline

```text
NSE stock universe
    ↓
Quantitative analysis
    ↓
Market cap filter
    ↓
Top 10 shortlist
    ↓
Download annual reports / presentations / concalls
    ↓
Ollama qualitative LLM analysis
    ↓
Final watchlist for manual verification
```

## Requirements

Install Python packages:

```bash
pip install -r requirements.txt
```

Install Ollama:

```text
https://ollama.com/download
```

Pull model:

```bash
ollama pull qwen2.5:7b
```

## Environment

Create `.env`:

```text
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_MODEL=qwen2.5:7b
```

## Run full pipeline

```bash
python run_all_Version.py
```

Default settings:

```text
Market cap filter: ₹20,000 Cr+
Shortlist: Top 10
LLM model: qwen2.5:7b via Ollama
```

## Quick test

```bash
python run_all_Version.py --limit 300 --shortlist-top-n 5
```

## Quant only

```bash
python run_all_Version.py --skip-doc-download --skip-qualitative
```

## Output files

```text
data/output/nse_quant_output.csv
data/output/qualitative_llm_input.csv
data/output/qualitative_llm_output.csv
reports/qualitative/
```

## Manual verification checklist

Before any investment decision, manually verify:

- ROCE
- 5-year sales CAGR
- 5-year profit CAGR
- free cash flow history
- promoter holding
- promoter pledge
- FII/DII holding trend
- quarterly results trend
- debt trend
- auditor notes
- related-party transactions
- valuation history
- sector outlook

## Disclaimer

This tool is only for research and learning.  
Do not make investment decisions based only on this output.

## For my (Intaaz) understanding

The pipeline runs in 6 steps. Steps 1-3 are a two-pass quant process:

**Step 1** — `nse_free_quant_agent.py --disable-screener-merge`  
Scans the full NSE universe (~2000 stocks) via yfinance. Filters by market cap ≥ ₹20,000 Cr. Outputs the top 50 candidates using yfinance-only scores.

**Step 2** — `screener_fundamentals_collector.py`  
Scrapes Screener.in only for the top 50 candidates (not all 2000). Collects ROCE, 5Y CAGR, promoter holding/pledge, FII/DII trend, quarterly consistency.

**Step 3** — `nse_free_quant_agent.py --rerank-from-cache`  
Re-scores all 50 using the combined formula:
`investor_master_score = 0.70 × base_quant_score + 0.30 × enhanced_screener_score`
Selects the final top 20.

**Step 4** — `download_screener_docs.py`  
Downloads annual reports, concalls, and presentations for the top 20 into `data/docs/SYMBOL/`.

**Step 5** — `qualitative_llm_reader.py`  
Sends each company's documents + financial metrics to the LLM (Groq/Gemini/Ollama).
Produces a qualitative score (0-100), rating, bull/bear case, governance flags, and final decision.

**Step 6** — `print_finallist.py`  
Reads `qualitative_llm_output.csv` and renders a colour-coded PNG table:
- Green = Strong Buy zone
- Yellow = Accumulate zone
- Red = Expensive zone

## Finally the run command flow

### Option A — One command (recommended)

```bash
python run_all_Version.py --screener-candidates 50 --shortlist-top-n 20 --min-market-cap-cr 20000
```

This runs all 6 steps in order and generates the final PNG automatically:

```
Step 1 → yfinance scan: full NSE universe, filter ≥ ₹20,000 Cr, top 50 candidates
Step 2 → Screener.in: fetch fundamentals for top 50 (ROCE, 5Y CAGR, promoter, FII/DII)
Step 3 → Rerank: combine yfinance + Screener scores, select final top 20
Step 4 → Download: annual reports / concalls / presentations for top 20
Step 5 → LLM: qualitative analysis via Groq / Gemini / Ollama
Step 6 → Report: generate data/output/final_investor_report.png
```

Skip flags (add any combination):
```bash
--skip-screener-collect    # skip Step 2 (use cached screener_fundamentals.csv)
--skip-doc-download        # skip Step 4
--skip-qualitative         # skip Step 5 + 6
--skip-report              # skip Step 6 only (keep qualitative output, skip PNG)
```

---

### Option B — Step by step (for debugging or partial reruns)

```bash
# Step 1 — Full NSE yfinance scan. RUN ONCE PER DAY.
python .\src\nse_free_quant_agent.py --limit 0 --min-market-cap-cr 20000 --shortlist-top-n 50 --disable-screener-merge

# Step 2 — Collect Screener fundamentals for top 50
python .\src\screener_fundamentals_collector.py --input .\data\output\qualitative_llm_input.csv --limit 50 --overwrite

# Step 3 — Rerank using Screener data, select final top 20
python .\src\nse_free_quant_agent.py --rerank-from-cache --min-market-cap-cr 20000 --shortlist-top-n 20

# Step 4 — Download annual reports / concalls / presentations
python .\src\download_screener_docs.py --input .\data\output\qualitative_llm_input.csv --output-dir .\data\docs --limit 20

# Step 5 — LLM qualitative analysis
python .\src\qualitative_llm_reader.py --input .\data\output\qualitative_llm_input.csv --output .\data\output\qualitative_llm_output.csv --docs-dir .\data\docs --reports-dir .\reports\qualitative --limit 20

# Step 6 — Generate final PNG report
python .\src\print_finallist.py --input .\data\output\qualitative_llm_output.csv --output .\data\output\final_investor_report.png --top-n 20
```

Why the 3-step quant process (Steps 1-3)?
- Step 1 uses fast/free yfinance data to scan the full ~2000 stock NSE universe
- Step 2 fetches deep Screener data only for the top 50, not all 2000 stocks (efficient)
- Step 3 reranks using: yfinance score + Screener ROCE/5Y CAGR/FCF/promoter/quarter trend

## OUTPUT

data/output/nse_quant_output.csv
data/output/qualitative_llm_input.csv
data/input/screener_fundamentals.csv
data/output/qualitative_llm_output.csv
reports/qualitative/

## Examlpe consol output 

Top 20 stocks after qualitative analysis:
MAHABANK | Quant: 92.57 | Qual: 70 | Final: 86.93 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
HEROMOTOCO | Quant: 86.14 | Qual: 85 | Final: 85.74 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
HINDZINC | Quant: 88.49 | Qual: 75 | Final: 85.12 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
NMDC | Quant: 88.35 | Qual: 72 | Final: 84.26 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
COALINDIA | Quant: 87.15 | Qual: 75 | Final: 84.11 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
MUTHOOTFIN | Quant: 86.72 | Qual: 75 | Final: 83.79 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
LUPIN | Quant: 85.99 | Qual: 75 | Final: 83.24 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
INDIANB | Quant: 87.95 | Qual: 65 | Final: 82.21 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
GODFRYPHLP | Quant: 85.17 | Qual: 70 | Final: 81.38 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
VEDL | Quant: 83.12 | Qual: 78 | Final: 81.33 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
HBLENGINE | Quant: 83.23 | Qual: 75 | Final: 81.17 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
HINDPETRO | Quant: 82.21 | Qual: 75 | Final: 80.41 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
BPCL | Quant: 82.18 | Qual: 75 | Final: 80.39 | HIGH CONVICTION WATCHLIST / BUY ZONE IF PRICE FITS
EMMVEE | Quant: 81.41 | Qual: 75 | Final: 79.81 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
HUDCO | Quant: 83.06 | Qual: 70 | Final: 79.8 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
BAJAJHLDNG | Quant: 80.6 | Qual: 78 | Final: 79.69 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
OIL | Quant: 82.83 | Qual: 70 | Final: 79.62 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
WAAREEENER | Quant: 80.89 | Qual: 75 | Final: 79.42 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
IOC | Quant: 81.02 | Qual: 70 | Final: 78.27 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION
BANKINDIA | Quant: 82.55 | Qual: 58 | Final: 76.41 | GOOD WATCHLIST - WAIT FOR RIGHT VALUATION