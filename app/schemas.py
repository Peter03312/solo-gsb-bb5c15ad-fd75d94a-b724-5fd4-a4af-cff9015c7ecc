"""HTTP 入参/出参模型。

关键校验：
- 所有时间字段必须带时区偏移（naive datetime 直接 422）；
- 必填字段缺失同样 422，错误体在 main 中统一为 {"error": {...}}。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import constants as C


def _aware(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{name} 必须带时区偏移，例如 2025-02-11T10:00:00+08:00")
    return dt


class CreateOrderRequest(BaseModel):
    order_id: str = Field(min_length=1)
    created_at: datetime
    timezone: str = Field(description="IANA 时区名，如 Asia/Shanghai")
    calendar_code: str = Field(default="CN", description="节假日表代码，默认 CN")
    promise_by: datetime | None = Field(default=None, description="承诺送达时限，可空")
    weight_kg: float | None = None

    @field_validator("created_at", "promise_by")
    @classmethod
    def _tz(cls, v):
        if v is not None:
            _aware(v, "时间字段")
        return v


class EventIn(BaseModel):
    event_id: str = Field(min_length=1)
    event_type: str
    event_time: datetime = Field(description="扫描发生时间，必须带时区偏移")
    arrived_at: datetime = Field(description="事件到达/接收时间，必须带时区偏移；流内必须单调不减")
    note: str = ""

    # 更正事件专用
    correction_action: Literal["amend", "cancel", "insert"] | None = None
    target_event_id: str | None = Field(default=None, description="amend/cancel 时必填：被更正的原事件 ID")

    @field_validator("event_time", "arrived_at")
    @classmethod
    def _tz(cls, v):
        _aware(v, "时间字段")
        return v


class EventBatchRequest(BaseModel):
    events: list[EventIn] = Field(min_length=1)
