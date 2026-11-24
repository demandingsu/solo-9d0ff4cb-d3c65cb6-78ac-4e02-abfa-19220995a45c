"""请求/响应 Pydantic 模型。所有日期均为 UTC，格式 YYYY-MM-DD。"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _parse_utc_day(value: str) -> str:
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - pydantic 会包装
        raise ValueError(f"日期必须是 YYYY-MM-DD：{value!r}") from exc
    return value


class MileageRecord(BaseModel):
    """车厢 UTC 日走行米数报送/更正（record_no 相同即更正）。"""

    record_no: str = Field(min_length=1)
    car_id: str = Field(min_length=1)
    day: str  # UTC 日
    meters: int = Field(ge=0, description="走行米数，零米视为已报")

    @field_validator("day")
    @classmethod
    def _check_day(cls, v: str) -> str:
        return _parse_utc_day(v)


class MountRecord(BaseModel):
    """轮对在某车厢的装车段 [start_day, end_day)，含首日、不含末日。"""

    record_no: str = Field(min_length=1)
    wheelset_id: str = Field(min_length=1)
    car_id: str = Field(min_length=1)
    start_day: str
    end_day: str

    @field_validator("start_day", "end_day")
    @classmethod
    def _check_days(cls, v: str) -> str:
        return _parse_utc_day(v)

    @model_validator(mode="after")
    def _check_range(self) -> "MountRecord":
        if self.end_day <= self.start_day:
            raise ValueError("装车段必须含首日、不含末日，要求 end_day > start_day")
        return self


class OverhaulRecord(BaseModel):
    """轮对大修日报送/更正。"""

    record_no: str = Field(min_length=1)
    wheelset_id: str = Field(min_length=1)
    overhaul_day: str

    @field_validator("overhaul_day")
    @classmethod
    def _check_day(cls, v: str) -> str:
        return _parse_utc_day(v)


class BatchIngestRequest(BaseModel):
    """分批报送：三类记录都可选，record_no 在各自表内命名。"""

    mileage: list[MileageRecord] = Field(default_factory=list)
    mounts: list[MountRecord] = Field(default_factory=list)
    overhauls: list[OverhaulRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_duplicate_record_nos(self) -> "BatchIngestRequest":
        for field_name in ("mileage", "mounts", "overhauls"):
            seen: set[str] = set()
            for item in getattr(self, field_name):
                if item.record_no in seen:
                    raise ValueError(f"{field_name} 内记录号重复：{item.record_no}")
                seen.add(item.record_no)
        return self


class BatchIngestResult(BaseModel):
    status: Literal["committed"] = "committed"
    received: dict[str, int]
    inserted: dict[str, int]
    corrected: dict[str, int]
    inserted_record_nos: dict[str, list[str]]
    corrected_record_nos: dict[str, list[str]]


class MissingRange(BaseModel):
    """某车厢连续缺报日期段 [start_day, end_day)，含首日、不含末日。"""

    start_day: str
    end_day: str
    days: int = Field(description="缺报天数（含首日、不含末日）")


class CarMileageBreakdown(BaseModel):
    car_id: str
    mount_start_day: str = Field(description="查询区间与装车段相交后的实际起算日（含）")
    mount_end_day: str = Field(description="实际截止次日（不含）")
    known_meters: int
    missing_ranges: list[MissingRange] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing_ranges


class MileageQueryResponse(BaseModel):
    wheelset_id: str
    cutoff_day: str
    baseline_overhaul_day: str = Field(description="截至该日最近一次大修日，里程从该日起算（含）")
    range_start_day: str
    range_end_exclusive_day: str
    known_meters: int = Field(description="已报里程合计；有缺报时只是下限")
    mileage_complete: bool = Field(description="true=里程完整；false=存在缺报，known_meters 为下限")
    is_lower_bound: bool
    cars: list[CarMileageBreakdown]
    missing_ranges: dict[str, list[MissingRange]]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    db_path: str
