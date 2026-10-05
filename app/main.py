"""FastAPI 入口：快递理赔时限判定 HTTP API。"""
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .errors import AppError
from .schemas import CreateOrderRequest, EventBatchRequest, EventIn
from .services import (archive_determination, create_order, determination_history,
                       ingest_events, list_events, live_determination)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    yield


app = FastAPI(
    title="快递理赔时限判定服务",
    version="1.0.0",
    description="按订单创建时生效政策版本判定理赔申请时限；支持时区/节假日、事件去重与迟到更正。",
    lifespan=lifespan,
)


# ------------------------------------------------------------ 统一错误格式

@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"] if x not in ("body",))
        details.append({
            "field": loc or "body",
            "type": err["type"],
            "message": err["msg"],
        })
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "INVALID_REQUEST",
            "message": "请求参数校验失败（字段缺失或时间缺少时区等）",
            "details": details,
        }},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------ 订单

@app.post("/api/v1/orders", status_code=201)
def api_create_order(req: CreateOrderRequest):
    return create_order(req)


@app.get("/api/v1/orders/{order_id}")
def api_get_order(order_id: str):
    from .services import require_order
    return require_order(order_id)


# ------------------------------------------------------------ 事件

@app.post("/api/v1/orders/{order_id}/events")
def api_ingest_event(order_id: str, req: EventIn):
    return ingest_events(order_id, [req])


@app.post("/api/v1/orders/{order_id}/events/batch")
def api_ingest_batch(order_id: str, req: EventBatchRequest):
    return ingest_events(order_id, req.events)


@app.get("/api/v1/orders/{order_id}/events")
def api_list_events(order_id: str):
    return list_events(order_id)


# ------------------------------------------------------------ 判定

def _parse_as_of(v: str | None) -> datetime | None:
    if not v:
        return None
    from .engine import parse_dt
    try:
        return parse_dt(v)
    except Exception:
        raise AppError("INVALID_AS_OF", "as_of 必须是带时区偏移的 ISO8601 时间", 422)


@app.get("/api/v1/orders/{order_id}/determination")
def api_live(order_id: str, as_of: str | None = None):
    """实时判定（最新一代未归档则现算；已归档则返回不可变快照）。"""
    return live_determination(order_id, as_of=_parse_as_of(as_of))


@app.post("/api/v1/orders/{order_id}/determination/archive")
def api_archive(order_id: str, as_of: str | None = None):
    """归档当前代判定。归档后迟到事件只会另开新一代，不改写本快照。"""
    return archive_determination(order_id, as_of=_parse_as_of(as_of))


@app.get("/api/v1/orders/{order_id}/determinations")
def api_history(order_id: str):
    return determination_history(order_id)


# ------------------------------------------------------------ 基础数据

@app.get("/api/v1/policies")
def api_policies():
    rows = db.q("SELECT version, valid_from, valid_to, body_json FROM policies ORDER BY valid_from")
    return {"policies": [
        {"version": r["version"], "valid_from": r["valid_from"],
         "valid_to": r["valid_to"], "body": json.loads(r["body_json"])}
        for r in rows
    ]}


@app.get("/api/v1/calendars/{calendar_code}")
def api_calendar(calendar_code: str):
    rows = db.q(
        "SELECT date, kind, name FROM calendars WHERE calendar_code = ? ORDER BY date",
        (calendar_code,),
    )
    if not rows:
        raise AppError("CALENDAR_NOT_FOUND", f"节假日表不存在: {calendar_code}", 404)
    return {"calendar_code": calendar_code, "days": [dict(r) for r in rows]}
