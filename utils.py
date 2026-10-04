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


def tz_short(tz_offset: int | None) -> str | None:
    """«МСК» / «МСК+2» — короткая пометка часового пояса."""
    if tz_offset is None:
        return None
    return "МСК" if tz_offset == 0 else f"МСК{tz_offset:+d}"


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


# ---------- анкета простым текстом (для таблицы и Excel) ----------

def age_text(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return f"{n} лет"
    return f"{n} {'год' if n % 10 == 1 else 'года' if 2 <= n % 10 <= 4 else 'лет'}"


def anketa_lines(app: dict) -> list[str]:
    """Анкета клиента в виде коротких строк (без разметки)."""
    gender = {"Мужской": "муж.", "Женский": "жен."}.get(app.get("gender"), app.get("gender"))
    head = " · ".join(x for x in (app.get("name"), gender, age_text(app["age"]) if app.get("age") else None) if x)
    contacts = " · ".join(x for x in (
        app.get("phone"), app.get("telegram"),
        f"MAX {app['max_contact']}" if app.get("max_contact") else None,
        f"WhatsApp {app['whatsapp']}" if app.get("whatsapp") else None,
    ) if x)
    tz = tz_short(app.get("tz_offset"))
    place = " · ".join(x for x in (
        f"{app['city']} ({tz})" if app.get("city") and tz else app.get("city") or tz_text(app.get("tz_offset")),
        app.get("unit"), app.get("served"),
        f"мед.: {app['medical']}" if app.get("medical") else None,
    ) if x)
    call = call_time_display(app.get("call_time"), app.get("tz_offset"))
    return [x for x in (head, contacts, place, f"Звонить: {call}" if call else None) if x]


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """«07.10 18:00», «18:30», «завтра 10:00», «сегодня 20:00» (по Москве) -> время в UTC.
    Прошедшее время не принимается."""
    now = now or utc_now()
    local = now.astimezone(MSK)
    t = text.strip().lower().replace(",", " ")
    m = re.search(r"(\d{1,2})[:.](\d{2})\s*$", t)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    head = t[:m.start()].strip()
    try:
        if head == "завтра":
            day = (local + timedelta(days=1)).date()
        elif head in ("сегодня", ""):
            day = local.date()
        else:
            d = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", head)
            if not d:
                return None
            year = int(d.group(3)) if d.group(3) else local.year
            year += 2000 if year < 100 else 0
            day = local.replace(year=year, month=int(d.group(2)), day=int(d.group(1))).date()
        when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK)
    except ValueError:
        return None
    if head == "" and when <= local:
        when += timedelta(days=1)  # «18:30» без даты: сегодня, а если уже прошло — завтра
    return when.astimezone(timezone.utc) if when > local else None
