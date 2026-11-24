"""FastAPI 入口：报送、更正、查询路由与统一错误处理。"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .database import connect, get_db_path, init_db
from .errors import DomainError
from .schemas import (
    BatchIngestRequest,
    BatchIngestResult,
    HealthResponse,
    MileageQueryResponse,
)
from .service import ingest_batch, query_mileage


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="轮对大修走行里程检修数仓供数 API",
    version=__version__,
    description=(
        "分批报送车厢 UTC 日走行米数、轮对装车日期段（含首日、不含末日）与大修日，"
        "支持按记录号更正；按轮对和截止日查询大修后跨车厢累计走行里程。"
    ),
    lifespan=lifespan,
)


def get_conn():
    conn = connect()
    try:
        init_db(conn)  # 幂等，保证测试/新库立即可用
        yield conn
    finally:
        conn.close()


@app.exception_handler(DomainError)
async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


@app.get("/health", response_model=HealthResponse, tags=["运维"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", db_path=get_db_path())


@app.post(
    "/ingest/batch",
    response_model=BatchIngestResult,
    tags=["报送与更正"],
    summary="分批报送（同一 record_no 再次报送即为更正，失败整批回滚）",
)
def ingest(
    payload: BatchIngestRequest,
    conn=Depends(get_conn),
) -> BatchIngestResult:
    result = ingest_batch(conn, payload)
    return BatchIngestResult(**result)


@app.get(
    "/wheelsets/{wheelset_id}/mileage",
    response_model=MileageQueryResponse,
    tags=["里程查询"],
    summary="查询大修后截至 cutoff_day 的跨车厢累计走行里程",
    responses={
        422: {"description": "截至该日找不到大修，缺少起算日"},
    },
)
def mileage(
    wheelset_id: str,
    cutoff_day: str,
    conn=Depends(get_conn),
) -> MileageQueryResponse:
    try:
        dt.date.fromisoformat(cutoff_day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="cutoff_day 必须是 YYYY-MM-DD") from exc
    result = query_mileage(conn, wheelset_id, cutoff_day)
    return MileageQueryResponse(**result)
