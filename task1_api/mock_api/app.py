"""
Partner Traffic & Revenue API — mock server.

Deterministic given (date, dimensions, version_epoch). Everything a candidate can
observe is reproducible, so two people get the same exercise.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Partner Traffic & Revenue API", version="2.4.1")

CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "acme-de-takehome")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "s3cr3t-do-not-commit")
TOKEN_TTL_S = int(os.environ.get("MOCK_TOKEN_TTL_S", "300"))
RATE_LIMIT_PER_MIN = int(os.environ.get("MOCK_RATE_LIMIT", "60"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MOCK_MAX_JOBS", "2"))
JOB_RUNTIME_S = float(os.environ.get("MOCK_JOB_RUNTIME_S", "3"))
ROW_CAP = int(os.environ.get("MOCK_ROW_CAP", "10000"))
PAGE_MAX = int(os.environ.get("MOCK_PAGE_MAX", "1000"))
RESTATE_DAYS = int(os.environ.get("MOCK_RESTATE_DAYS", "3"))
VERSION_EPOCH_MIN = int(os.environ.get("MOCK_VERSION_EPOCH_MIN", "10"))

# The account this key belongs to. Reports are rendered in ACCOUNT_TZ, not UTC —
# the published spec says UTC. Money is minor units of ACCOUNT_CURRENCY, as a
# string; the spec says float, major units, USD.
ACCOUNT_TZ = "America/Los_Angeles"
ACCOUNT_TZ_OFFSET_H = -7
ACCOUNT_CURRENCY = "EUR"

DIMENSIONS = {
    "date": "report date in the account time zone",
    "hour": "hour of day, 0-23, account time zone",
    "country": "ISO-3166 alpha-2",
    "channel": "traffic channel",
    "campaign_id": "partner campaign id",
    "placement": "placement slug",
    "creative_id": "creative id",
    "device": "desktop | mobile | tablet",
}
METRICS = ["impressions", "clicks", "installs", "signups", "payers", "spend", "revenue"]

# Combinations the backend refuses. `placement` and `creative_id` are stored in a
# different shard than `hour`, so they cannot be crossed.
FORBIDDEN_PAIRS = [{"placement", "hour"}, {"creative_id", "hour"}]
MAX_DIMENSIONS = 3

COUNTRIES = ["US", "GB", "DE", "FR", "BR", "MX", "JP", "PL", "IT", "ES", "CA", "AU"]
CHANNELS = ["search", "social", "display", "video", "native"]
DEVICES = ["desktop", "mobile", "tablet"]
PLACEMENTS = [f"pl_{i:03d}" for i in range(12)]
CREATIVES = [f"cr_{i:04d}" for i in range(150)]
CAMPAIGNS = [f"cmp_{i:05d}" for i in range(60)]

_tokens: dict[str, float] = {}
_jobs: dict[str, dict] = {}
_req_log: list[float] = []


# --------------------------------------------------------------------------- #
# Deterministic row generation
# --------------------------------------------------------------------------- #

def version_epoch() -> int:
    """Advances every VERSION_EPOCH_MIN minutes. Restated days move with it."""
    return int(time.time() // (VERSION_EPOCH_MIN * 60))


def account_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=ACCOUNT_TZ_OFFSET_H)).date()


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def cell(day: date, dims: dict[str, str]) -> dict[str, int]:
    """One row's metrics. Stable for settled days; drifts for restated ones."""
    key = (day.isoformat(),) + tuple(f"{k}={v}" for k, v in sorted(dims.items()))
    base = _h(*key)
    imps = 200 + base % 40_000
    clicks = int(imps * (0.02 + (base >> 8) % 90 / 2_000))
    installs = int(clicks * (0.18 + (base >> 16) % 200 / 800))
    signups = int(installs * (0.40 + (base >> 24) % 400 / 1000))
    payers = int(signups * (0.12 + (base >> 32) % 120 / 800))
    spend = int(clicks * (18 + (base >> 20) % 140))          # minor units
    revenue = int(payers * (1800 + (base >> 12) % 4200))     # minor units

    # Attribution window: the most recent RESTATE_DAYS days keep moving. Later
    # conversions land, and a slice of them is clawed back as fraud.
    lag = (account_today() - day).days
    if 0 <= lag < RESTATE_DAYS:
        maturity = 0.55 + 0.15 * lag + 0.02 * (version_epoch() % 8)
        maturity = min(maturity, 1.0)
        installs = int(installs * maturity)
        signups = int(signups * maturity)
        payers = int(payers * maturity)
        revenue = int(revenue * maturity)
    return {"impressions": imps, "clicks": clicks, "installs": installs,
            "signups": signups, "payers": payers, "spend": spend, "revenue": revenue}


def dim_values(dim: str, day: date) -> list[str]:
    return {
        "date": [day.isoformat()],
        "hour": [str(h) for h in range(24)],
        "country": COUNTRIES,
        "channel": CHANNELS,
        "device": DEVICES,
        "placement": PLACEMENTS,
        "creative_id": CREATIVES,
        "campaign_id": CAMPAIGNS,
    }[dim]


def build_rows(date_from: date, date_to: date, dims: list[str], metrics: list[str]) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    day = date_from
    while day <= date_to:
        combos: list[dict[str, str]] = [{}]
        for d in dims:
            if d == "date":
                continue
            combos = [{**c, d: v} for c in combos for v in dim_values(d, day)]
        for c in combos:
            m = cell(day, c)
            key_dims = {"date": day.isoformat()} if "date" in dims else {}
            key_dims.update(c)
            key = tuple(sorted(key_dims.items()))
            agg = by_key.setdefault(key, {"_dims": key_dims,
                                          **{k: 0 for k in metrics}})
            for k in metrics:
                agg[k] += m[k]
        day += timedelta(days=1)

    rows: list[dict] = []
    for agg in by_key.values():
        row = dict(agg.pop("_dims"))
        for k in metrics:
            # Money arrives as a STRING in minor units of the account currency.
            row[k] = str(agg[k]) if k in ("spend", "revenue") else agg[k]
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Auth and rate limiting
# --------------------------------------------------------------------------- #

def require_token(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, {"error": "missing_token"})
    tok = authorization.split(" ", 1)[1]
    exp = _tokens.get(tok)
    if exp is None:
        raise HTTPException(401, {"error": "invalid_token"})
    if exp < time.time():
        raise HTTPException(401, {"error": "token_expired",
                                  "message": "obtain a new token from /oauth/token"})


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path.startswith("/v2/"):
        now = time.time()
        _req_log[:] = [t for t in _req_log if now - t < 60]
        if len(_req_log) >= RATE_LIMIT_PER_MIN:
            retry = int(61 - (now - _req_log[0]))
            return JSONResponse(
                {"error": "rate_limited",
                 "message": f"{RATE_LIMIT_PER_MIN} requests per minute per account"},
                status_code=429, headers={"Retry-After": str(max(1, retry))})
        _req_log.append(now)
    return await call_next(request)


class TokenReq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: str = "client_credentials"


@app.post("/oauth/token")
def token(body: TokenReq):
    if body.client_id != CLIENT_ID or body.client_secret != CLIENT_SECRET:
        raise HTTPException(401, {"error": "invalid_client"})
    tok = uuid.uuid4().hex
    _tokens[tok] = time.time() + TOKEN_TTL_S
    return {"access_token": tok, "token_type": "Bearer", "expires_in": TOKEN_TTL_S}


@app.get("/v2/account")
def account(authorization: str | None = Header(None)):
    require_token(authorization)
    return {"account_id": "acct_5512", "timezone": ACCOUNT_TZ, "currency": ACCOUNT_CURRENCY,
            "attribution_window_days": RESTATE_DAYS}


@app.get("/v2/dimensions")
def dimensions(authorization: str | None = Header(None)):
    require_token(authorization)
    return {"dimensions": [{"name": k, "description": v} for k, v in DIMENSIONS.items()],
            "metrics": METRICS, "max_dimensions": MAX_DIMENSIONS}


# --------------------------------------------------------------------------- #
# Report jobs
# --------------------------------------------------------------------------- #

class ReportReq(BaseModel):
    date_from: str
    date_to: str
    dimensions: list[str] = Field(default_factory=lambda: ["date"])
    metrics: list[str] = Field(default_factory=lambda: ["impressions", "clicks"])


@app.post("/v2/reports")
def create_report(body: ReportReq, authorization: str | None = Header(None)):
    require_token(authorization)
    try:
        d_from = date.fromisoformat(body.date_from)
        d_to = date.fromisoformat(body.date_to)
    except ValueError:
        raise HTTPException(400, {"error": "bad_date", "message": "expected YYYY-MM-DD"})
    if d_to < d_from:
        raise HTTPException(400, {"error": "bad_range"})
    if (d_to - d_from).days > 6:
        raise HTTPException(400, {"error": "range_too_wide",
                                  "message": "at most 7 days per report"})
    unknown = [d for d in body.dimensions if d not in DIMENSIONS]
    if unknown:
        raise HTTPException(400, {"error": "unknown_dimension", "dimensions": unknown})
    bad = [m for m in body.metrics if m not in METRICS]
    if bad:
        raise HTTPException(400, {"error": "unknown_metric", "metrics": bad})
    if len(body.dimensions) > MAX_DIMENSIONS:
        raise HTTPException(400, {"error": "too_many_dimensions",
                                  "message": f"at most {MAX_DIMENSIONS}"})
    for pair in FORBIDDEN_PAIRS:
        if pair <= set(body.dimensions):
            raise HTTPException(400, {
                "error": "incompatible_dimensions",
                "message": f"{sorted(pair)} cannot be requested together"})
    live = sum(1 for j in _jobs.values() if j["done_at"] > time.time())
    if live >= MAX_CONCURRENT_JOBS:
        raise HTTPException(429, {"error": "too_many_jobs",
                                  "message": f"at most {MAX_CONCURRENT_JOBS} running reports"},
                            headers={"Retry-After": "5"})
    rid = uuid.uuid4().hex[:16]
    _jobs[rid] = {"req": body, "created_at": time.time(),
                  "done_at": time.time() + JOB_RUNTIME_S,
                  "version": version_epoch(), "rows": None}
    return {"report_id": rid, "status": "PENDING"}


@app.get("/v2/reports/{report_id}")
def report_status(report_id: str, authorization: str | None = Header(None)):
    require_token(authorization)
    job = _jobs.get(report_id)
    if not job:
        raise HTTPException(404, {"error": "unknown_report"})
    left = job["done_at"] - time.time()
    status = "DONE" if left <= 0 else ("RUNNING" if left < JOB_RUNTIME_S * 0.6 else "PENDING")
    out = {"report_id": report_id, "status": status}
    if status == "DONE":
        if job["rows"] is None:
            r = job["req"]
            rows = build_rows(date.fromisoformat(r.date_from), date.fromisoformat(r.date_to),
                              r.dimensions, r.metrics)
            job["truncated"] = len(rows) > ROW_CAP
            job["rows"] = rows[:ROW_CAP]
        out["row_count"] = len(job["rows"])
    return out


@app.get("/v2/reports/{report_id}/rows")
def report_rows(report_id: str,
                offset: int = Query(0, ge=0),
                limit: int = Query(100, ge=1),
                order_by: str | None = None,
                authorization: str | None = Header(None)):
    require_token(authorization)
    job = _jobs.get(report_id)
    if not job:
        raise HTTPException(404, {"error": "unknown_report"})
    if job["done_at"] > time.time():
        raise HTTPException(409, {"error": "not_ready", "message": "report is still running"})
    if job["rows"] is None:
        report_status(report_id, authorization)
    rows = job["rows"]
    limit = min(limit, PAGE_MAX)

    if order_by:
        keys = [k.strip() for k in order_by.split(",")]
        rows = sorted(rows, key=lambda r: tuple(str(r.get(k, "")) for k in keys))
    else:
        # No stable order is promised without order_by: the backend reads shards
        # in whatever order they answer, so page boundaries move between calls.
        seed = _h(report_id, int(time.time() // 7))
        rows = sorted(rows, key=lambda r: _h(seed, *sorted(r.items(), key=lambda kv: kv[0])))

    page = rows[offset:offset + limit]
    return {
        "rows": page,
        "total": len(rows),
        "has_more": offset + limit < len(rows),
        "meta": {
            "currency": ACCOUNT_CURRENCY,
            "timezone": ACCOUNT_TZ,
            "data_version": job["version"],
            "restated_through": (account_today() - timedelta(days=RESTATE_DAYS - 1)).isoformat(),
            "truncated": job.get("truncated", False),
        },
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "account_today": account_today().isoformat(),
            "version_epoch": version_epoch()}
