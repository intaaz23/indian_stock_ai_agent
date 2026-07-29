# --- ADD/UPDATE IMPORTS AT TOP ---
from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from threading import Thread, Lock
from datetime import datetime
import os
import sqlite3
import subprocess
import uuid
import sys
import time


# -----------------------------
# Paths (repo-specific)
# -----------------------------
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
REPO_ROOT = BACKEND_DIR.parents[1]  # IMPORTANT: repo root
SRC_DIR = REPO_ROOT / "src"
DATA_OUTPUT_DIR = REPO_ROOT / "data" / "output"
DOCS_DIR = REPO_ROOT / "data" / "docs"
REPORTS_DIR = REPO_ROOT / "reports" / "qualitative"

QUAL_INPUT = DATA_OUTPUT_DIR / "qualitative_llm_input.csv"
QUAL_OUTPUT = DATA_OUTPUT_DIR / "qualitative_llm_output.csv"
FINAL_PNG = DATA_OUTPUT_DIR / "final_investor_report.png"
FINAL_CSV = DATA_OUTPUT_DIR / "final_investor_report.csv"

DB_PATH = BACKEND_DIR / "jobs.db"
TEMPLATES = Jinja2Templates(directory=str(BACKEND_DIR / "templates"))
def num2(value):
    try:
        if value is None:
            return "-"
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return value

TEMPLATES.env.filters["num2"] = num2

app = FastAPI(title="Indian Stock AI Agent Web API", version="1.1.0")

# Serve the static frontend at /ui
if FRONTEND_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

# CORS: restrict origins via ALLOWED_ORIGINS env var (comma-separated).
# Defaults to "*" for local dev; always set a specific origin in production.
_origins_env = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RUN_LOCK = Lock()

# API key auth: set API_KEY env var to require a key on mutating endpoints.
# If API_KEY is not set, auth is skipped (suitable for local-only use).
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

def _check_api_key(api_key: str = Security(_API_KEY_HEADER)):
    expected = os.getenv("API_KEY", "")
    if not expected:
        return  # no key configured — open access (local dev)
    if not api_key or api_key != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


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
            started_at TEXT,
            ended_at TEXT,
            duration_seconds REAL,
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
        INSERT INTO jobs(
            job_id, status, message, top_n, limit_n, error,
            created_at, updated_at, started_at, ended_at, duration_seconds,
            cmd1_log, cmd2_log
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, "queued", "Job created", top_n, limit_n, None,
          now, now, None, None, None, "", ""))
    conn.commit()
    conn.close()


# Columns that update_job is allowed to write — prevents accidental or
# injection-style writes to unexpected columns if kwargs ever widens.
_ALLOWED_JOB_COLUMNS = frozenset({
    "status", "message", "error",
    "started_at", "ended_at", "updated_at",
    "duration_seconds", "cmd1_log", "cmd2_log",
})

def update_job(job_id: str, **kwargs):
    if not kwargs:
        return
    invalid = set(kwargs) - _ALLOWED_JOB_COLUMNS
    if invalid:
        raise ValueError(f"update_job: unknown column(s): {invalid}")
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


# --- NEW: active job check ---
def get_active_job():
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM jobs
        WHERE status IN ('queued', 'running')
        ORDER BY created_at DESC
        LIMIT 1
    """).fetchone()
    conn.close()
    return dict(row) if row else None


# --- NEW: latest jobs list ---
def list_jobs(limit: int = 20):
    conn = get_conn()
    rows = conn.execute("""
        SELECT job_id, status, message, top_n, limit_n, error,
               created_at, started_at, ended_at, duration_seconds
        FROM jobs
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -----------------------------
# Core runner
# -----------------------------
def run_pipeline(job_id: str, top_n: int, limit_n: int, input_csv_path: str):
    lock_acquired = False
    start_ts = time.time()

    try:
        # acquire lock
        lock_acquired = RUN_LOCK.acquire(blocking=False)
        if not lock_acquired:
            update_job(
                job_id,
                status="failed",
                message="Another run is in progress. Please retry after it completes.",
                error="run_locked"
            )
            return

        update_job(
            job_id,
            status="running",
            message="Running qualitative_llm_reader.py",
            started_at=datetime.utcnow().isoformat()
        )

        # Ensure folders exist
        DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        DOCS_DIR.mkdir(parents=True, exist_ok=True)

        # Command 1
        cmd1 = [
            sys.executable,
            str(SRC_DIR / "qualitative_llm_reader.py"),
            "--input", str(input_csv_path),
            "--output", str(QUAL_OUTPUT),
            "--docs-dir", str(DOCS_DIR),
            "--reports-dir", str(REPORTS_DIR),
            "--limit", str(limit_n),
        ]

        res1 = subprocess.run(
            cmd1,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False
        )

        cmd1_log = f"STDOUT:\n{res1.stdout}\n\nSTDERR:\n{res1.stderr}"
        update_job(job_id, cmd1_log=cmd1_log)

        if res1.returncode != 0:
            raise RuntimeError(f"qualitative_llm_reader failed (code {res1.returncode})")

        if not QUAL_OUTPUT.exists():
            raise RuntimeError(f"Expected output not found: {QUAL_OUTPUT}")

        update_job(job_id, message="Running print_finallist.py")

        # Command 2
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
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False
        )

        cmd2_log = f"STDOUT:\n{res2.stdout}\n\nSTDERR:\n{res2.stderr}"
        update_job(job_id, cmd2_log=cmd2_log)

        # success classification
        # 1) hard fail if png missing
        if not FINAL_PNG.exists():
            raise RuntimeError(f"print_finallist failed and PNG not found (code {res2.returncode})")

        # 2) if cmd2 non-zero but png exists => partial_success
        if res2.returncode != 0:
            duration = round(time.time() - start_ts, 2)
            update_job(
                job_id,
                status="partial_success",
                message="PNG generated, but print_finallist returned non-zero exit code.",
                error=f"print_finallist_exit_{res2.returncode}",
                ended_at=datetime.utcnow().isoformat(),
                duration_seconds=duration
            )
            return

        # 3) detect qualitative rate-limit warning from cmd1 logs
        lower_log = (res1.stdout + "\n" + res1.stderr).lower()
        if "rate limit" in lower_log or "429" in lower_log:
            duration = round(time.time() - start_ts, 2)
            update_job(
                job_id,
                status="partial_success",
                message="Completed with rate-limit warnings in qualitative step.",
                error="qualitative_rate_limit_warning",
                ended_at=datetime.utcnow().isoformat(),
                duration_seconds=duration
            )
            return

        # clean success
        duration = round(time.time() - start_ts, 2)
        update_job(
            job_id,
            status="success",
            message="Analysis completed successfully",
            ended_at=datetime.utcnow().isoformat(),
            duration_seconds=duration
        )

    except Exception as e:
        duration = round(time.time() - start_ts, 2)
        update_job(
            job_id,
            status="failed",
            message="Analysis failed",
            error=str(e),
            ended_at=datetime.utcnow().isoformat(),
            duration_seconds=duration
        )
    finally:
        if lock_acquired:
            RUN_LOCK.release()


# -----------------------------
# API routes
# -----------------------------
@app.on_event("startup")
def startup():
    init_db()
    # Reset jobs left in running/queued state from a previous server crash.
    # Without this, the run lock would never be acquired and all new /run
    # requests would get a 409 Conflict forever after an unclean shutdown.
    conn = get_conn()
    conn.execute(
        """
        UPDATE jobs
        SET status='failed',
            error='server_restarted',
            message='Job interrupted by server restart',
            updated_at=?
        WHERE status IN ('queued', 'running')
        """,
        (datetime.utcnow().isoformat(),),
    )
    conn.commit()
    conn.close()


@app.get("/", response_class=RedirectResponse, include_in_schema=False)
def home():
    return RedirectResponse(url="/ui/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-analysis")
def run_analysis(payload: RunRequest, _: None = Security(_check_api_key)):
    # route-level lock guard (friendly early rejection)
    active = get_active_job()
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Another job is active: {active['job_id']} (status={active['status']})"
        )

    # pre-check files
    if not (SRC_DIR / "qualitative_llm_reader.py").exists():
        raise HTTPException(status_code=404, detail="src/qualitative_llm_reader.py not found")
    if not (SRC_DIR / "print_finallist.py").exists():
        raise HTTPException(status_code=404, detail="src/print_finallist.py not found")

    # NEW: fallback input discovery
    candidate_inputs = [
        REPO_ROOT / "data" / "output" / "qualitative_llm_input.csv",
        REPO_ROOT / "data" / "qualitative_llm_input.csv",
    ]
    input_csv = next((p for p in candidate_inputs if p.exists()), None)
    if not input_csv:
        raise HTTPException(
            status_code=404,
            detail=f"Input CSV not found. Tried: {[str(p) for p in candidate_inputs]}"
        )

    job_id = str(uuid.uuid4())
    create_job(job_id, payload.top_n, payload.limit)

    # CHANGED: pass input_csv path to pipeline
    t = Thread(
        target=run_pipeline,
        args=(job_id, payload.top_n, payload.limit, str(input_csv)),
        daemon=True
    )
    t.start()

    return {
        "job_id": job_id,
        "status": "queued",
        "message": f"Pipeline started in background (input={input_csv})"
    }

@app.get("/job-status/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# --- NEW: jobs history endpoint ---
@app.get("/jobs")
def jobs(limit: int = Query(default=20, ge=1, le=100)):
    return {"count": limit, "items": list_jobs(limit=limit)}


@app.post("/admin/reset-stuck-jobs", summary="Reset stuck queued/running jobs")
def reset_stuck_jobs(_: None = Security(_check_api_key)):
    """
    Forcefully marks any queued or running jobs as failed.
    Use this when a job is stuck and blocking new /run-analysis requests.
    Protected by the same API key as /run-analysis.
    """
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.execute(
        """
        UPDATE jobs
        SET status='failed', error='manual_reset', message='Reset by admin endpoint', updated_at=?
        WHERE status IN ('queued', 'running')
        """,
        (now,),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return {"reset_count": affected, "message": f"{affected} stuck job(s) cleared. You can now call /run-analysis."}


@app.get("/latest-report-image")
def latest_report_image():
    if not FINAL_PNG.exists():
        raise HTTPException(status_code=404, detail="No report image found. Run analysis first.")
    return FileResponse(str(FINAL_PNG), media_type="image/png", filename="final_investor_report.png")


@app.get("/latest-results")
def latest_results(top_n: int = Query(default=20, ge=1, le=100)):
    import csv

    if not QUAL_OUTPUT.exists():
        raise HTTPException(status_code=404, detail="qualitative_llm_output.csv not found. Run analysis first.")

    rows = []
    with open(QUAL_OUTPUT, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return -1e18

    rows.sort(key=lambda r: fnum(r.get("final_score", "")), reverse=True)
    rows = rows[:top_n]

    wanted = [
        "symbol", "sector", "price", "market_cap_cr",
        "final_score", "quant_score", "qualitative_score",
        "roe_percent_screener", "roe_percent", "roce_percent",
        "debt_to_equity_percent", "current_pe", "promoter_holding",
        "sales_growth_5y", "free_cash_flow_5y", "estimated_fair_value",
        "strong_buy_below", "accumulate_below", "expensive_above",
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
        "error": job.get("error"),
        "status": job.get("status")
    }


@app.get("/stock-report/{symbol}", summary="Get qualitative LLM report for a stock")
def stock_report(symbol: str):
    # Sanitise symbol — only allow alphanumeric, hyphen, ampersand
    import re
    clean = re.sub(r"[^A-Za-z0-9\-&]", "", symbol).upper()
    if not clean:
        raise HTTPException(status_code=400, detail="Invalid symbol")

    report_path = REPORTS_DIR / f"{clean}_qualitative_report.md"
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No qualitative report found for {clean}. Run the pipeline first."
        )
    try:
        content = report_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read report: {e}")

    return {"symbol": clean, "markdown": content, "found": True}