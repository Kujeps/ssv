"""Таблица модератора: свои заявки по запросу и быстрое редактирование прямо в чате.

Таблица: № · дата передачи · анкета · статус · комментарий · перезвонить · доп. информация.
Правка — в один-два нажатия: статус, комментарий (в том числе быстрой фразой), доп. информация,
данные клиента. Совсем быстро: сообщение «#21 текст» — комментарий, «#21+ текст» — доп. информация.
"""
import re
from contextlib import suppress
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message, ReactionTypeEmoji,
)

from config import (
    CALLBACK_OPTIONS, CHOICES, EDIT_FIELDS, LOCAL_TZ, NOCALL_ALERT_ATTEMPTS, QUICK_COMMENTS, STATUSES,
)
from db import (
    add_note, build_table_xlsx, callback_time, change_status, delete_last_note, get_application,
    set_extra, status_label, table_counts, table_page, update_field, update_last_note,
)
from form import ERRORS, NORMALIZERS
from notify import notify_admins, refresh_cards
from roles import IsModerator
from ui import btn, lead_text
from utils import anketa_lines, fmt_ts, parse_when, tz_text

table = Router(name="table")
table.message.filter(F.chat.type == ChatType.PRIVATE)
table.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class TableFlow(StatesGroup):
    text = State()


TABS = [("all", "Все"), ("open", "Активные"), ("cb", "Перезвонить"), ("final", "Закрытые")]
STATUS_MENU = ["reached", "nocall", "callback", "agreed", "refused", "junk"]
TIME_HINT = (
    "Напишите дату и время <b>по Москве</b>, например: <code>07.10 18:00</code>, "
    "<code>завтра 10:00</code> или просто <code>18:30</code>."
)
QUICK_RE = re.compile(r"^#(\d{1,9})(\+?)\s+(\S.*)$", re.S)
CANCEL = InlineKeyboardMarkup(inline_keyboard=[[btn("↩️ Отмена", "tb:cancel")]])


def mine(app: dict | None, user_id: int) -> bool:
    """Заявка закреплена за этим модератором (в любом статусе, кроме «Новая»)."""
    return bool(app and app["assigned_to"] == user_id and app["status"] != "new")


def short(text: str, limit: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------- таблица ----------

def row_block(app: dict) -> str:
    when = fmt_ts(app["assigned_at"] or app["status_at"] or app["created_at"])
    icon = STATUSES.get(app["status"], STATUSES["new"])[0]
    head = f"<b>#{app['id']}</b> · {when} · {icon} {escape(status_label(app))}"
    if app["callback_at"] and app["status"] in ("nocall", "callback"):
        head += f" · 🔁 {fmt_ts(app['callback_at'])}"
    lines = [head] + [escape(x) for x in anketa_lines(app, comment_limit=100)]
    if app["notes"]:
        lines.append("💬 " + escape(short(app["notes"][-1]["text"])))
    if app.get("extra"):
        lines.append("ℹ️ " + escape(short(app["extra"])))
    return "\n".join(lines)


def table_view(user_id: int, flt: str, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    total, page, apps = table_page(user_id, flt, page)
    counts = table_counts(user_id)
    pages = max(1, -(-total // 5))
    title = dict(TABS)[flt]
    text = (
        f"📋 <b>Моя таблица</b> · {title}: {total}" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
        + "\n\n" + ("\n\n".join(row_block(a) for a in apps) if apps else "Здесь пока пусто.")
        + "\n\n<i>Нажмите на заявку, чтобы изменить. Быстро: «#21 текст» — комментарий, "
        "«#21+ текст» — доп. информация.</i>"
    )
    keyboard = [[btn(("• " if k == flt else "") + f"{t} ({counts[k]})", f"tb:v:{k}:0") for k, t in TABS[:2]],
                [btn(("• " if k == flt else "") + f"{t} ({counts[k]})", f"tb:v:{k}:0") for k, t in TABS[2:]]]
    for a in apps:
        keyboard.append([btn(f"✏️ #{a['id']} {(a['name'] or 'без имени')[:22]}", f"tb:e:{a['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"tb:v:{flt}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("➡️", f"tb:v:{flt}:{page + 1}"))
        keyboard.append(nav)
    keyboard.append([btn("📤 Excel", "tb:xl"), btn("🏠 Меню", "mp:menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard), page


async def get_ctx(state: FSMContext) -> dict:
    return (await state.get_data()).get("tbl") or {"flt": "all", "page": 0}


async def show_table(target: Message, user_id: int, state: FSMContext, edit: bool) -> None:
    ctx = await get_ctx(state)
    text, markup, page = table_view(user_id, ctx["flt"], ctx["page"])
    await state.update_data(tbl={"flt": ctx["flt"], "page": page})
    if edit:
        with suppress(TelegramBadRequest):
            await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@table.message(Command("table"), IsModerator())
async def on_table_cmd(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(tbl={"flt": "all", "page": 0})
    await show_table(message, message.from_user.id, state, edit=False)


@table.callback_query(F.data == "tb:open", IsModerator())
async def on_table_open(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(None)
    await state.update_data(tbl={"flt": "all", "page": 0})
    await show_table(cb.message, cb.from_user.id, state, edit=False)


@table.callback_query(F.data.regexp(r"^tb:v:(all|open|cb|final):\d+$"), IsModerator())
async def on_table_view(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, flt, page = cb.data.split(":")
    await cb.answer()
    await state.update_data(tbl={"flt": flt, "page": int(page)})
    await show_table(cb.message, cb.from_user.id, state, edit=True)


@table.callback_query(F.data == "tb:b", IsModerator())
async def on_table_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(None)
    await show_table(cb.message, cb.from_user.id, state, edit=True)


@table.callback_query(F.data == "tb:xl", IsModerator())
async def on_table_excel(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    buf = build_table_xlsx(cb.from_user.id)
    filename = f"my_table_{datetime.now(LOCAL_TZ):%Y-%m-%d_%H%M}.xlsx"
    await bot.send_document(
        cb.message.chat.id, BufferedInputFile(buf.read(), filename=filename),
        caption="📋 Моя таблица: дата передачи, анкета, статус, комментарий, перезвонить, доп. информация",
    )


# ---------- экран редактирования заявки ----------

def edit_text(app: dict, user_id: int) -> str:
    return lead_text(app, "mod", user_id) + "\n\n<i>Что изменить?</i>"


def edit_markup(app: dict) -> InlineKeyboardMarkup:
    i = app["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📊 Статус", f"tb:s:{i}"), btn("💬 Комментарий", f"tb:c:{i}")],
        [btn("ℹ️ Доп. информация", f"tb:x:{i}"), btn("✏️ Данные клиента", f"tb:d:{i}")],
        [btn("⬅️ К таблице", "tb:b")],
    ])


async def render_edit(bot: Bot, chat_id: int, message_id: int, app_id: int, user_id: int) -> None:
    app = get_application(app_id)
    with suppress(TelegramBadRequest):
        await bot.edit_message_text(
            edit_text(app, user_id), chat_id=chat_id, message_id=message_id, reply_markup=edit_markup(app)
        )


async def guard(cb: CallbackQuery, app_id: int) -> dict | None:
    app = get_application(app_id)
    if not mine(app, cb.from_user.id):
        await cb.answer("Эта заявка уже не закреплена за вами", show_alert=True)
        return None
    return app


async def set_markup(cb: CallbackQuery, rows: list[list]) -> None:
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def grid(buttons: list, per_row: int = 2) -> list[list]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


@table.callback_query(F.data.regexp(r"^tb:e:\d+$"), IsModerator())
async def on_edit_open(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    await cb.answer()
    await state.set_state(None)
    await render_edit(bot, cb.message.chat.id, cb.message.message_id, app_id, cb.from_user.id)


@table.callback_query(F.data.regexp(r"^tb:n:\d+$"), IsModerator())
async def on_edit_new(cb: CallbackQuery) -> None:
    """Из карточки заявки: экран редактирования отдельным сообщением."""
    app_id = int(cb.data.rsplit(":", 1)[1])
    app = await guard(cb, app_id)
    if not app:
        return
    await cb.answer()
    await cb.message.answer(edit_text(app, cb.from_user.id), reply_markup=edit_markup(app))


async def finish_edit(bot: Bot, cb: CallbackQuery, app_id: int, toast: str) -> None:
    await cb.answer(toast)
    await render_edit(bot, cb.message.chat.id, cb.message.message_id, app_id, cb.from_user.id)
    await refresh_cards(bot, app_id)


# --- статус ---

@table.callback_query(F.data.regexp(r"^tb:s:\d+$"), IsModerator())
async def on_status_menu(cb: CallbackQuery) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    app = await guard(cb, app_id)
    if not app:
        return
    await cb.answer()
    options = [btn(f"{STATUSES[s][0]} {STATUSES[s][1]}", f"tb:st:{app_id}:{s}") for s in STATUS_MENU if s != app["status"]]
    await set_markup(cb, grid(options) + [[btn("⬅️ Назад", f"tb:e:{app_id}")]])


async def ask_text(cb: CallbackQuery, state: FSMContext, app_id: int, kind: str, prompt: str) -> None:
    await state.set_state(TableFlow.text)
    msg = await cb.message.answer(prompt, reply_markup=CANCEL)
    await state.update_data(kind=kind, app_id=app_id, screen=cb.message.message_id, prompt=msg.message_id)


@table.callback_query(F.data.regexp(r"^tb:st:\d+:\w+$"), IsModerator())
async def on_status_pick(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, _, app_id_str, status = cb.data.split(":")
    app_id, user = int(app_id_str), cb.from_user
    app = await guard(cb, app_id)
    if not app:
        return
    if status in ("agreed", "reached"):
        info = change_status(app_id, user.id, user.full_name, status)
        if info is None:
            await cb.answer("Не получилось изменить статус", show_alert=True)
            return
        await finish_edit(bot, cb, app_id, f"{STATUSES[status][0]} {STATUSES[status][1]}")
    elif status in ("refused", "junk"):
        await cb.answer()
        await ask_text(cb, state, app_id, f"st:{status}",
                       f"{STATUSES[status][0]} <b>Причина (обязательно)</b>\n\nНапишите, почему «{STATUSES[status][1]}».")
    elif status in ("nocall", "callback"):
        await cb.answer()
        rows = [[btn(label, f"tb:tm:{app_id}:{status}:{opt}")] for opt, label in CALLBACK_OPTIONS.items()]
        rows.append([btn("✍️ Своё время", f"tb:tm:{app_id}:{status}:custom")])
        rows.append([btn("⬅️ Назад", f"tb:s:{app_id}")])
        await set_markup(cb, rows)
    else:
        await cb.answer()


@table.callback_query(F.data.regexp(r"^tb:tm:\d+:\w+:\w+$"), IsModerator())
async def on_time_pick(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, _, app_id_str, status, opt = cb.data.split(":")
    app_id, user = int(app_id_str), cb.from_user
    app = await guard(cb, app_id)
    if not app:
        return
    if opt == "custom":
        await cb.answer()
        await ask_text(cb, state, app_id, f"tm:{status}", f"{STATUSES[status][0]} <b>Когда перезвонить?</b>\n\n{TIME_HINT}")
        return
    if opt not in CALLBACK_OPTIONS:
        await cb.answer()
        return
    info = change_status(app_id, user.id, user.full_name, status,
                         callback_dt=callback_time(opt, app["tz_offset"]))
    if info is None:
        await cb.answer("Не получилось изменить статус", show_alert=True)
        return
    await finish_edit(bot, cb, app_id, f"{STATUSES[status][0]} {STATUSES[status][1]}")
    if status == "nocall" and info["attempts"] >= NOCALL_ALERT_ATTEMPTS:
        await notify_admins(bot, f"⚠️ По заявке #{app_id} уже <b>{info['attempts']}</b> недозвона(ов) ({escape(user.full_name)}).")


# --- комментарий ---

@table.callback_query(F.data.regexp(r"^tb:c:\d+$"), IsModerator())
async def on_comment_menu(cb: CallbackQuery) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    await cb.answer()
    phrases = [btn(p, f"tb:q:{app_id}:{i}") for i, p in enumerate(QUICK_COMMENTS)]
    rows = grid(phrases) + [
        [btn("✍️ Написать", f"tb:cw:{app_id}")],
        [btn("✏️ Изменить последний", f"tb:ce:{app_id}"), btn("🗑 Удалить последний", f"tb:cd:{app_id}")],
        [btn("⬅️ Назад", f"tb:e:{app_id}")],
    ]
    await set_markup(cb, rows)


@table.callback_query(F.data.regexp(r"^tb:q:\d+:\d+$"), IsModerator())
async def on_quick_phrase(cb: CallbackQuery, bot: Bot) -> None:
    _, _, app_id_str, idx = cb.data.split(":")
    app_id = int(app_id_str)
    if not await guard(cb, app_id) or not (0 <= int(idx) < len(QUICK_COMMENTS)):
        return
    add_note(app_id, cb.from_user.id, cb.from_user.full_name, QUICK_COMMENTS[int(idx)])
    await finish_edit(bot, cb, app_id, "Комментарий добавлен")


@table.callback_query(F.data.regexp(r"^tb:cw:\d+$"), IsModerator())
async def on_comment_write(cb: CallbackQuery, state: FSMContext) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    await cb.answer()
    await ask_text(cb, state, app_id, "note", "💬 <b>Комментарий</b>\n\nНапишите — добавлю к комментариям заявки.")


@table.callback_query(F.data.regexp(r"^tb:ce:\d+$"), IsModerator())
async def on_comment_edit(cb: CallbackQuery, state: FSMContext) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    app = await guard(cb, app_id)
    if not app:
        return
    mine_notes = [n for n in app["notes"] if n.get("by_name") == cb.from_user.full_name]
    if not mine_notes:
        await cb.answer("У вас пока нет комментариев к этой заявке", show_alert=True)
        return
    await cb.answer()
    await ask_text(cb, state, app_id, "note_edit",
                   f"✏️ <b>Изменить последний комментарий</b>\n\nСейчас: <code>{escape(mine_notes[-1]['text'])}</code>\n\n"
                   "Напишите новый текст — он заменит прежний.")


@table.callback_query(F.data.regexp(r"^tb:cd:\d+$"), IsModerator())
async def on_comment_delete(cb: CallbackQuery, bot: Bot) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    if delete_last_note(app_id, cb.from_user.id) is None:
        await cb.answer("У вас пока нет комментариев к этой заявке", show_alert=True)
        return
    await finish_edit(bot, cb, app_id, "Последний комментарий удалён")


# --- доп. информация ---

@table.callback_query(F.data.regexp(r"^tb:x:\d+$"), IsModerator())
async def on_extra_menu(cb: CallbackQuery) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    await cb.answer()
    await set_markup(cb, [
        [btn("➕ Дописать", f"tb:xa:{app_id}"), btn("✏️ Заменить", f"tb:xs:{app_id}")],
        [btn("🗑 Очистить", f"tb:xc:{app_id}")],
        [btn("⬅️ Назад", f"tb:e:{app_id}")],
    ])


@table.callback_query(F.data.regexp(r"^tb:x[as]:\d+$"), IsModerator())
async def on_extra_write(cb: CallbackQuery, state: FSMContext) -> None:
    mode, app_id = cb.data.split(":")[1], int(cb.data.rsplit(":", 1)[1])
    app = await guard(cb, app_id)
    if not app:
        return
    await cb.answer()
    now = f"Сейчас: <code>{escape(app['extra'])}</code>\n\n" if app.get("extra") else ""
    verb = "допишу новой строкой" if mode == "xa" else "заменю прежний текст"
    await ask_text(cb, state, app_id, mode, f"ℹ️ <b>Доп. информация</b>\n\n{now}Напишите текст — {verb}.")


@table.callback_query(F.data.regexp(r"^tb:xc:\d+$"), IsModerator())
async def on_extra_clear(cb: CallbackQuery, bot: Bot) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    set_extra(app_id, cb.from_user.id, cb.from_user.full_name, "", "clear")
    await finish_edit(bot, cb, app_id, "Доп. информация очищена")


# --- данные клиента ---

@table.callback_query(F.data.regexp(r"^tb:d:\d+$"), IsModerator())
async def on_data_menu(cb: CallbackQuery) -> None:
    app_id = int(cb.data.rsplit(":", 1)[1])
    if not await guard(cb, app_id):
        return
    await cb.answer()
    fields = [btn(label, f"tb:f:{app_id}:{key}") for key, (label, _) in EDIT_FIELDS.items()]
    await set_markup(cb, grid(fields, 3) + [[btn("⬅️ Назад", f"tb:e:{app_id}")]])


def current_value(app: dict, key: str) -> str:
    _, column = EDIT_FIELDS[key]
    value = tz_text(app["tz_offset"]) if key == "tz" else app.get(column)
    return str(value) if value not in (None, "") else "—"


@table.callback_query(F.data.regexp(r"^tb:f:\d+:\w+$"), IsModerator())
async def on_field_pick(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, app_id_str, key = cb.data.split(":")
    app_id = int(app_id_str)
    app = await guard(cb, app_id)
    if not app or key not in EDIT_FIELDS:
        return
    await cb.answer()
    if key in CHOICES:
        options = [btn(label, f"tb:fv:{app_id}:{key}:{i}") for i, label in enumerate(CHOICES[key])]
        rows = grid(options)
        if key != "tz":
            rows.append([btn("✍️ Своё значение", f"tb:fw:{app_id}:{key}")])
        rows.append([btn("⬅️ Назад", f"tb:d:{app_id}")])
        await set_markup(cb, rows)
        return
    await ask_field(cb, state, app, key)


async def ask_field(cb: CallbackQuery, state: FSMContext, app: dict, key: str) -> None:
    label = EDIT_FIELDS[key][0]
    await ask_text(cb, state, app["id"], f"f:{key}",
                   f"✏️ <b>{label}</b>\n\nСейчас: <code>{escape(current_value(app, key))}</code>\n\nОтправьте новое значение.")


@table.callback_query(F.data.regexp(r"^tb:fw:\d+:\w+$"), IsModerator())
async def on_field_write(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, app_id_str, key = cb.data.split(":")
    app = await guard(cb, int(app_id_str))
    if not app or key not in EDIT_FIELDS:
        return
    await cb.answer()
    await ask_field(cb, state, app, key)


@table.callback_query(F.data.regexp(r"^tb:fv:\d+:\w+:\d+$"), IsModerator())
async def on_field_choice(cb: CallbackQuery, bot: Bot) -> None:
    _, _, app_id_str, key, idx = cb.data.split(":")
    app_id = int(app_id_str)
    if not await guard(cb, app_id) or key not in CHOICES or not (0 <= int(idx) < len(CHOICES[key])):
        return
    if not update_field(app_id, cb.from_user.id, cb.from_user.full_name, key, CHOICES[key][int(idx)]):
        await cb.answer("Не получилось изменить", show_alert=True)
        return
    await finish_edit(bot, cb, app_id, f"{EDIT_FIELDS[key][0]} изменено")


# ---------- ввод текста ----------

@table.callback_query(F.data == "tb:cancel", IsModerator())
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await cb.answer("Отменено")
    with suppress(TelegramBadRequest):
        await cb.message.delete()


@table.message(StateFilter(TableFlow.text), F.text, ~F.text.startswith("/"), IsModerator())
async def on_table_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    kind, app_id, user = data.get("kind"), data.get("app_id"), message.from_user
    text = message.text.strip()
    app = get_application(app_id) if app_id else None
    if not mine(app, user.id):
        await state.set_state(None)
        await message.answer("Эта заявка уже не закреплена за вами.")
        return

    ok, error, info = False, None, None
    if kind == "note":
        add_note(app_id, user.id, user.full_name, text[:300])
        ok = True
    elif kind == "note_edit":
        ok = update_last_note(app_id, user.id, text[:300])
    elif kind in ("xa", "xs"):
        ok = set_extra(app_id, user.id, user.full_name, text, "add" if kind == "xa" else "set") is not None
    elif kind.startswith("f:"):
        key = kind[2:]
        value = NORMALIZERS[key](text)
        if value is None:
            error = ERRORS[key]
        else:
            ok = update_field(app_id, user.id, user.full_name, key, value)
    elif kind.startswith("st:"):
        if len(text) < 2:
            error = "Напишите хотя бы пару слов."
        else:
            info = change_status(app_id, user.id, user.full_name, kind[3:], note=f"Причина: {text[:300]}")
            ok = info is not None
    elif kind.startswith("tm:"):
        when = parse_when(text)
        if when is None:
            error = "Не понял время или оно уже прошло. " + TIME_HINT
        else:
            info = change_status(app_id, user.id, user.full_name, kind[3:], callback_dt=when)
            ok = info is not None
    if error:
        await message.answer(error)
        return  # остаёмся в режиме ввода — можно поправить
    await state.set_state(None)
    # Убираем вопрос и ответ из чата, обновляем экран заявки.
    for mid in (data.get("prompt"), message.message_id):
        if mid:
            with suppress(TelegramBadRequest):
                await bot.delete_message(message.chat.id, mid)
    if not ok:
        await message.answer("Не получилось сохранить — заявка уже изменилась.")
        return
    if data.get("screen"):
        await render_edit(bot, message.chat.id, data["screen"], app_id, user.id)
    await refresh_cards(bot, app_id)
    if kind == "tm:nocall" and info and info["attempts"] >= NOCALL_ALERT_ATTEMPTS:
        await notify_admins(bot, f"⚠️ По заявке #{app_id} уже <b>{info['attempts']}</b> недозвона(ов) ({escape(user.full_name)}).")


@table.message(StateFilter(TableFlow.text), IsModerator())
async def on_table_text_other(message: Message) -> None:
    await message.answer("Отправьте текст сообщением или нажмите «Отмена».")


# ---------- быстрый ввод: «#21 текст» / «#21+ текст» ----------

@table.message(F.text.regexp(r"^#\d{1,9}\+?\s+\S"), StateFilter(default_state), IsModerator())
async def on_quick_input(message: Message, bot: Bot) -> None:
    m = QUICK_RE.match(message.text.strip())
    if not m:
        return
    app_id, plus, text = int(m.group(1)), bool(m.group(2)), m.group(3).strip()
    user = message.from_user
    if not mine(get_application(app_id), user.id):
        await message.answer(f"Заявка #{app_id} не закреплена за вами.")
        return
    if plus:
        set_extra(app_id, user.id, user.full_name, text[:300], "add")
        done = f"ℹ️ #{app_id}: доп. информация дописана"
    else:
        add_note(app_id, user.id, user.full_name, text[:300])
        done = f"💬 #{app_id}: комментарий добавлен"
    await refresh_cards(bot, app_id)
    with suppress(Exception):
        await message.react([ReactionTypeEmoji(emoji="👍")])
    await message.answer(done)
