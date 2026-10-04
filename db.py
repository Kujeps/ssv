"""Работа с базой данных (SQLite): заявки, очередь, модераторы, архив, статистика."""
import re
import secrets
import sqlite3
import string
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

import config
from config import (
    BLOCKING_STATUSES, CHANGEABLE_STATUSES, EDIT_FIELDS, FINAL_STATUSES, HOLD_MINUTES,
    HOLD_WARN_MINUTES, MSK, OPEN_STATUSES, PARKED_STATUSES, PER_PAGE, SOURCE_CODE_LEN, STATUSES,
    TZ_BY_LABEL, TZ_LABEL_BY_OFFSET,
)
from utils import (
    anketa_lines, call_time_display, client_local, fmt_ts, lead_priority, norm_phone, parse_utc,
    safe_cell, tz_text, utc_now, utc_str,
)


def _pylower(value):
    return value.lower() if isinstance(value, str) else value


def connect() -> sqlite3.Connection:
    """Соединение с БД. SQLite сам не понимает регистр кириллицы — добавляем свою функцию."""
    db = sqlite3.connect(config.DB_PATH)
    db.create_function("pylower", 1, _pylower)
    return db


def ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _marks(items) -> str:
    return ",".join("?" * len(items))


def _log(db, app_id: int, status: str, by_id: int, by_name: str, kind: str,
         holder_id: int | None = None) -> None:
    """История смен статуса. kind: take, take_cb, final, nocall, release, timeout,
    cb_timeout, admin, remove_mod, migrate."""
    db.execute(
        "INSERT INTO status_history (app_id, status, by_id, by_name, kind, holder_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (app_id, status, by_id, by_name, kind, holder_id),
    )


def init_db() -> None:
    with closing(connect()) as db, db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                name TEXT,
                phone TEXT,
                telegram TEXT,
                max_contact TEXT,
                whatsapp TEXT
            )
            """
        )
        # Воронка для статистики: одна строка на пользователя, у каждого этапа
        # (запустил бота / начал заявку / завершил заявку) — своя отметка времени.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS funnel (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                started_at TEXT,
                started_form_at TEXT,
                completed_at TEXT
            )
            """
        )
        ensure_columns(db, "applications", {
            "legacy": "INTEGER DEFAULT 0",
            "gender": "TEXT",
            "medical": "TEXT",
            "age": "INTEGER",
            "city": "TEXT",
            "unit": "TEXT",
            "served": "TEXT",
            "call_time": "TEXT",
            "comment": "TEXT",
            "source": "TEXT",
            "status": "TEXT DEFAULT 'new'",
            "status_by": "TEXT",
            "status_by_id": "INTEGER",
            "status_at": "TEXT",
            "reminded_at": "TEXT",
            "remind_count": "INTEGER DEFAULT 0",
            # Очередь модераторов.
            "tz_offset": "INTEGER",       # часовой пояс клиента: часы от Москвы
            "assigned_to": "INTEGER",     # модератор, который взял заявку
            "assigned_name": "TEXT",
            "taken_at": "TEXT",           # когда взята (для таймера на обработку)
            "callback_at": "TEXT",        # когда перезвонить (после недозвона), UTC
            "attempts": "INTEGER DEFAULT 0",
            "queued_at": "TEXT",          # когда попала в очередь (подана или возвращена)
            "warned": "INTEGER DEFAULT 0",
            "cb_notified": "INTEGER DEFAULT 0",
            "assigned_at": "TEXT",        # когда заявка передана модератору (первое взятие)
            "extra": "TEXT",              # «Доп. информация» — свободные пометки модератора
        })
        ensure_columns(db, "funnel", {
            "blocked_at": "TEXT",
            "source": "TEXT",
            "last_start_at": "TEXT",              # последний /start
            "starts": "INTEGER DEFAULT 0",        # сколько раз нажимал /start
            # Напоминания о незавершённой заявке.
            "rem_count": "INTEGER DEFAULT 0",
            "rem_last_at": "TEXT",
            "rem_first_at": "TEXT",
            "rem_off": "INTEGER DEFAULT 0",       # нажал «Не напоминать»
        })
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_messages (
                app_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        ensure_columns(db, "lead_messages", {"view": "TEXT DEFAULT 'admin'"})
        db.execute("CREATE INDEX IF NOT EXISTS idx_lead_messages_app ON lead_messages (app_id)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                by_id INTEGER,
                by_name TEXT,
                text TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                by_id INTEGER,
                by_name TEXT
            )
            """
        )
        ensure_columns(db, "status_history", {"kind": "TEXT", "holder_id": "INTEGER"})
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER
            )
            """
        )
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                by_id INTEGER,
                by_name TEXT,
                field TEXT,
                old TEXT,
                new TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS start_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                source TEXT,
                is_new INTEGER
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                stage INTEGER,
                kind TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS moderators (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                added_by INTEGER,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notice_id INTEGER
            )
            """
        )
        _migrate_queue(db)


def _migrate_queue(db: sqlite3.Connection) -> None:
    """Все необработанные заявки (в том числе из времён группы менеджеров) — в очередь.
    Условие идемпотентно: у «В работе» / «Не дозвонились» теперь всегда есть модератор."""
    db.execute("UPDATE applications SET status = 'new' WHERE status IS NULL")
    db.execute(
        "UPDATE applications SET queued_at = created_at WHERE queued_at IS NULL AND status = 'new'"
    )
    rows = db.execute(
        f"SELECT id FROM applications WHERE status IN ({_marks(OPEN_STATUSES)}) AND assigned_to IS NULL",
        OPEN_STATUSES,
    ).fetchall()
    for (app_id,) in rows:
        db.execute(
            "UPDATE applications SET status = 'new', status_by = NULL, status_by_id = NULL,"
            " status_at = NULL, queued_at = CURRENT_TIMESTAMP, remind_count = 0,"
            " reminded_at = NULL, taken_at = NULL, callback_at = NULL WHERE id = ?",
            (app_id,),
        )
        _log(db, app_id, "new", 0, "Система", "migrate")


# ---------- воронка и рассылка ----------

def touch_funnel(user, stage: str, source: str | None = None) -> None:
    assert stage in ("started_at", "started_form_at", "completed_at")
    with closing(connect()) as db, db:
        # Источник (метка рекламы) запоминается по первому заходу с меткой.
        db.execute(
            "INSERT INTO funnel (user_id, username, full_name, source) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,"
            " full_name = excluded.full_name,"
            " source = COALESCE(funnel.source, excluded.source)",
            (user.id, user.username, user.full_name, source),
        )
        db.execute(
            f"UPDATE funnel SET {stage} = COALESCE({stage}, CURRENT_TIMESTAMP) WHERE user_id = ?",
            (user.id,),
        )


def had_started_before(user_id: int) -> bool:
    with closing(connect()) as db:
        row = db.execute("SELECT started_at FROM funnel WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def get_broadcast_recipients() -> list[int]:
    """Все, кто хоть раз нажимал /start и не заблокировал бота."""
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT user_id FROM funnel WHERE started_at IS NOT NULL AND blocked_at IS NULL"
        ).fetchall()
    return [r[0] for r in rows]


def mark_blocked(user_id: int) -> None:
    with closing(connect()) as db, db:
        db.execute("UPDATE funnel SET blocked_at = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))


# ---------- рекламные источники ----------

def source_names() -> dict[str, str]:
    with closing(connect()) as db:
        return dict(db.execute("SELECT code, name FROM sources").fetchall())


def source_label(code: str | None) -> str | None:
    """Название источника по коду из ссылки (неизвестный код показываем как есть)."""
    return source_names().get(code, code) if code else None


def source_name_taken(name: str) -> bool:
    return any(n.casefold() == name.casefold() for n in source_names().values())


def create_source(name: str, user_id: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    with closing(connect()) as db, db:
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(SOURCE_CODE_LEN))
            if not db.execute("SELECT 1 FROM sources WHERE code = ?", (code,)).fetchone():
                break
        db.execute(
            "INSERT INTO sources (code, name, created_by) VALUES (?, ?, ?)", (code, name, user_id)
        )
    return code


def list_sources(limit: int = 20) -> tuple[int, list[tuple]]:
    """Последние созданные ссылки с числами: запустили / начали заявку / подали."""
    with closing(connect()) as db:
        total = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        rows = db.execute(
            "SELECT s.code, s.name,"
            " COUNT(f.user_id) FILTER (WHERE f.started_at IS NOT NULL),"
            " COUNT(f.user_id) FILTER (WHERE f.started_form_at IS NOT NULL),"
            " COUNT(f.user_id) FILTER (WHERE f.completed_at IS NOT NULL)"
            " FROM sources s LEFT JOIN funnel f ON f.source = s.code"
            " GROUP BY s.code ORDER BY s.rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return total, rows


def used_sources() -> list[tuple[str, str]]:
    """Источники, по которым есть заявки: (код, название)."""
    names = source_names()
    with closing(connect()) as db:
        codes = [r[0] for r in db.execute(
            "SELECT source FROM applications WHERE source IS NOT NULL"
            " GROUP BY source ORDER BY COUNT(*) DESC LIMIT 20"
        )]
    return [(c, names.get(c, c)) for c in codes]


# ---------- заявки ----------

def save_application(user, data: dict) -> int:
    age = data.get("age")
    with closing(connect()) as db, db:
        row = db.execute("SELECT source FROM funnel WHERE user_id = ?", (user.id,)).fetchone()
        cur = db.execute(
            "INSERT INTO applications"
            " (user_id, username, full_name, name, phone, telegram, max_contact, whatsapp,"
            "  gender, medical, age, city, unit, served, call_time, comment, source,"
            "  tz_offset, status, queued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', CURRENT_TIMESTAMP)",
            (
                user.id, user.username, user.full_name,
                data.get("name"), data.get("phone"), data.get("tg"),
                data.get("max"), data.get("wa"),
                data.get("gender"), data.get("medical"), int(age) if age else None, data.get("city"),
                data.get("unit"), data.get("served"), data.get("call_time"),
                data.get("comment"), row[0] if row else None,
                TZ_BY_LABEL.get(data.get("tz")),
            ),
        )
        return cur.lastrowid


def active_application(user_id: int) -> dict | None:
    """Заявка человека, которая ещё ждёт обработки (блокирует подачу новой)."""
    marks = ",".join("?" * len(BLOCKING_STATUSES))
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id, created_at, COALESCE(status, 'new') AS status FROM applications"
            f" WHERE user_id = ? AND COALESCE(status, 'new') IN ({marks}) ORDER BY id DESC LIMIT 1",
            (user_id, *BLOCKING_STATUSES),
        ).fetchone()
    return dict(row) if row else None


def get_application(app_id: int) -> dict | None:
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            return None
        notes = db.execute(
            "SELECT by_name, text, created_at FROM lead_notes WHERE app_id = ? ORDER BY id",
            (app_id,),
        ).fetchall()
    app = dict(row)
    app["status"] = app["status"] or "new"
    app["notes"] = [dict(n) for n in notes]
    return app


def save_lead_message(app_id: int, chat_id: int, message_id: int, view: str) -> None:
    with closing(connect()) as db, db:
        db.execute(
            "INSERT OR REPLACE INTO lead_messages (app_id, chat_id, message_id, view)"
            " VALUES (?, ?, ?, ?)",
            (app_id, chat_id, message_id, view),
        )


def lead_message_refs(app_id: int) -> list[tuple[int, int, str]]:
    with closing(connect()) as db:
        return db.execute(
            "SELECT chat_id, message_id, COALESCE(view, 'admin') FROM lead_messages WHERE app_id = ?",
            (app_id,),
        ).fetchall()


def add_note(app_id: int, by_id: int, by_name: str, text: str) -> None:
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO lead_notes (app_id, by_id, by_name, text) VALUES (?, ?, ?, ?)",
            (app_id, by_id, by_name, text),
        )


def get_history(app_id: int) -> list[dict]:
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT status, by_name, kind, created_at FROM status_history WHERE app_id = ? ORDER BY id",
            (app_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- модераторы ----------

def add_moderator(user_id: int, name: str | None, username: str | None, added_by: int) -> bool:
    """True — добавлен новый, False — уже был (данные обновлены)."""
    with closing(connect()) as db, db:
        existed = db.execute("SELECT 1 FROM moderators WHERE user_id = ?", (user_id,)).fetchone()
        db.execute(
            "INSERT INTO moderators (user_id, name, username, added_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " name = COALESCE(excluded.name, moderators.name),"
            " username = COALESCE(excluded.username, moderators.username)",
            (user_id, name, username, added_by),
        )
    return not existed


def remove_moderator(user_id: int) -> list[int]:
    """Убирает модератора; его заявки в работе и «перезвонить» возвращаются в очередь."""
    with closing(connect()) as db, db:
        db.execute("DELETE FROM moderators WHERE user_id = ?", (user_id,))
        rows = db.execute(
            f"SELECT id FROM applications WHERE assigned_to = ? AND status IN ({_marks(OPEN_STATUSES)})",
            (user_id, *OPEN_STATUSES),
        ).fetchall()
        for (app_id,) in rows:
            _requeue(db, app_id, 0, "Система", "remove_mod", user_id)
    return [r[0] for r in rows]


def is_moderator(user_id: int) -> bool:
    with closing(connect()) as db:
        return db.execute("SELECT 1 FROM moderators WHERE user_id = ?", (user_id,)).fetchone() is not None


def list_moderators() -> list[dict]:
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM moderators ORDER BY added_at, user_id").fetchall()
    return [dict(r) for r in rows]


def moderator_ids() -> list[int]:
    return [m["user_id"] for m in list_moderators()]


def set_notice_id(user_id: int, message_id: int | None) -> None:
    with closing(connect()) as db, db:
        db.execute("UPDATE moderators SET notice_id = ? WHERE user_id = ?", (message_id, user_id))


def get_notice_id(user_id: int) -> int | None:
    with closing(connect()) as db:
        row = db.execute("SELECT notice_id FROM moderators WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else None


# ---------- очередь ----------

def queue_count() -> int:
    with closing(connect()) as db:
        return db.execute("SELECT COUNT(*) FROM applications WHERE status = 'new'").fetchone()[0]


def queue_summary() -> dict:
    with closing(connect()) as db:
        return dict(db.execute(
            "SELECT COALESCE(status, 'new'), COUNT(*) FROM applications"
            f" WHERE COALESCE(status, 'new') IN ('new', {_marks(OPEN_STATUSES)}) GROUP BY 1",
            OPEN_STATUSES,
        ).fetchall())


def active_lead(mod_id: int) -> dict | None:
    """Заявка, которую модератор сейчас обрабатывает."""
    with closing(connect()) as db:
        row = db.execute(
            "SELECT id FROM applications WHERE status = 'work' AND assigned_to = ?", (mod_id,)
        ).fetchone()
    return get_application(row[0]) if row else None


def parked_leads(mod_id: int) -> list[dict]:
    """Недозвонившиеся заявки модератора: когда пора перезванивать — сверху."""
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, name, status, callback_at, attempts, tz_offset FROM applications"
            f" WHERE status IN ({_marks(PARKED_STATUSES)}) AND assigned_to = ?"
            " ORDER BY callback_at IS NULL, callback_at, id",
            (*PARKED_STATUSES, mod_id),
        ).fetchall()
    return [dict(r) for r in rows]


def take_lead(mod_id: int, mod_name: str, now: datetime | None = None) -> tuple[str, int | None]:
    """Выдаёт модератору заявку из очереди. Возвращает ('taken' | 'busy' | 'empty', id).
    Сначала идут те, кому удобно говорить сейчас (по времени клиента), затем самые старые.
    Пока у модератора есть заявка в работе, новую он не получит ('busy')."""
    with closing(connect()) as db, db:
        db.row_factory = sqlite3.Row
        busy = db.execute(
            "SELECT id FROM applications WHERE status = 'work' AND assigned_to = ?", (mod_id,)
        ).fetchone()
        if busy:
            return "busy", busy["id"]
        rows = db.execute(
            "SELECT id, tz_offset, call_time FROM applications WHERE status = 'new' ORDER BY id"
        ).fetchall()
        if not rows:
            return "empty", None
        best = min(rows, key=lambda r: (lead_priority(r["tz_offset"], r["call_time"], now), r["id"]))
        db.execute(
            "UPDATE applications SET status = 'work', assigned_to = ?, assigned_name = ?,"
            " taken_at = CURRENT_TIMESTAMP, assigned_at = CURRENT_TIMESTAMP, warned = 0,"
            " callback_at = NULL, status_by = ?, status_by_id = ?, status_at = CURRENT_TIMESTAMP,"
            " remind_count = 0, reminded_at = NULL WHERE id = ? AND status = 'new'",
            (mod_id, mod_name, mod_name, mod_id, best["id"]),
        )
        _log(db, best["id"], "work", mod_id, mod_name, "take", mod_id)
    return "taken", best["id"]


def take_callback(mod_id: int, mod_name: str, app_id: int) -> str:
    """Модератор берёт в работу свою заявку из «Перезвонить»: 'taken' | 'busy' | 'gone'."""
    with closing(connect()) as db, db:
        if db.execute(
            "SELECT 1 FROM applications WHERE status = 'work' AND assigned_to = ?", (mod_id,)
        ).fetchone():
            return "busy"
        row = db.execute(
            f"SELECT 1 FROM applications WHERE id = ? AND status IN ({_marks(PARKED_STATUSES)})"
            " AND assigned_to = ?",
            (app_id, *PARKED_STATUSES, mod_id),
        ).fetchone()
        if not row:
            return "gone"
        db.execute(
            "UPDATE applications SET status = 'work', taken_at = CURRENT_TIMESTAMP, warned = 0,"
            " callback_at = NULL, status_by = ?, status_by_id = ?, status_at = CURRENT_TIMESTAMP"
            " WHERE id = ?",
            (mod_name, mod_id, app_id),
        )
        _log(db, app_id, "work", mod_id, mod_name, "take_cb", mod_id)
    return "taken"


def _owned(db, app_id: int, mod_id: int, statuses: tuple[str, ...]) -> bool:
    marks = ",".join("?" * len(statuses))
    return db.execute(
        f"SELECT 1 FROM applications WHERE id = ? AND assigned_to = ? AND status IN ({marks})",
        (app_id, mod_id, *statuses),
    ).fetchone() is not None


def finish_lead(app_id: int, mod_id: int, mod_name: str, status: str, note: str | None = None) -> bool:
    """Итоговый статус (Согласился / Отказался / Мусор). False — заявка уже не у этого модератора."""
    assert status in FINAL_STATUSES
    with closing(connect()) as db, db:
        if not _owned(db, app_id, mod_id, ("work",)):
            return False
        db.execute(
            "UPDATE applications SET status = ?, status_by = ?, status_by_id = ?,"
            " status_at = CURRENT_TIMESTAMP, taken_at = NULL, warned = 0 WHERE id = ?",
            (status, mod_name, mod_id, app_id),
        )
        _log(db, app_id, status, mod_id, mod_name, "final", mod_id)
        if note:
            db.execute(
                "INSERT INTO lead_notes (app_id, by_id, by_name, text) VALUES (?, ?, ?, ?)",
                (app_id, mod_id, mod_name, note),
            )
    return True


def callback_time(option: str, tz_offset: int | None, now: datetime | None = None) -> datetime:
    """Когда перезвонить: через 1/3 часа или завтра в 10:00 по времени клиента (UTC)."""
    now = now or utc_now()
    if option == "1h":
        return now + timedelta(hours=1)
    if option == "3h":
        return now + timedelta(hours=3)
    # 10:00 по времени клиента: сегодня, если ещё не наступило, иначе завтра.
    local = client_local(tz_offset, now)
    target = local.replace(hour=10, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target - timedelta(hours=tz_offset or 0)  # из времени клиента обратно в МСК


def postpone_lead(app_id: int, mod_id: int, mod_name: str, option: str,
                  note: str | None = None) -> int | None:
    """Недозвон: заявка остаётся за модератором в «Перезвонить». Возвращает число попыток."""
    with closing(connect()) as db, db:
        if not _owned(db, app_id, mod_id, ("work",)):
            return None
        tz_offset = db.execute("SELECT tz_offset FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
        when = utc_str(callback_time(option, tz_offset))
        db.execute(
            "UPDATE applications SET status = 'nocall', callback_at = ?, attempts = attempts + 1,"
            " cb_notified = 0, taken_at = NULL, warned = 0, status_by = ?, status_by_id = ?,"
            " status_at = CURRENT_TIMESTAMP WHERE id = ?",
            (when, mod_name, mod_id, app_id),
        )
        _log(db, app_id, "nocall", mod_id, mod_name, "nocall", mod_id)
        if note:
            db.execute(
                "INSERT INTO lead_notes (app_id, by_id, by_name, text) VALUES (?, ?, ?, ?)",
                (app_id, mod_id, mod_name, note),
            )
        return db.execute("SELECT attempts FROM applications WHERE id = ?", (app_id,)).fetchone()[0]


def _requeue(db, app_id: int, by_id: int, by_name: str, kind: str, holder_id: int | None) -> None:
    db.execute(
        "UPDATE applications SET status = 'new', assigned_to = NULL, assigned_name = NULL,"
        " taken_at = NULL, callback_at = NULL, warned = 0, cb_notified = 0, assigned_at = NULL,"
        " queued_at = CURRENT_TIMESTAMP, remind_count = 0, reminded_at = NULL,"
        " status_by = ?, status_by_id = ?, status_at = CURRENT_TIMESTAMP WHERE id = ?",
        (by_name, by_id, app_id),
    )
    _log(db, app_id, "new", by_id, by_name, kind, holder_id)


def release_lead(app_id: int, mod_id: int, mod_name: str, reason: str) -> bool:
    """Модератор сам возвращает заявку в очередь (причина обязательна)."""
    with closing(connect()) as db, db:
        if not _owned(db, app_id, mod_id, OPEN_STATUSES):
            return False
        _requeue(db, app_id, mod_id, mod_name, "release", mod_id)
        db.execute(
            "INSERT INTO lead_notes (app_id, by_id, by_name, text) VALUES (?, ?, ?, ?)",
            (app_id, mod_id, mod_name, f"Возврат в очередь: {reason}"),
        )
    return True


def admin_requeue(app_id: int, admin_id: int, admin_name: str) -> tuple[bool, int | None]:
    """Админ возвращает любую не новую заявку в очередь. Возвращает (успех, прежний модератор)."""
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT COALESCE(status, 'new'), assigned_to FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if not row or row[0] == "new":
            return False, None
        _requeue(db, app_id, admin_id, admin_name, "admin", row[1])
    return True, row[1]


# ---------- фоновые проверки ----------

def expired_holds(now: datetime | None = None) -> list[tuple[int, int]]:
    """Заявки, которые модератор держит дольше HOLD_MINUTES: (id заявки, id модератора)."""
    limit = utc_str((now or utc_now()) - timedelta(minutes=HOLD_MINUTES))
    with closing(connect()) as db:
        return db.execute(
            "SELECT id, assigned_to FROM applications WHERE status = 'work' AND taken_at <= ?",
            (limit,),
        ).fetchall()


def hold_warnings(now: datetime | None = None) -> list[tuple[int, int, int]]:
    """Скоро истекающие заявки без предупреждения: (id, модератор, минут осталось)."""
    warn_after = max(HOLD_MINUTES - HOLD_WARN_MINUTES, 0)
    limit = utc_str((now or utc_now()) - timedelta(minutes=warn_after))
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT id, assigned_to, taken_at FROM applications"
            " WHERE status = 'work' AND warned = 0 AND taken_at <= ?",
            (limit,),
        ).fetchall()
    result = []
    for app_id, mod_id, taken_at in rows:
        left = HOLD_MINUTES - int((((now or utc_now()) - parse_utc(taken_at)).total_seconds()) // 60)
        result.append((app_id, mod_id, max(left, 0)))
    return result


def mark_warned(app_id: int) -> None:
    with closing(connect()) as db, db:
        db.execute("UPDATE applications SET warned = 1 WHERE id = ?", (app_id,))


def due_callbacks(now: datetime | None = None) -> list[tuple[int, int]]:
    """Пора перезванивать, а модератор ещё не уведомлён: (id заявки, id модератора)."""
    with closing(connect()) as db:
        return db.execute(
            "SELECT id, assigned_to FROM applications"
            " WHERE status IN ('nocall', 'callback') AND cb_notified = 0 AND callback_at <= ?",
            (utc_str(now),),
        ).fetchall()


def mark_cb_notified(app_id: int) -> None:
    with closing(connect()) as db, db:
        db.execute("UPDATE applications SET cb_notified = 1 WHERE id = ?", (app_id,))


def overdue_callbacks(now: datetime | None = None) -> list[tuple[int, int]]:
    """Перезвон просрочен больше чем на CALLBACK_RETURN_HOURS."""
    limit = utc_str((now or utc_now()) - timedelta(hours=config.CALLBACK_RETURN_HOURS))
    with closing(connect()) as db:
        return db.execute(
            "SELECT id, assigned_to FROM applications"
            " WHERE status IN ('nocall', 'callback') AND callback_at <= ?",
            (limit,),
        ).fetchall()


def system_requeue(app_id: int, kind: str, holder_id: int | None) -> bool:
    """Автовозврат в очередь (kind: timeout / cb_timeout)."""
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT status FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if not row or row[0] not in OPEN_STATUSES:
            return False
        _requeue(db, app_id, 0, "Система", kind, holder_id)
    return True


# ---------- архив: фильтры и поиск ----------

def period_bounds(key: str, now: datetime | None = None) -> tuple[str, str | None]:
    """Границы периода (в UTC) по местному дню (московскому)."""
    now = (now or utc_now()).astimezone(MSK)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        start, end = day, None
    elif key == "yesterday":
        start, end = day - timedelta(days=1), day
    elif key == "7d":
        start, end = day - timedelta(days=6), None
    else:  # 30d
        start, end = day - timedelta(days=29), None
    return utc_str(start), utc_str(end) if end else None


def _archive_where(filters: dict, scope_mod: int | None) -> tuple[str, list]:
    cond, args = [], []
    if scope_mod is not None:
        cond.append("a.assigned_to = ?")
        args.append(scope_mod)
    if filters.get("status"):
        cond.append("COALESCE(a.status, 'new') = ?")
        args.append(filters["status"])
    if filters.get("mod") is not None:
        cond.append("a.assigned_to = ?")
        args.append(filters["mod"])
    if filters.get("source"):
        cond.append("a.source = ?")
        args.append(filters["source"])
    if filters.get("period"):
        start, end = period_bounds(filters["period"])
        cond.append("COALESCE(a.status_at, a.created_at) >= ?")
        args.append(start)
        if end:
            cond.append("COALESCE(a.status_at, a.created_at) < ?")
            args.append(end)
    q = (filters.get("q") or "").strip()
    if re.fullmatch(r"#\d+", q):  # «#12» — строго заявка с таким номером
        cond.append("a.id = ?")
        args.append(int(q[1:]))
    elif q:
        like = f"%{q.lower().lstrip('#')}%"
        parts = [f"pylower(a.{c}) LIKE ?" for c in
                 ("name", "city", "full_name", "username", "phone", "telegram", "max_contact", "whatsapp")]
        args += [like] * len(parts)
        parts += ["CAST(a.id AS TEXT) = ?", "CAST(a.user_id AS TEXT) = ?"]
        args += [q.lstrip("#"), q]
        phone = norm_phone(q)
        if phone:
            parts += ["a.phone = ?", "a.max_contact = ?", "a.whatsapp = ?", "a.telegram = ?"]
            args += [phone] * 4
        cond.append("(" + " OR ".join(parts) + ")")
    return (" WHERE " + " AND ".join(cond)) if cond else "", args


def archive_page(filters: dict, page: int, scope_mod: int | None = None,
                 per_page: int = PER_PAGE) -> tuple[int, int, list[dict]]:
    """Страница списка заявок: (всего, номер страницы после выравнивания, строки). Новые сверху."""
    where, args = _archive_where(filters, scope_mod)
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        total = db.execute(f"SELECT COUNT(*) FROM applications a{where}", args).fetchone()[0]
        pages = max(1, -(-total // per_page))
        page = min(max(page, 0), pages - 1)
        rows = db.execute(
            "SELECT a.id, a.name, COALESCE(a.status, 'new') AS status, a.created_at,"
            " a.status_at, a.assigned_name, a.city"
            f" FROM applications a{where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            (*args, per_page, page * per_page),
        ).fetchall()
    return total, page, [dict(r) for r in rows]


def archive_moderators() -> list[tuple[int, str]]:
    """Модераторы, у которых есть заявки (в том числе уже удалённые)."""
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT assigned_to, MAX(assigned_name) FROM applications"
            " WHERE assigned_to IS NOT NULL GROUP BY assigned_to ORDER BY MAX(assigned_name)"
        ).fetchall()
    return [(r[0], r[1] or str(r[0])) for r in rows]


# ---------- статистика ----------

def get_stats() -> dict:
    with closing(connect()) as db:
        row = db.execute(
            "SELECT"
            " COUNT(*) FILTER (WHERE started_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE started_form_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE completed_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE blocked_at IS NOT NULL)"
            " FROM funnel"
        ).fetchone()
        total_apps = db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        by_status = dict(db.execute(
            "SELECT COALESCE(status, 'new'), COUNT(*) FROM applications GROUP BY 1"
        ).fetchall())
        sources = db.execute(
            "SELECT COALESCE(source, ''),"
            " COUNT(*) FILTER (WHERE started_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE started_form_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE completed_at IS NOT NULL)"
            " FROM funnel GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ).fetchall()
    started, started_form, completed, blocked = row
    return {
        "started": started,
        "started_form": started_form,
        "completed": completed,
        "blocked": blocked,
        "total_apps": total_apps,
        "by_status": by_status,
        "sources": sources,
    }


def moderator_stats() -> list[dict]:
    """Работа каждого модератора по истории статусов."""
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        mods = {r["user_id"]: r["name"] for r in db.execute("SELECT user_id, name FROM moderators")}
        hist = db.execute(
            "SELECT app_id, status, by_id, by_name, kind, holder_id, created_at"
            " FROM status_history ORDER BY id"
        ).fetchall()
        active = {r["assigned_to"]: r["id"] for r in db.execute(
            "SELECT id, assigned_to FROM applications WHERE status = 'work' AND assigned_to IS NOT NULL")}
        parked: dict[int, int] = {}
        for r in db.execute(
            "SELECT assigned_to, COUNT(*) AS n FROM applications"
            f" WHERE status IN ({_marks(PARKED_STATUSES)}) AND assigned_to IS NOT NULL"
            " GROUP BY assigned_to",
            PARKED_STATUSES,
        ):
            parked[r["assigned_to"]] = r["n"]
    stats: dict[int, dict] = {}

    def entry(uid: int, name: str | None = None) -> dict:
        e = stats.setdefault(uid, {
            "user_id": uid, "name": mods.get(uid) or name or str(uid), "takes": 0,
            "agreed": 0, "refused": 0, "junk": 0, "nocalls": 0, "releases": 0, "timeouts": 0,
            "durations": [], "active": active.get(uid), "parked": parked.get(uid, 0),
            "is_active": uid in mods,
        })
        if name and not mods.get(uid):
            e["name"] = name
        return e

    for uid, name in mods.items():
        entry(uid, name)
    last_take: dict[tuple[int, int], str] = {}
    for h in hist:
        kind, uid = h["kind"], h["by_id"]
        if kind in ("take", "take_cb"):
            entry(uid, h["by_name"])["takes"] += 1
            last_take[(h["app_id"], uid)] = h["created_at"]
        elif kind == "final":
            e = entry(uid, h["by_name"])
            e[h["status"]] += 1
            t0 = parse_utc(last_take.get((h["app_id"], uid)))
            t1 = parse_utc(h["created_at"])
            if t0 and t1:
                e["durations"].append((t1 - t0).total_seconds() / 60)
        elif kind == "nocall":
            entry(uid, h["by_name"])["nocalls"] += 1
        elif kind == "release":
            entry(uid, h["by_name"])["releases"] += 1
        elif kind in ("timeout", "cb_timeout") and h["holder_id"]:
            entry(h["holder_id"])["timeouts"] += 1
    result = []
    for e in stats.values():
        d = e.pop("durations")
        e["avg_minutes"] = int(sum(d) / len(d)) if d else None
        result.append(e)
    return sorted(result, key=lambda e: (not e["is_active"], e["name"].lower()))


# ---------- Excel ----------

EXPORT_COLUMNS = [
    ("#", 5), ("Дата и время", 17), ("Дата передачи", 17), ("Имя", 16), ("Телефон", 16), ("Telegram", 14),
    ("MAX", 14), ("WhatsApp", 14), ("Пол", 10), ("Мед. образование", 16), ("Возраст", 9),
    ("Город", 16), ("Часовой пояс", 18), ("Подразделение", 20), ("Служба ранее", 18),
    ("Удобное время", 30), ("Комментарий клиента", 30), ("Источник", 14), ("Статус", 16),
    ("Модератор", 16), ("Недозвонов", 11), ("Статус изменён", 17), ("Заметки", 40), ("Доп. информация", 40),
    ("Профиль", 20), ("Username", 14), ("User ID", 12),
]


def build_export_xlsx(filters: dict | None = None, scope_mod: int | None = None) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append([name for name, _ in EXPORT_COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    where, args = _archive_where(filters or {}, scope_mod)
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        names = {r[0]: r[1] for r in db.execute("SELECT code, name FROM sources")}
        apps = db.execute(f"SELECT * FROM applications a{where} ORDER BY a.id", args).fetchall()
        notes: dict[int, list[str]] = {}
        for n in db.execute(
            "SELECT app_id, by_name, text, created_at FROM lead_notes ORDER BY id"
        ):
            notes.setdefault(n["app_id"], []).append(
                f"{n['by_name']} ({fmt_ts(n['created_at'])}): {n['text']}"
            )
    for a in apps:
        status = a["status"] or "new"
        ws.append([safe_cell(v) for v in [
            a["id"], fmt_ts(a["created_at"], "%d.%m.%Y %H:%M"),
            fmt_ts(a["assigned_at"], "%d.%m.%Y %H:%M") if a["assigned_at"] else "", a["name"], a["phone"],
            a["telegram"], a["max_contact"], a["whatsapp"], a["gender"], a["medical"], a["age"],
            a["city"], tz_text(a["tz_offset"]), a["unit"], a["served"],
            call_time_display(a["call_time"], a["tz_offset"]), a["comment"],
            names.get(a["source"], a["source"]), STATUSES.get(status, STATUSES["new"])[1],
            a["assigned_name"], a["attempts"] or 0,
            fmt_ts(a["status_at"], "%d.%m.%Y %H:%M") if a["status_at"] else "",
            "\n".join(notes.get(a["id"], [])), a["extra"],
            a["full_name"], f"@{a['username']}" if a["username"] else "", a["user_id"],
        ]])
    for i, (_, width) in enumerate(EXPORT_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------- настройки ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    with closing(connect()) as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def reminders_enabled() -> bool:
    """Напоминания по умолчанию выключены — админ включает их в панели."""
    return get_setting("reminders") == "1"


# ---------- журнал запусков и пользователи ----------

def log_start(user, source: str | None, is_new: bool) -> None:
    """Каждое нажатие /start попадает в журнал (админу в личку больше не пишется)."""
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO start_log (user_id, username, full_name, source, is_new) VALUES (?, ?, ?, ?, ?)",
            (user.id, user.username, user.full_name, source, int(is_new)),
        )
        db.execute(
            "UPDATE funnel SET last_start_at = CURRENT_TIMESTAMP, starts = COALESCE(starts, 0) + 1"
            " WHERE user_id = ?",
            (user.id,),
        )


USER_FILTERS = {
    "all": "1",
    "not_applied": "f.completed_at IS NULL",
    "abandoned": "f.started_form_at IS NOT NULL AND f.completed_at IS NULL",
    "applied": "f.completed_at IS NOT NULL",
    "optout": "COALESCE(f.rem_off, 0) = 1",
    "blocked": "f.blocked_at IS NOT NULL",
}


def _users_where(kind: str, staff_ids: list[int]) -> tuple[str, list]:
    marks = ",".join("?" * len(staff_ids)) or "NULL"
    where = (
        f" WHERE f.started_at IS NOT NULL AND ({USER_FILTERS[kind]})"
        f" AND f.user_id NOT IN ({marks})"
        " AND f.user_id NOT IN (SELECT user_id FROM moderators)"
    )
    return where, list(staff_ids)


def users_counts(staff_ids: list[int]) -> dict[str, int]:
    with closing(connect()) as db:
        result = {}
        for kind in USER_FILTERS:
            where, args = _users_where(kind, staff_ids)
            result[kind] = db.execute(f"SELECT COUNT(*) FROM funnel f{where}", args).fetchone()[0]
    return result


def users_page(kind: str, page: int, staff_ids: list[int], per_page: int = 8) -> tuple[int, int, list[dict]]:
    """Пользователи бота, недавно запускавшие — сверху: (всего, страница, строки)."""
    where, args = _users_where(kind, staff_ids)
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        total = db.execute(f"SELECT COUNT(*) FROM funnel f{where}", args).fetchone()[0]
        pages = max(1, -(-total // per_page))
        page = min(max(page, 0), pages - 1)
        rows = db.execute(
            "SELECT f.user_id, f.username, f.full_name, f.source, f.started_at, f.last_start_at,"
            " f.starts, f.started_form_at, f.completed_at, f.rem_count, f.rem_off, f.blocked_at"
            f" FROM funnel f{where} ORDER BY COALESCE(f.last_start_at, f.started_at) DESC, f.user_id DESC"
            " LIMIT ? OFFSET ?",
            (*args, per_page, page * per_page),
        ).fetchall()
    return total, page, [dict(r) for r in rows]


def build_users_xlsx(kind: str, staff_ids: list[int]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Пользователи"
    headers = [
        ("User ID", 12), ("Имя", 20), ("Username", 16), ("Источник", 16), ("Первый запуск", 17),
        ("Последний запуск", 17), ("Запусков", 10), ("Начал анкету", 14), ("Подал заявку", 14),
        ("Напоминаний", 12), ("Отписался от напоминаний", 22), ("Заблокировал бота", 18),
    ]
    ws.append([h for h, _ in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    names = source_names()
    where, args = _users_where(kind, staff_ids)
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT * FROM funnel f{where} ORDER BY COALESCE(f.last_start_at, f.started_at) DESC", args
        ).fetchall()
    yes = lambda v: "да" if v else ""
    for r in rows:
        ws.append([safe_cell(v) for v in [
            r["user_id"], r["full_name"], f"@{r['username']}" if r["username"] else "",
            names.get(r["source"], r["source"]), fmt_ts(r["started_at"], "%d.%m.%Y %H:%M"),
            fmt_ts(r["last_start_at"] or r["started_at"], "%d.%m.%Y %H:%M"), r["starts"] or 1,
            yes(r["started_form_at"]), yes(r["completed_at"]), r["rem_count"] or 0,
            yes(r["rem_off"]), yes(r["blocked_at"]),
        ]])
    for i, (_, width) in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------- напоминания ----------

def reminder_candidates(staff_ids: list[int]) -> list[dict]:
    """Все, кому положено очередное напоминание: запустили бота, не подали заявку,
    не заблокировали и не отписались. due_at — когда пора слать (UTC), group — start/form."""
    delays = config.REMINDER_DELAYS_HOURS
    marks = ",".join("?" * len(staff_ids)) or "NULL"
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT user_id, started_at, started_form_at, rem_count, rem_last_at FROM funnel"
            " WHERE started_at IS NOT NULL AND completed_at IS NULL AND blocked_at IS NULL"
            " AND COALESCE(rem_off, 0) = 0 AND COALESCE(rem_count, 0) < ?"
            f" AND user_id NOT IN ({marks})"
            " AND user_id NOT IN (SELECT user_id FROM moderators)",
            (len(delays), *staff_ids),
        ).fetchall()
    result = []
    for r in rows:
        stage = r["rem_count"] or 0
        base = parse_utc(r["started_at"] if stage == 0 else r["rem_last_at"] or r["started_at"])
        if base is None:
            continue
        result.append({
            "user_id": r["user_id"], "stage": stage,
            "group": "form" if r["started_form_at"] else "start",
            "due_at": base + timedelta(hours=delays[stage]),
        })
    return sorted(result, key=lambda c: c["due_at"])


def mark_reminder_sent(user_id: int, stage: int, group: str) -> None:
    with closing(connect()) as db, db:
        db.execute(
            "UPDATE funnel SET rem_count = COALESCE(rem_count, 0) + 1, rem_last_at = CURRENT_TIMESTAMP,"
            " rem_first_at = COALESCE(rem_first_at, CURRENT_TIMESTAMP) WHERE user_id = ?",
            (user_id,),
        )
        db.execute(
            "INSERT INTO reminder_log (user_id, stage, kind) VALUES (?, ?, ?)", (user_id, stage, group)
        )


def set_reminders_off(user_id: int) -> None:
    with closing(connect()) as db, db:
        db.execute("UPDATE funnel SET rem_off = 1 WHERE user_id = ?", (user_id,))


def reminder_stats() -> dict:
    with closing(connect()) as db:
        by_stage = dict(db.execute("SELECT stage, COUNT(*) FROM reminder_log GROUP BY stage").fetchall())
        reminded, converted, optout = db.execute(
            "SELECT COUNT(*) FILTER (WHERE rem_first_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE rem_first_at IS NOT NULL AND completed_at >= rem_first_at),"
            " COUNT(*) FILTER (WHERE rem_off = 1) FROM funnel"
        ).fetchone()
    return {
        "enabled": reminders_enabled(), "by_stage": by_stage, "total": sum(by_stage.values()),
        "reminded": reminded, "converted": converted, "optout": optout,
    }



# ---------- таблица модератора: статусы, пометки, правки данных ----------

def _edit_log(db, app_id: int, by_id: int, by_name: str, field: str, old, new) -> None:
    db.execute(
        "INSERT INTO lead_edits (app_id, by_id, by_name, field, old, new) VALUES (?, ?, ?, ?, ?, ?)",
        (app_id, by_id, by_name, field, None if old is None else str(old), None if new is None else str(new)),
    )


def get_edits(app_id: int) -> list[dict]:
    with closing(connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT by_name, field, old, new, created_at FROM lead_edits WHERE app_id = ? ORDER BY id",
            (app_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def change_status(app_id: int, mod_id: int, mod_name: str, new_status: str,
                  note: str | None = None, callback_dt: datetime | None = None) -> dict | None:
    """Модератор меняет статус своей заявки в таблице (из любого статуса, кроме «Новая»).
    Для «Не дозвонились» и «Перезвонить» нужно время перезвона (callback_dt).
    None — заявка не за этим модератором или нет времени перезвона."""
    assert new_status in CHANGEABLE_STATUSES
    with closing(connect()) as db, db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT status, assigned_to, attempts FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if not row or row["assigned_to"] != mod_id or row["status"] not in OPEN_STATUSES + FINAL_STATUSES:
            return None
        when = None
        if new_status in ("nocall", "callback"):
            if callback_dt is None:
                return None
            when = utc_str(callback_dt)
        attempts = (row["attempts"] or 0) + (1 if new_status == "nocall" else 0)
        db.execute(
            "UPDATE applications SET status = ?, status_by = ?, status_by_id = ?,"
            " status_at = CURRENT_TIMESTAMP, taken_at = NULL, warned = 0, callback_at = ?,"
            " cb_notified = 0, attempts = ? WHERE id = ?",
            (new_status, mod_name, mod_id, when, attempts, app_id),
        )
        kind = "final" if new_status in FINAL_STATUSES else new_status
        _log(db, app_id, new_status, mod_id, mod_name, kind, mod_id)
        if note:
            db.execute(
                "INSERT INTO lead_notes (app_id, by_id, by_name, text) VALUES (?, ?, ?, ?)",
                (app_id, mod_id, mod_name, note),
            )
    return {"callback_at": when, "attempts": attempts, "old": row["status"]}


def set_extra(app_id: int, mod_id: int, mod_name: str, text: str, mode: str) -> str | None:
    """Доп. информация: mode = add (дописать строкой) | set (заменить) | clear.
    Возвращает новый текст или None, если заявка не за этим модератором."""
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT extra, assigned_to, COALESCE(status, 'new') FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if not row or row[1] != mod_id or row[2] == "new":
            return None
        old = row[0] or ""
        new = "" if mode == "clear" else (text if mode == "set" or not old else f"{old}\n{text}")
        new = new[:1000]
        db.execute("UPDATE applications SET extra = ? WHERE id = ?", (new or None, app_id))
        _edit_log(db, app_id, mod_id, mod_name, "Доп. информация", old or None, new or None)
    return new


def update_field(app_id: int, mod_id: int, mod_name: str, key: str, value) -> bool:
    """Модератор правит данные клиента. value — уже проверенное значение (для tz — название пояса)."""
    label, column = EDIT_FIELDS[key]
    with closing(connect()) as db, db:
        row = db.execute(
            f"SELECT {column}, assigned_to, COALESCE(status, 'new') FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if not row or row[1] != mod_id or row[2] == "new":
            return False
        old = row[0]
        if key == "tz":
            new = TZ_BY_LABEL[value]
            old_show, new_show = TZ_LABEL_BY_OFFSET.get(old, old), value
        else:
            new = int(value) if key == "age" else value
            old_show, new_show = old, new
        db.execute(f"UPDATE applications SET {column} = ? WHERE id = ?", (new, app_id))
        _edit_log(db, app_id, mod_id, mod_name, label, old_show, new_show)
    return True


def update_last_note(app_id: int, by_id: int, text: str) -> bool:
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT id, text, by_name FROM lead_notes WHERE app_id = ? AND by_id = ? ORDER BY id DESC LIMIT 1",
            (app_id, by_id),
        ).fetchone()
        if not row:
            return False
        db.execute("UPDATE lead_notes SET text = ? WHERE id = ?", (text, row[0]))
        _edit_log(db, app_id, by_id, row[2], "Комментарий", row[1], text)
    return True


def delete_last_note(app_id: int, by_id: int) -> str | None:
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT id, text, by_name FROM lead_notes WHERE app_id = ? AND by_id = ? ORDER BY id DESC LIMIT 1",
            (app_id, by_id),
        ).fetchone()
        if not row:
            return None
        db.execute("DELETE FROM lead_notes WHERE id = ?", (row[0],))
        _edit_log(db, app_id, by_id, row[2], "Комментарий", row[1], None)
    return row[1]


TABLE_FILTERS = {
    "all": ("", ()),
    "open": (f" AND status IN ({_marks(OPEN_STATUSES)})", OPEN_STATUSES),
    "cb": (" AND status IN ('nocall', 'callback')", ()),
    "final": (f" AND status IN ({_marks(FINAL_STATUSES)})", FINAL_STATUSES),
}


def _table_ids(db, mod_id: int, flt: str) -> list[int]:
    cond, args = TABLE_FILTERS[flt]
    order = ("callback_at IS NULL, callback_at, id" if flt == "cb" else
             "(status = 'work') DESC, COALESCE(assigned_at, status_at, created_at) DESC, id DESC")
    return [r[0] for r in db.execute(
        f"SELECT id FROM applications WHERE assigned_to = ?{cond} ORDER BY {order}", (mod_id, *args)
    )]


def table_counts(mod_id: int) -> dict[str, int]:
    with closing(connect()) as db:
        return {f: len(_table_ids(db, mod_id, f)) for f in TABLE_FILTERS}


def table_page(mod_id: int, flt: str, page: int, per_page: int = PER_PAGE) -> tuple[int, int, list[dict]]:
    """Страница таблицы модератора: (всего, номер страницы, заявки целиком с комментариями)."""
    with closing(connect()) as db:
        ids = _table_ids(db, mod_id, flt)
    total = len(ids)
    pages = max(1, -(-total // per_page))
    page = min(max(page, 0), pages - 1)
    return total, page, [get_application(i) for i in ids[page * per_page:(page + 1) * per_page]]


TABLE_COLUMNS = [
    ("№", 6), ("Дата передачи", 16), ("Анкета", 46), ("Статус", 18),
    ("Комментарий модератора", 46), ("Перезвонить", 16), ("Доп. информация", 40),
]


def status_label(app: dict) -> str:
    label = STATUSES.get(app["status"], STATUSES["new"])[1]
    return f"{label} (попытка {app['attempts']})" if app["status"] == "nocall" and app.get("attempts") else label


def build_table_xlsx(mod_id: int) -> BytesIO:
    """«Моя таблица» модератора: дата передачи, анкета, статус, комментарий, перезвон, доп. информация."""
    from openpyxl.styles import Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Моя таблица"
    ws.append([name for name, _ in TABLE_COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    with closing(connect()) as db:
        ids = _table_ids(db, mod_id, "all")
    for app_id in ids:
        a = get_application(app_id)
        comments = "\n".join(f"{fmt_ts(n['created_at'])} {n['text']}" for n in a["notes"])
        ws.append([safe_cell(v) for v in [
            a["id"], fmt_ts(a["assigned_at"] or a["status_at"] or a["created_at"], "%d.%m.%Y %H:%M"),
            "\n".join(anketa_lines(a)), status_label(a), comments,
            fmt_ts(a["callback_at"], "%d.%m.%Y %H:%M") if a["callback_at"] else "", a["extra"],
        ]])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for i, (_, width) in enumerate(TABLE_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
