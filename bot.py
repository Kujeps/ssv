import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing, suppress
from datetime import datetime
from html import escape
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x]
ADMIN_USER_IDS = [
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x
]
DB_PATH = os.getenv("DB_PATH", "leads.db")


class Form(StatesGroup):
    name = State()
    phone = State()
    tg = State()
    max = State()
    wa = State()
    confirm = State()


class Broadcast(StatesGroup):
    waiting = State()
    confirm = State()


# Порядок шагов анкеты и соответствие шаг -> состояние.
STEPS = ["name", "phone", "tg", "max", "wa"]
STEP_STATES = {step: getattr(Form, step) for step in STEPS}
CONTACT_KEYS = ["phone", "tg", "max", "wa"]

FIELDS = [
    ("name", "👤", "Имя"),
    ("phone", "📞", "Телефон"),
    ("tg", "✈️", "Telegram"),
    ("max", "💬", "MAX"),
    ("wa", "🟢", "WhatsApp"),
]

GREETING = (
    "👋 <b>Здравствуйте, оставьте заявку и мы свяжемся с вами!</b>\n\n"
    "Это займёт меньше минуты — всего несколько простых шагов."
)

PROMPTS = {
    "name": "👤 <b>Как к вам обращаться?</b>\n\nНапишите ваше имя.",
    "phone": (
        "📞 <b>Номер телефона</b>\n\n"
        "Отправьте номер кнопкой ниже или напишите его вручную.\n"
        "<i>Достаточно указать любой один способ связи — остальные можно пропустить.</i>"
    ),
    "tg": "✈️ <b>Telegram</b>\n\nНапишите ваш @ник или ссылку на профиль.",
    "max": "💬 <b>MAX</b>\n\nУкажите номер телефона или ссылку на ваш профиль в MAX.",
    "wa": "🟢 <b>WhatsApp</b>\n\nУкажите номер, на котором есть WhatsApp.",
}

ERRORS = {
    "name": "Пожалуйста, напишите имя текстом (до 60 символов).",
    "phone": "Не похоже на номер телефона. Пример: <code>+7 999 123-45-67</code>",
    "tg": "Не получилось разобрать ник. Пример: <code>@username</code>",
    "max": "Укажите номер телефона или ссылку/ник в MAX.",
    "wa": "Не похоже на номер телефона. Пример: <code>+7 999 123-45-67</code>",
}

BTN_SHARE = "📱 Поделиться номером"
BTN_SKIP = "⏭ Пропустить"
BTN_BACK = "⬅️ Назад"

bot_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
dp = Dispatcher()

# Анкета заполняется только в личке. Всё, что пишут в группах, бот игнорирует:
# в группу он лишь отправляет готовые заявки через send_message.
dp.message.filter(F.chat.type == ChatType.PRIVATE)
dp.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


# ---------- база ----------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as db, db:
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
        # (запустил бота / начал заявку / завершил заявку) — своя отметка времени,
        # выставляется один раз при первом достижении этапа.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS funnel (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                started_at TEXT,
                started_form_at TEXT,
                completed_at TEXT,
                blocked_at TEXT
            )
            """
        )
        # Миграция для баз, созданных до появления столбца blocked_at.
        cols = {row[1] for row in db.execute("PRAGMA table_info(funnel)")}
        if "blocked_at" not in cols:
            db.execute("ALTER TABLE funnel ADD COLUMN blocked_at TEXT")


def touch_funnel(user: User, stage: str) -> None:
    assert stage in ("started_at", "started_form_at", "completed_at")
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute(
            "INSERT INTO funnel (user_id, username, full_name) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,"
            " full_name = excluded.full_name",
            (user.id, user.username, user.full_name),
        )
        db.execute(
            f"UPDATE funnel SET {stage} = COALESCE({stage}, CURRENT_TIMESTAMP) WHERE user_id = ?",
            (user.id,),
        )


def get_stats() -> dict:
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute(
            "SELECT"
            " COUNT(*) FILTER (WHERE started_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE started_form_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE completed_at IS NOT NULL),"
            " COUNT(*) FILTER (WHERE blocked_at IS NOT NULL)"
            " FROM funnel"
        ).fetchone()
        total_apps = db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    started, started_form, completed, blocked = row
    return {
        "started": started,
        "started_form": started_form,
        "completed": completed,
        "blocked": blocked,
        "total_apps": total_apps,
    }


def get_broadcast_recipients() -> list[int]:
    """Все, кто хоть раз нажимал /start и не заблокировал бота."""
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute(
            "SELECT user_id FROM funnel WHERE started_at IS NOT NULL AND blocked_at IS NULL"
        ).fetchall()
    return [r[0] for r in rows]


def mark_blocked(user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute(
            "UPDATE funnel SET blocked_at = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,)
        )


def build_export_xlsx() -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    headers = [
        "#", "Дата и время", "Имя", "Телефон", "Telegram", "MAX", "WhatsApp",
        "Профиль", "Username", "User ID",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute(
            "SELECT id, created_at, name, phone, telegram, max_contact, whatsapp,"
            " full_name, username, user_id FROM applications ORDER BY id"
        ).fetchall()
    for app_id, created_at, name, phone, tg, max_c, wa, full_name, username, user_id in rows:
        ws.append([
            app_id, created_at, name, phone, tg, max_c, wa,
            full_name, f"@{username}" if username else "", user_id,
        ])
    widths = [5, 19, 16, 16, 14, 14, 14, 20, 14, 12]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def save_application(user: User, data: dict) -> int:
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        cur = db.execute(
            "INSERT INTO applications"
            " (user_id, username, full_name, name, phone, telegram, max_contact, whatsapp)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user.id, user.username, user.full_name,
                data.get("name"), data.get("phone"), data.get("tg"),
                data.get("max"), data.get("wa"),
            ),
        )
        return cur.lastrowid


# ---------- проверка введённых значений ----------

def norm_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def norm_tg(text: str) -> str | None:
    m = re.fullmatch(
        r"(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{4,31})/?", text.strip()
    )
    if m:
        return "@" + m.group(1)
    return norm_phone(text)


def norm_max(text: str) -> str | None:
    phone = norm_phone(text)
    if phone:
        return phone
    return text.strip() if 3 <= len(text.strip()) <= 100 else None


def norm_wa(text: str) -> str | None:
    m = re.search(r"wa\.me/\+?(\d{10,15})", text)
    if m:
        return "+" + m.group(1)
    return norm_phone(text)


def norm_name(text: str) -> str | None:
    text = text.strip()
    return text if 1 <= len(text) <= 60 else None


NORMALIZERS = {"name": norm_name, "phone": norm_phone, "tg": norm_tg, "max": norm_max, "wa": norm_wa}


# ---------- оформление ----------

def header(i: int) -> str:
    bar = "●" * (i + 1) + "○" * (len(STEPS) - i - 1)
    return f"<b>Шаг {i + 1} из {len(STEPS)}</b>  {bar}"


def card(data: dict, skip_empty: bool = False) -> str:
    lines = []
    for key, icon, label in FIELDS:
        value = data.get(key)
        if value:
            lines.append(f"{icon} <b>{label}:</b> {escape(value)}")
        elif not skip_empty:
            lines.append(f"{icon} <b>{label}:</b> —")
    return "\n".join(lines)


def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def step_keyboard(step: str, data: dict, user: User) -> InlineKeyboardMarkup:
    i = STEPS.index(step)
    rows = []
    if step == "name" and user.first_name:
        rows.append([btn(f"👋 Меня зовут {user.first_name}", "use:name")])
    if step == "tg" and user.username:
        rows.append([btn(f"✈️ Мой @{user.username}", "use:tg")])
    if step in ("max", "wa") and data.get("phone"):
        rows.append([btn(f"📞 Тот же номер {data['phone']}", "use:phone")])
    nav = []
    if i > 0:
        nav.append(btn(BTN_BACK, "back"))
    if step != "name":
        nav.append(btn(BTN_SKIP, "skip"))
    if nav:
        rows.append(nav)
    rows.append([btn("✖️ Отмена", "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SHARE, request_contact=True)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_SKIP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="+7 999 123-45-67",
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Отправить заявку", "send")],
            [btn("✏️ Заполнить заново", "restart"), btn(BTN_BACK, "back")],
            [btn("✖️ Отмена", "cancel")],
        ]
    )


def apply_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("📝 Оставить заявку", "apply")]])


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📣 Разослать", "bc_send")],
            [btn("✖️ Отмена", "bc_cancel")],
        ]
    )


# ---------- движок анкеты ----------

async def clear_prev(bot: Bot, chat_id: int, data: dict) -> None:
    """Убирает кнопки с предыдущего вопроса (и клавиатуру с номером телефона)."""
    if data.get("prompt_id"):
        with suppress(TelegramBadRequest):
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=data["prompt_id"], reply_markup=None
            )
    if data.get("reply_kb"):
        with suppress(TelegramBadRequest):
            tmp = await bot.send_message(chat_id, "…", reply_markup=ReplyKeyboardRemove())
            await tmp.delete()


async def show_step(bot: Bot, chat_id: int, state: FSMContext, step: str, user: User) -> None:
    data = await state.get_data()
    await clear_prev(bot, chat_id, data)
    text = f"{header(STEPS.index(step))}\n\n{PROMPTS[step]}"
    markup = phone_keyboard() if step == "phone" else step_keyboard(step, data, user)
    msg = await bot.send_message(chat_id, text, reply_markup=markup)
    await state.set_state(STEP_STATES[step])
    await state.update_data(step=step, prompt_id=msg.message_id, reply_kb=step == "phone")


async def show_confirm(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    await clear_prev(bot, chat_id, data)
    text = f"📋 <b>Проверьте вашу заявку</b>\n\n{card(data)}\n\nВсё верно?"
    msg = await bot.send_message(chat_id, text, reply_markup=confirm_keyboard())
    await state.set_state(Form.confirm)
    await state.update_data(step="confirm", prompt_id=msg.message_id, reply_kb=False)


async def save_and_next(
    bot: Bot, chat_id: int, state: FSMContext, user: User, step: str, value: str | None
) -> None:
    await state.update_data(**{step: value})
    i = STEPS.index(step)
    if i + 1 < len(STEPS):
        await show_step(bot, chat_id, state, STEPS[i + 1], user)
        return
    data = await state.get_data()
    if not any(data.get(k) for k in CONTACT_KEYS):
        await bot.send_message(
            chat_id,
            "⚠️ Нужен хотя бы один способ связи — укажите телефон, Telegram, MAX или WhatsApp.",
        )
        await show_step(bot, chat_id, state, "phone", user)
        return
    await show_confirm(bot, chat_id, state)


async def go_back(bot: Bot, chat_id: int, state: FSMContext, user: User, step: str) -> None:
    if step == "confirm":
        prev = STEPS[-1]
    else:
        prev = STEPS[max(STEPS.index(step) - 1, 0)]
    await show_step(bot, chat_id, state, prev, user)


async def start_form(bot: Bot, chat_id: int, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    await clear_prev(bot, chat_id, data)
    await state.clear()
    await show_step(bot, chat_id, state, "name", user)


async def guard(cb: CallbackQuery, state: FSMContext) -> dict | None:
    """Пропускает только нажатия на кнопки актуального сообщения анкеты."""
    data = await state.get_data()
    if cb.message is None or cb.message.message_id != data.get("prompt_id"):
        await cb.answer("Эта кнопка уже неактуальна")
        return None
    return data


def admin_text(app_id: int, user: User, data: dict) -> str:
    username = f"@{user.username}" if user.username else "нет username"
    return (
        f"🆕 <b>Новая заявка #{app_id}</b>\n\n"
        f"{card(data, skip_empty=True)}\n\n"
        f"🔗 <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a> · "
        f"{escape(username)} · ID <code>{user.id}</code>"
    )


# ---------- обработчики ----------

@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext, bot: Bot) -> None:
    touch_funnel(message.from_user, "started_at")
    await clear_prev(bot, message.chat.id, await state.get_data())
    await state.clear()
    await message.answer(GREETING, reply_markup=apply_keyboard())


@dp.message(Command("cancel"))
async def on_cancel_cmd(message: Message, state: FSMContext, bot: Bot) -> None:
    current = await state.get_state()
    if current in (Broadcast.waiting.state, Broadcast.confirm.state):
        await state.clear()
        await message.answer("🚫 Рассылка отменена.")
        return
    await clear_prev(bot, message.chat.id, await state.get_data())
    await state.clear()
    await message.answer("🚫 Заявка отменена.", reply_markup=apply_keyboard())


@dp.message(Command("stats"))
async def on_stats(message: Message) -> None:
    if message.from_user.id not in ADMIN_USER_IDS:
        return
    s = get_stats()
    started, started_form, completed, blocked = (
        s["started"], s["started_form"], s["completed"], s["blocked"]
    )

    def pct(part: int, whole: int) -> str:
        return f"{part / whole:.0%}" if whole else "—"

    await message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"🚀 Запустили бота: <b>{started}</b>\n"
        f"📝 Начали заполнять заявку: <b>{started_form}</b> ({pct(started_form, started)} от запустивших)\n"
        f"✅ Завершили заявку: <b>{completed}</b> ({pct(completed, started_form)} от начавших, "
        f"{pct(completed, started)} от запустивших)\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b> ({pct(blocked, started)} от запустивших)\n\n"
        f"📨 Всего заявок отправлено: <b>{s['total_apps']}</b>\n\n"
        "<i>Блокировку бот узнаёт только при попытке написать пользователю "
        "(например, во время рассылки), поэтому число может быть занижено.</i>"
    )


@dp.message(Command("export"))
async def on_export(message: Message) -> None:
    if message.from_user.id not in ADMIN_USER_IDS:
        return
    with closing(sqlite3.connect(DB_PATH)) as db:
        count = db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    if not count:
        await message.answer("Заявок пока нет.")
        return
    buf = build_export_xlsx()
    filename = f"leads_{datetime.now():%Y-%m-%d_%H%M}.xlsx"
    await message.answer_document(
        BufferedInputFile(buf.read(), filename=filename),
        caption=f"📊 Заявки: {count} шт.",
    )


@dp.message(Command("broadcast"))
async def on_broadcast_cmd(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_USER_IDS:
        return
    recipients = get_broadcast_recipients()
    if not recipients:
        await message.answer("Пока нет ни одного пользователя, который запускал бота.")
        return
    await state.set_state(Broadcast.waiting)
    await message.answer(
        f"📣 <b>Рассылка</b> ({len(recipients)} получателей)\n\n"
        "Отправьте сообщение, которое разослать — текст, фото, видео или документ. "
        "Оно уйдёт пользователям в точности так, как вы его отправите.\n\n"
        "/cancel — чтобы отменить."
    )


@dp.message(StateFilter(Broadcast.waiting))
async def on_broadcast_content(message: Message, state: FSMContext) -> None:
    recipients = get_broadcast_recipients()
    if not recipients:
        await state.clear()
        await message.answer("Пока нет ни одного пользователя, который запускал бота.")
        return
    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.confirm)
    await message.answer(
        f"👆 Сообщение выше уйдёт <b>{len(recipients)}</b> пользователям. Разослать?",
        reply_markup=broadcast_confirm_keyboard(),
    )


@dp.callback_query(StateFilter(Broadcast.confirm), F.data == "bc_cancel")
async def on_broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("🚫 Рассылка отменена.")


@dp.callback_query(StateFilter(Broadcast.confirm), F.data == "bc_send")
async def on_broadcast_send(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await cb.answer()
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)

    recipients = get_broadcast_recipients()
    total = len(recipients)
    status = await cb.message.answer(f"⏳ Рассылка: 0/{total}")
    ok = fail = 0

    for i, user_id in enumerate(recipients, start=1):
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=data["from_chat_id"],
                message_id=data["message_id"],
            )
            ok += 1
        except TelegramForbiddenError:
            fail += 1
            mark_blocked(user_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=data["from_chat_id"],
                    message_id=data["message_id"],
                )
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            logging.exception("Не удалось разослать сообщение пользователю %s", user_id)
            fail += 1

        if i % 20 == 0 or i == total:
            with suppress(TelegramBadRequest):
                await status.edit_text(f"⏳ Рассылка: {i}/{total} (✅ {ok} · ⚠️ {fail})")
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\nДоставлено: {ok}\nНе доставлено: {fail}"
    )


@dp.callback_query(F.data == "apply")
async def on_apply(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    touch_funnel(cb.from_user, "started_form_at")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    await start_form(bot, cb.message.chat.id, state, cb.from_user)


@dp.callback_query(F.data == "cancel")
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    with suppress(TelegramBadRequest):
        await cb.message.edit_text("🚫 Заявка отменена.", reply_markup=apply_keyboard())


@dp.callback_query(StateFilter(Form.confirm), F.data == "send")
async def on_send(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    await state.clear()
    app_id = save_application(cb.from_user, data)
    touch_funnel(cb.from_user, "completed_at")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text(app_id, cb.from_user, data))
        except Exception:
            logging.exception("Не удалось отправить заявку в чат %s", admin_id)
    name = f", {escape(data['name'])}" if data.get("name") else ""
    await cb.answer("Заявка отправлена!")
    await cb.message.edit_text(
        f"🎉 <b>Заявка отправлена!</b>\n\nСпасибо{name}! Мы свяжемся с вами в ближайшее время.",
        reply_markup=None,
    )


@dp.callback_query(StateFilter(Form.confirm), F.data == "restart")
async def on_restart(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await guard(cb, state) is None:
        return
    await cb.answer()
    await start_form(bot, cb.message.chat.id, state, cb.from_user)


STEP_FILTER = StateFilter(*STEP_STATES.values(), Form.confirm)


@dp.callback_query(STEP_FILTER, F.data == "back")
async def on_back(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    await cb.answer()
    await go_back(bot, cb.message.chat.id, state, cb.from_user, data["step"])


@dp.callback_query(StateFilter(*STEP_STATES.values()), F.data == "skip")
async def on_skip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    await cb.answer()
    await save_and_next(bot, cb.message.chat.id, state, cb.from_user, data["step"], None)


@dp.callback_query(StateFilter(*STEP_STATES.values()), F.data.startswith("use:"))
async def on_use(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    source = cb.data.split(":", 1)[1]
    value = {
        "name": cb.from_user.first_name,
        "tg": f"@{cb.from_user.username}" if cb.from_user.username else None,
        "phone": data.get("phone"),
    }.get(source)
    if not value:
        await cb.answer("Нет данных для подстановки")
        return
    await cb.answer()
    await save_and_next(bot, cb.message.chat.id, state, cb.from_user, data["step"], value)


@dp.message(StateFilter(Form.phone), F.contact)
async def on_contact(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = norm_phone(message.contact.phone_number) or message.contact.phone_number
    await save_and_next(bot, message.chat.id, state, message.from_user, "phone", phone)


@dp.message(StateFilter(*STEP_STATES.values()), F.text, ~F.text.startswith("/"))
async def on_step_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    step = data["step"]
    text = message.text.strip()
    if step == "phone" and text == BTN_BACK:
        await go_back(bot, message.chat.id, state, message.from_user, step)
        return
    if step == "phone" and text == BTN_SKIP:
        await save_and_next(bot, message.chat.id, state, message.from_user, step, None)
        return
    value = NORMALIZERS[step](text)
    if value is None:
        await message.answer(ERRORS[step])
        return
    await save_and_next(bot, message.chat.id, state, message.from_user, step, value)


@dp.callback_query()
async def on_stale_callback(cb: CallbackQuery) -> None:
    await cb.answer("Эта кнопка уже неактуальна. Нажмите /start")


@dp.message(STEP_FILTER)
async def on_step_other(message: Message) -> None:
    await message.answer("Пожалуйста, ответьте текстом или воспользуйтесь кнопками под вопросом.")


@dp.message(StateFilter(Broadcast.confirm))
async def on_broadcast_confirm_other(message: Message) -> None:
    await message.answer("Воспользуйтесь кнопками под сообщением выше — «Разослать» или «Отмена».")


@dp.message()
async def on_anything(message: Message) -> None:
    await message.answer(
        "Чтобы оставить заявку, нажмите кнопку ниже 👇", reply_markup=apply_keyboard()
    )


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Оставить заявку")],
        scope=BotCommandScopeDefault(),
    )
    admin_commands = [
        BotCommand(command="start", description="Оставить заявку"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="export", description="Выгрузить заявки в Excel"),
        BotCommand(command="broadcast", description="Рассылка пользователям"),
    ]
    for admin_id in ADMIN_USER_IDS:
        with suppress(TelegramBadRequest):
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not ADMIN_IDS:
        logging.warning("ADMIN_CHAT_IDS не задан: заявки будут только в базе %s", DB_PATH)
    if not ADMIN_USER_IDS:
        logging.warning("ADMIN_USER_IDS не задан: команда /stats будет недоступна никому")
    init_db()
    bot = Bot(BOT_TOKEN, default=bot_props)
    await setup_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
