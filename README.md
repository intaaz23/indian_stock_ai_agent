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
python run_all.py
```

Default settings:

```text
Market cap filter: ₹20,000 Cr+
Shortlist: Top 10
LLM model: qwen2.5:7b via Ollama
```

## Quick test

```bash
python run_all.py --limit 300 --shortlist-top-n 5
```

## Quant only

```bash
python run_all.py --skip-doc-download --skip-qualitative
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

## For my(Intaaz) understnding 

python .\src\nse_free_quant_agent.py --limit 0 --min-market-cap-cr 20000 --shortlist-top-n 50 --disable-screener-merge

python .\src\screener_fundamentals_collector.py --input .\data\output\qualitative_llm_input.csv --limit 50 --overwrite

python .\src\nse_free_quant_agent.py --limit 0 --min-market-cap-cr 20000 --shortlist-top-n 20

Why this 3-step process is useful
Step #1 finds best candidates using fast/free yfinance data.
Step #2 collects deeper Screener structured data only for top 50, not all 2000 stocks.
Step #3 reranks those candidates using:
yfinance score
Screener score
India-specific metrics
promoter/shareholding data
ROCE/5Y CAGR/FCF/quarter trend
This is efficient and avoids scraping Screener for the full NSE universe.

## Finally the run command flow

python .\src\nse_free_quant_agent.py --limit 0 --min-market-cap-cr 20000 --shortlist-top-n 50 --disable-screener-merge **********RUN ONCE PER DAY****************

python .\src\screener_fundamentals_collector.py --input .\data\output\qualitative_llm_input.csv --limit 50 --overwrite

python .\src\nse_free_quant_agent.py --rerank-from-cache --min-market-cap-cr 20000 --shortlist-top-n 20

python .\src\download_screener_docs.py --input .\data\output\qualitative_llm_input.csv --output-dir .\data\docs --limit 20

python .\src\qualitative_llm_reader.py --input .\data\output\qualitative_llm_input.csv --output .\data\output\qualitative_llm_output.csv --docs-dir .\data\docs --reports-dir .\reports\qualitative --limit 20

python .\src\qualitative_llm_reader.py --input .\data\output\qualitative_llm_input.csv --output .\data\output\qualitative_llm_output.csv --docs-dir .\data\docs --reports-dir .\reports\qualitative --limit 20

## OUTPUT

data/output/nse_quant_output.csv
data/output/qualitative_llm_input.csv
data/input/screener_fundamentals.csv
data/output/qualitative_llm_output.csv
reports/qualitative/