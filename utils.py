"""Небольшие вспомогательные функции без привязки к Telegram и базе."""
import re
from datetime import datetime, timedelta, timezone

from config import CALL_WINDOWS, LOCAL_TZ, MSK, NIGHT_FROM, NIGHT_TO, TZ_LABEL_BY_OFFSET

SQL_TS = "%Y-%m-%d %H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_str(dt: datetime | None = None) -> str:
    """Время в формате SQLite (UTC)."""
    return (dt or utc_now()).astimezone(timezone.utc).strftime(SQL_TS)


def parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, SQL_TS).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def local_dt(ts: str | None) -> datetime | None:
    """Время из SQLite (UTC) -> локальное время."""
    dt = parse_utc(ts)
    return dt.astimezone(LOCAL_TZ) if dt else None


def fmt_ts(ts: str | None, pattern: str = "%d.%m %H:%M") -> str:
    dt = local_dt(ts)
    return dt.strftime(pattern) if dt else ""


def fmt_wait(minutes: int) -> str:
    if minutes >= 1440:
        return f"{minutes // 1440} д {minutes % 1440 // 60} ч"
    if minutes >= 60:
        return f"{minutes // 60} ч {minutes % 60:02d} мин"
    return f"{minutes} мин"


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


def safe_cell(value):
    """Строки, начинающиеся с «=», Excel считал бы формулой — экранируем."""
    if isinstance(value, str) and value.startswith("="):
        return "'" + value
    return value


def norm_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


# ---------- часовые пояса и окна для звонков ----------

def client_local(tz_offset: int | None, now: datetime | None = None) -> datetime:
    """Местное время клиента (смещение задано относительно Москвы)."""
    return (now or utc_now()).astimezone(MSK) + timedelta(hours=tz_offset or 0)


def tz_text(tz_offset: int | None) -> str | None:
    return None if tz_offset is None else TZ_LABEL_BY_OFFSET.get(tz_offset, f"МСК{tz_offset:+d}")


def call_time_display(call_time: str | None, tz_offset: int | None) -> str | None:
    """«Вечер (17–21)» + пояс клиента -> то же окно по Москве."""
    if not call_time:
        return None
    window = CALL_WINDOWS.get(call_time)
    if window and tz_offset:
        start, end = (window[0] - tz_offset) % 24, (window[1] - tz_offset) % 24
        return f"{call_time} у клиента = {start:02d}:00–{end:02d}:00 МСК"
    if window and tz_offset == 0:
        return f"{call_time} МСК"
    return call_time


def lead_priority(tz_offset: int | None, call_time: str | None, now: datetime | None = None) -> int:
    """Чем меньше число, тем раньше заявку нужно выдать модератору.
    0 — клиенту удобно говорить прямо сейчас, 1 — подходит любое время,
    2 — удобное окно сейчас закрыто, 3 — у клиента ночь."""
    hour = client_local(tz_offset, now).hour
    if hour >= NIGHT_FROM or hour < NIGHT_TO:
        return 3
    window = CALL_WINDOWS.get(call_time)
    if window is None:
        return 1
    return 0 if window[0] <= hour < window[1] else 2
