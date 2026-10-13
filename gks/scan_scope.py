"""Persistent repository push-time settings shared by the Web and daemon."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
PRESETS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
LABELS = {"24h": "最近 24 小时", "7d": "最近 7 天", "30d": "最近 30 天", "custom": "自定义日期", "after": "指定时间之后"}


def _after_time(raw) -> datetime:
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", raw):
        raise ValueError("请输入完整的北京时间，包含日期和时分")
    try:
        parsed = datetime.fromisoformat(raw).replace(tzinfo=BEIJING)
        parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError("请输入有效且受支持的北京时间") from error
    return parsed


def validate_settings(value, now: datetime | None = None) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("preset"), str) or value["preset"] not in LABELS:
        raise ValueError("请选择有效的扫描时间范围")
    preset = value["preset"]
    settings = {"preset": preset, "start_date": "", "end_date": ""}
    if preset == "after":
        after = _after_time(value.get("after_at"))
        if after >= (now or datetime.now(timezone.utc)):
            raise ValueError("指定时间必须早于当前北京时间")
        settings["after_at"] = after.replace(tzinfo=None).isoformat(timespec="seconds")
    if preset == "custom":
        dates = []
        for name in ("start_date", "end_date"):
            raw = value.get(name)
            if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                raise ValueError("自定义范围需要完整的开始日期和结束日期")
            try:
                dates.append(date.fromisoformat(raw))
            except ValueError as error:
                raise ValueError("请输入有效的日期") from error
            settings[name] = raw
        today = (now or datetime.now(timezone.utc)).astimezone(BEIJING).date()
        if dates[0] > dates[1]:
            raise ValueError("开始日期不能晚于结束日期")
        if dates[1] > today:
            raise ValueError("结束日期不能晚于北京时间的今天")
        try:
            datetime.combine(dates[0], time.min, BEIJING).astimezone(timezone.utc)
            datetime.combine(dates[1] + timedelta(days=1), time.min, BEIJING).astimezone(timezone.utc)
        except (OverflowError, ValueError) as error:
            raise ValueError("日期超出可支持的时间范围") from error
    return settings


@dataclass(frozen=True)
class PushWindow:
    preset: str
    start: datetime
    end: datetime
    label: str
    start_exclusive: bool = False

    def contains(self, pushed_at: datetime) -> bool:
        return (self.start < pushed_at if self.start_exclusive else self.start <= pushed_at) and pushed_at < self.end

    def as_dict(self) -> dict:
        return {"preset": self.preset, "start_at": self.start.isoformat(),
                "end_before": self.end.isoformat(), "label": self.label,
                "timezone": "Asia/Shanghai", "start_exclusive": self.start_exclusive}


def resolve_window(settings: dict, now: datetime | None = None) -> PushWindow:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("范围计算需要带时区的时间")
    now = now.astimezone(timezone.utc)
    settings = validate_settings(settings, now)
    preset = settings["preset"]
    if preset == "after":
        start = _after_time(settings["after_at"]).astimezone(timezone.utc)
        label = f"{settings['after_at'].replace('T', ' ')} 之后（北京时间）"
        return PushWindow(preset, start, now, label, start_exclusive=True)
    if preset == "custom":
        start_date = date.fromisoformat(settings["start_date"])
        end_date = date.fromisoformat(settings["end_date"])
        start = datetime.combine(start_date, time.min, BEIJING).astimezone(timezone.utc)
        end = datetime.combine(end_date + timedelta(days=1), time.min, BEIJING).astimezone(timezone.utc)
        label = f"{settings['start_date']} 至 {settings['end_date']}（北京时间，含结束日）"
    else:
        start, end, label = now - PRESETS[preset], now, LABELS[preset]
    return PushWindow(preset, start, end, label)


def load_settings(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return validate_settings({"preset": "30d"})
    except (OSError, ValueError) as error:
        raise ValueError("无法读取扫描时间设置，请重新保存时间范围") from error
    return validate_settings(value)


def save_settings(path: str | Path, value, now: datetime | None = None) -> dict:
    settings = validate_settings(value, now)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".scan-scope-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return settings


def scope_snapshot(path: str | Path) -> dict:
    settings = load_settings(path)
    return {"settings": settings, "window": resolve_window(settings).as_dict()}
