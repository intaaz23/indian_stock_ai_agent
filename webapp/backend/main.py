from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pathlib import Path
from threading import Thread
from datetime import datetime
import sqlite3
import subprocess
import uuid
import json
import sys

# -----------------------------
# Paths (repo-specific)
# -----------------------------
BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parents[1]  # indian_stock_ai_agent/
SRC_DIR = REPO_ROOT / "src"
DATA_OUTPUT_DIR = REPO_ROOT / "data" / "output"
DOCS_DIR = REPO_ROOT / "data" / "docs"
REPORTS_DIR = REPO_ROOT / "reports" / "qualitative"

QUAL_INPUT = DATA_OUTPUT_DIR / "qualitative_llm_input.csv"
QUAL_OUTPUT = DATA_OUTPUT_DIR / "qualitative_llm_output.csv"
FINAL_PNG = DATA_OUTPUT_DIR / "final_investor_report.png"

DB_PATH = BACKEND_DIR / "jobs.db"
TEMPLATES = Jinja2Templates(directory=str(BACKEND_DIR / "templates"))

app = FastAPI(title="Indian Stock AI Agent Web API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Models
# -----------------------------
class RunRequest(BaseModel):
    top_n: int = Field(default=20, ge=1, le=100)
    limit: int = Field(default=20, ge=1, le=200)

# -----------------------------
# SQLite helpers
# -----------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            message TEXT,
            top_n INTEGER,
            limit_n INTEGER,
            error TEXT,
            created_at TEXT,
            updated_at TEXT,
            cmd1_log TEXT,
            cmd2_log TEXT
        )
    """)
    conn.commit()
    conn.close()

def create_job(job_id: str, top_n: int, limit_n: int):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO jobs(job_id, status, message, top_n, limit_n, error, created_at, updated_at, cmd1_log, cmd2_log)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, "queued", "Job created", top_n, limit_n, None, now, now, "", ""))
    conn.commit()
    conn.close()

def update_job(job_id: str, **kwargs):
    if not kwargs:
        return
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    keys = list(kwargs.keys())
    vals = [kwargs[k] for k in keys]
    set_clause = ", ".join([f"{k}=?" for k in keys])

    conn = get_conn()
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE job_id=?", vals + [job_id])
    conn.commit()
    conn.close()

def get_job(job_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# -----------------------------
# Core runner
# -----------------------------
def run_pipeline(job_id: str, top_n: int, limit_n: int):
    try:
        update_job(job_id, status="running", message="Running qualitative_llm_reader.py")

        # Ensure folders exist
        DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        DOCS_DIR.mkdir(parents=True, exist_ok=True)

        # Command 1 (exactly your command, made portable via sys.executable)
        cmd1 = [
            sys.executable,
            str(SRC_DIR / "qualitative_llm_reader.py"),
            "--input", str(QUAL_INPUT),
            "--output", str(QUAL_OUTPUT),
            "--docs-dir", str(DOCS_DIR),
            "--reports-dir", str(REPORTS_DIR),
            "--limit", str(limit_n),
        ]

        res1 = subprocess.run(
            cmd1,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True
        )

        cmd1_log = f"STDOUT:\n{res1.stdout}\n\nSTDERR:\n{res1.stderr}"
        update_job(job_id, cmd1_log=cmd1_log)

        if res1.returncode != 0:
            raise RuntimeError(f"qualitative_llm_reader failed (code {res1.returncode})")

        if not QUAL_OUTPUT.exists():
            raise RuntimeError(f"Expected output not found: {QUAL_OUTPUT}")

        update_job(job_id, message="Running print_finallist.py")

        # Command 2 (exactly your command structure)
        cmd2 = [
            sys.executable,
            str(SRC_DIR / "print_finallist.py"),
            "--input", str(QUAL_OUTPUT),
            "--output", str(FINAL_PNG),
            "--top-n", str(top_n),
        ]

        res2 = subprocess.run(
            cmd2,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True
        )

        cmd2_log = f"STDOUT:\n{res2.stdout}\n\nSTDERR:\n{res2.stderr}"
        update_job(job_id, cmd2_log=cmd2_log)

        if res2.returncode != 0:
            raise RuntimeError(f"print_finallist failed (code {res2.returncode})")

        if not FINAL_PNG.exists():
            raise RuntimeError(f"Expected PNG not found: {FINAL_PNG}")

        update_job(job_id, status="success", message="Analysis completed successfully")

    except Exception as e:
        update_job(job_id, status="failed", message="Analysis failed", error=str(e))

# -----------------------------
# API routes
# -----------------------------
@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return TEMPLATES.TemplateResponse("index.html", {"request": request})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/run-analysis")
def run_analysis(payload: RunRequest):
    # Pre-check key files
    if not (SRC_DIR / "qualitative_llm_reader.py").exists():
        raise HTTPException(status_code=404, detail="src/qualitative_llm_reader.py not found")
    if not (SRC_DIR / "print_finallist.py").exists():
        raise HTTPException(status_code=404, detail="src/print_finallist.py not found")
    if not QUAL_INPUT.exists():
        raise HTTPException(status_code=404, detail=f"Input CSV not found: {QUAL_INPUT}")

    job_id = str(uuid.uuid4())
    create_job(job_id, payload.top_n, payload.limit)

    t = Thread(target=run_pipeline, args=(job_id, payload.top_n, payload.limit), daemon=True)
    t.start()

    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Pipeline started in background"
    }

@app.get("/job-status/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/latest-report-image")
def latest_report_image():
    if not FINAL_PNG.exists():
        raise HTTPException(status_code=404, detail="No report image found. Run analysis first.")
    return FileResponse(str(FINAL_PNG), media_type="image/png", filename="final_investor_report.png")

@app.get("/latest-results")
def latest_results(top_n: int = Query(default=20, ge=1, le=100)):
    """
    Reads qualitative_llm_output.csv and returns top_n rows by final_score
    (useful for table view in UI).
    """
    import csv

    if not QUAL_OUTPUT.exists():
        raise HTTPException(status_code=404, detail="qualitative_llm_output.csv not found. Run analysis first.")

    # Lightweight parse without pandas dependency
    rows = []
    with open(QUAL_OUTPUT, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    def fnum(x):
        try:
            return float(x)
        except:
            return -1e18

    rows.sort(key=lambda r: fnum(r.get("final_score", "")), reverse=True)
    rows = rows[:top_n]

    # compact investor fields
    wanted = [
        "symbol", "sector", "price", "market_cap_cr",
        "final_score", "quant_score", "qualitative_score",
        "roe_percent_screener", "roe_percent", "roce_percent",
        "debt_to_equity_percent", "current_pe", "promoter_holding",
        "sales_growth_5y", "free_cash_flow_5y", "estimated_fair_value"
    ]

    out = []
    for r in rows:
        d = {}
        for k in wanted:
            if k == "roe_percent_screener":
                continue
            if k == "roe_percent":
                d["roe_percent"] = r.get("roe_percent_screener") or r.get("roe_percent")
            else:
                d[k] = r.get(k)
        out.append(d)

    return {"count": len(out), "results": out}

@app.get("/job-logs/{job_id}")
def job_logs(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "cmd1_log": job.get("cmd1_log", ""),
        "cmd2_log": job.get("cmd2_log", ""),
        "error": job.get("error")
    }