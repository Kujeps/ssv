"""Админ и модераторы: панели, очередь заявок, архив, управление модераторами, статистика."""
import asyncio
import logging
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter, invert_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import (
    ADMIN_USER_IDS, CALLBACK_OPTIONS, LOCAL_TZ, NOCALL_ALERT_ATTEMPTS, REMINDER_DELAYS_HOURS,
    REMINDER_WINDOW_MSK,
    PER_PAGE, REASON_REQUIRED, STATUSES,
)
from db import (
    active_lead, add_moderator, add_note, admin_requeue, archive_moderators, archive_page,
    build_export_xlsx, create_source, finish_lead, get_application, get_broadcast_recipients,
    get_history, get_stats, list_moderators, list_sources, mark_blocked,
    moderator_stats, parked_leads, postpone_lead, release_lead, remove_moderator,
    save_lead_message, set_setting, source_label, source_name_taken, source_names, take_callback,
    take_lead, used_sources, build_users_xlsx, reminder_stats, reminders_enabled, users_counts,
    users_page,
)
from form import STEP_STATES, Form
from notify import notify_admins, notify_moderators_queue, refresh_cards
from reminders import pending_counts, staff_ids
from roles import IsAdmin, IsModerator, IsStaff, is_admin
from ui import (
    admin_panel, broadcast_confirm_keyboard, btn, lead_keyboard, lead_text, mod_panel,
)
from utils import fmt_ts, fmt_wait, pct, utc_str


class ModFlow(StatesGroup):
    text = State()


class AddMod(StatesGroup):
    waiting = State()


class ArchiveSearch(StatesGroup):
    query = State()


class SourceForm(StatesGroup):
    name = State()


class Broadcast(StatesGroup):
    waiting = State()
    confirm = State()


staff = Router(name="staff")
staff.message.filter(F.chat.type == ChatType.PRIVATE)
staff.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

CLIENT_STATES = StateFilter(*STEP_STATES.values(), Form.confirm)


# ---------- панели ----------

def panel_for(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    return admin_panel(user_id) if is_admin(user_id) else mod_panel(user_id)


async def send_panel(bot: Bot, chat_id: int, user_id: int) -> None:
    text, markup = panel_for(user_id)
    await bot.send_message(chat_id, text, reply_markup=markup)


@staff.message(CommandStart(), IsStaff())
async def on_staff_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await send_panel(bot, message.chat.id, message.from_user.id)


@staff.callback_query(F.data == "mp:menu", IsStaff())
async def on_menu(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    await state.clear()
    await send_panel(bot, cb.message.chat.id, cb.from_user.id)


@staff.message(Command("cancel"), IsStaff(), invert_f(CLIENT_STATES))
async def on_staff_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await message.answer("🚫 Отменено.", reply_markup=ReplyKeyboardRemove())


# ---------- модератор: взять заявку ----------

async def send_card(bot: Bot, chat_id: int, app_id: int) -> None:
    """Карточка заявки модератору (полная — только пока заявка за ним)."""
    app = get_application(app_id)
    msg = await bot.send_message(
        chat_id, lead_text(app, "mod", chat_id), reply_markup=lead_keyboard(app, "mod", chat_id)
    )
    save_lead_message(app_id, chat_id, msg.message_id, "mod")


async def next_prompt(bot: Bot, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn("📥 Взять следующую заявку", "mp:take")], [btn("🏠 Меню", "mp:menu")],
    ]))


@staff.callback_query(F.data == "mp:take", IsModerator())
async def on_take(cb: CallbackQuery, bot: Bot) -> None:
    user = cb.from_user
    status, app_id = take_lead(user.id, user.full_name)
    if status == "empty":
        await cb.answer("Очередь пуста — необработанных заявок нет", show_alert=True)
        return
    if status == "busy":
        await cb.answer("Сначала завершите текущую заявку")
        await send_card(bot, cb.message.chat.id, app_id)
        return
    await cb.answer("Заявка взята")
    await refresh_cards(bot, app_id)
    await send_card(bot, cb.message.chat.id, app_id)


@staff.callback_query(F.data == "mp:active", IsModerator())
async def on_active(cb: CallbackQuery, bot: Bot) -> None:
    app = active_lead(cb.from_user.id)
    if not app:
        await cb.answer("У вас нет заявки в работе", show_alert=True)
        return
    await cb.answer()
    await send_card(bot, cb.message.chat.id, app["id"])


@staff.callback_query(F.data == "mp:cb", IsModerator())
async def on_callbacks_list(cb: CallbackQuery) -> None:
    parked = parked_leads(cb.from_user.id)
    if not parked:
        await cb.answer("Заявок для перезвона нет", show_alert=True)
        return
    await cb.answer()
    now = utc_str()
    rows = []
    for p in parked[:10]:
        due = p["callback_at"] and p["callback_at"] <= now
        label = (f"{'⏰ ' if due else ''}#{p['id']} · {(p['name'] or 'без имени')[:16]}"
                 f" · {fmt_ts(p['callback_at'], '%d.%m %H:%M')} МСК")
        rows.append([btn(label, f"mt:{p['id']}")])
    rows.append([btn("🏠 Меню", "mp:menu")])
    await cb.message.answer(
        "🔁 <b>Перезвонить позже</b>\n\nНажмите на заявку, чтобы позвонить ещё раз. "
        "⏰ — время перезвона уже наступило.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@staff.callback_query(F.data.startswith("mt:"), IsModerator())
async def on_take_callback(cb: CallbackQuery, bot: Bot) -> None:
    app_id = int(cb.data.split(":", 1)[1])
    status = take_callback(cb.from_user.id, cb.from_user.full_name, app_id)
    if status == "busy":
        await cb.answer("Сначала завершите текущую заявку", show_alert=True)
        return
    if status == "gone":
        await cb.answer("Эта заявка недоступна", show_alert=True)
        return
    await cb.answer("Заявка взята")
    await refresh_cards(bot, app_id)
    await send_card(bot, cb.message.chat.id, app_id)


# ---------- модератор: итог по заявке ----------

MOD_PROMPTS = {
    "refused": "❌ <b>Причина отказа</b>\n\nНапишите, почему клиент отказался (обязательно).",
    "junk": "🗑 <b>Почему это мусор?</b>\n\nНапишите причину (обязательно).",
    "release": (
        "↩️ <b>Причина возврата</b>\n\n"
        "Напишите, почему возвращаете заявку в очередь (обязательно)."
    ),
    "note": "✍️ <b>Заметка</b>\n\nНапишите заметку к заявке.",
}
MOD_CANCEL = InlineKeyboardMarkup(inline_keyboard=[[btn("↩️ Отмена", "mx:cancel")]])


def owned_by(app: dict | None, user_id: int) -> bool:
    return bool(app and app["assigned_to"] == user_id and app["status"] in ("work", "nocall"))


@staff.callback_query(F.data.startswith("mv:"), IsModerator())
async def on_mod_action(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, app_id_str, act = cb.data.split(":")
    app_id, user = int(app_id_str), cb.from_user
    app = get_application(app_id)
    if not owned_by(app, user.id):
        await cb.answer("Эта заявка уже не закреплена за вами", show_alert=True)
        await refresh_cards(bot, app_id)
        return
    if act == "agreed":
        if not finish_lead(app_id, user.id, user.full_name, "agreed"):
            await cb.answer("Эта заявка уже не закреплена за вами", show_alert=True)
            return
        await cb.answer("✅ Согласился")
        await refresh_cards(bot, app_id)
        await next_prompt(bot, cb.message.chat.id, f"✅ Заявка #{app_id} закрыта: <b>Согласился</b>.")
    elif act == "nocall":
        await cb.answer()
        rows = [[btn(label, f"mc:{app_id}:{key}")] for key, label in CALLBACK_OPTIONS.items()]
        rows.append([btn("⬅️ Назад", f"mc:{app_id}:back")])
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif act in MOD_PROMPTS:
        await cb.answer()
        await state.set_state(ModFlow.text)
        await state.update_data(kind=act, app_id=app_id)
        await cb.message.answer(MOD_PROMPTS[act], reply_markup=MOD_CANCEL)
    else:
        await cb.answer()


@staff.callback_query(F.data.startswith("mc:"), IsModerator())
async def on_callback_time(cb: CallbackQuery, bot: Bot) -> None:
    _, app_id_str, option = cb.data.split(":")
    app_id, user = int(app_id_str), cb.from_user
    app = get_application(app_id)
    if not owned_by(app, user.id):
        await cb.answer("Эта заявка уже не закреплена за вами", show_alert=True)
        return
    if option == "back":
        await cb.answer()
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=lead_keyboard(app, "mod", user.id))
        return
    attempts = postpone_lead(app_id, user.id, user.full_name, option)
    if attempts is None:
        await cb.answer("Эта заявка уже не закреплена за вами", show_alert=True)
        return
    await cb.answer("📵 Отложено")
    await refresh_cards(bot, app_id)
    when = fmt_ts(get_application(app_id)["callback_at"], "%d.%m %H:%M")
    await next_prompt(
        bot, cb.message.chat.id,
        f"📵 Заявка #{app_id} в разделе «Перезвонить позже»: напомним <b>{when} МСК</b>.",
    )
    if attempts >= NOCALL_ALERT_ATTEMPTS:
        await notify_admins(
            bot, f"⚠️ По заявке #{app_id} уже <b>{attempts}</b> недозвона(ов) ({escape(user.full_name)})."
        )


@staff.callback_query(F.data == "mx:cancel", IsModerator())
async def on_mod_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await cb.answer("Отменено")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)


@staff.message(StateFilter(ModFlow.text), F.text, ~F.text.startswith("/"), IsModerator())
async def on_mod_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    kind, app_id, user = data.get("kind"), data.get("app_id"), message.from_user
    text = message.text.strip()[:300]
    if len(text) < 2:
        await message.answer("Напишите хотя бы пару слов.")
        return
    await state.set_state(None)
    app = get_application(app_id) if app_id else None
    if not owned_by(app, user.id):
        await message.answer("Эта заявка уже не закреплена за вами — возможно, время на неё вышло.")
        return
    if kind in REASON_REQUIRED:
        ok = finish_lead(app_id, user.id, user.full_name, kind, f"Причина: {text}")
        done = f"{STATUSES[kind][0]} Заявка #{app_id} закрыта: <b>{STATUSES[kind][1]}</b>."
    elif kind == "release":
        ok = release_lead(app_id, user.id, user.full_name, text)
        done = f"↩️ Заявка #{app_id} возвращена в очередь."
    else:  # note
        add_note(app_id, user.id, user.full_name, text)
        ok = True
        done = None
    if not ok:
        await message.answer("Эта заявка уже не закреплена за вами.")
        return
    await refresh_cards(bot, app_id)
    if done:
        await next_prompt(bot, message.chat.id, done)
    else:
        await message.answer("✍️ Заметка сохранена.")
    if kind == "release":
        await notify_moderators_queue(bot, "↩️ Заявка вернулась в очередь")


@staff.message(StateFilter(ModFlow.text), IsStaff())
async def on_mod_text_other(message: Message) -> None:
    await message.answer("Отправьте текст сообщением или нажмите «Отмена» под вопросом.")


# ---------- модератор: статистика ----------

def moderator_line(e: dict) -> str:
    parts = [
        f"взял {e['takes']}", f"✅ {e['agreed']}", f"❌ {e['refused']}", f"🗑 {e['junk']}",
        f"📵 {e['nocalls']}", f"↩️ {e['releases']}", f"⏱ автовозвр. {e['timeouts']}",
    ]
    if e["avg_minutes"] is not None:
        parts.append(f"ср. {fmt_wait(e['avg_minutes'])}")
    if e["active"]:
        parts.append(f"сейчас: #{e['active']}")
    if e["parked"]:
        parts.append(f"перезвонить: {e['parked']}")
    return f"👷 <b>{escape(e['name'])}</b> — " + " · ".join(parts)


@staff.callback_query(F.data == "mp:stats", IsModerator())
async def on_my_stats(cb: CallbackQuery) -> None:
    await cb.answer()
    mine = next((e for e in moderator_stats() if e["user_id"] == cb.from_user.id), None)
    text = "📊 <b>Моя статистика</b>\n\n" + (moderator_line(mine) if mine else "Пока нет данных.")
    await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("🏠 Меню", "mp:menu")]]))


# ---------- админ: ручной возврат в очередь ----------

@staff.callback_query(F.data.startswith("ad:"), IsAdmin())
async def on_admin_requeue(cb: CallbackQuery, bot: Bot) -> None:
    _, app_id_str, act = cb.data.split(":")
    app_id = int(app_id_str)
    if act != "requeue":
        await cb.answer()
        return
    ok, holder = admin_requeue(app_id, cb.from_user.id, cb.from_user.full_name)
    if not ok:
        await cb.answer("Заявка уже в очереди", show_alert=True)
        return
    await cb.answer("↩️ Возвращена в очередь")
    await refresh_cards(bot, app_id)
    if holder:
        with suppress(Exception):
            await bot.send_message(holder, f"↩️ Заявка #{app_id} возвращена администратором в очередь.")
    await notify_moderators_queue(bot, "↩️ Заявка вернулась в очередь")


# ---------- админ: статистика и Excel ----------

def build_stats_text() -> str:
    s = get_stats()
    started, started_form, completed, blocked = (
        s["started"], s["started_form"], s["completed"], s["blocked"]
    )
    by_status = s["by_status"]
    workable = s["total_apps"] - by_status.get("junk", 0)
    agreed = by_status.get("agreed", 0)
    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        f"🚀 Запустили бота: <b>{started}</b>",
        f"📝 Начали заполнять заявку: <b>{started_form}</b> ({pct(started_form, started)} от запустивших)",
        f"✅ Завершили заявку: <b>{completed}</b> ({pct(completed, started_form)} от начавших, "
        f"{pct(completed, started)} от запустивших)",
        f"🚫 Заблокировали бота: <b>{blocked}</b> ({pct(blocked, started)} от запустивших)",
        "",
        f"📨 Всего заявок отправлено: <b>{s['total_apps']}</b>",
        "",
        "📋 <b>Заявки по статусам</b>",
    ]
    for key, (icon, label) in STATUSES.items():
        title = "В очереди" if key == "new" else label
        lines.append(f"{icon} {title}: <b>{by_status.get(key, 0)}</b>")
    lines.append(
        f"🎯 Согласились: <b>{agreed}</b> из {workable} ({pct(agreed, workable)}, без учёта мусора)"
    )
    mods = moderator_stats()
    if mods:
        lines += ["", "👷 <b>Модераторы</b>"]
        lines += [moderator_line(e) for e in mods]
    rem = reminder_stats()
    lines += ["", f"🔔 <b>Напоминания</b> — {'включены' if rem['enabled'] else 'выключены'}"]
    if rem["total"] or rem["enabled"]:
        stages = " · ".join(f"{i + 1}-е: {rem['by_stage'].get(i, 0)}" for i in range(len(REMINDER_DELAYS_HOURS)))
        lines += [
            f"Отправлено: <b>{rem['total']}</b> ({stages})",
            f"Получили хотя бы одно: <b>{rem['reminded']}</b> · после этого подали заявку: "
            f"<b>{rem['converted']}</b> ({pct(rem['converted'], rem['reminded'])})",
            f"Отписались: <b>{rem['optout']}</b>",
        ]
    if s["sources"]:
        lines += ["", "🔖 <b>Источники</b> <i>(запустили → начали → подали)</i>"]
        names = source_names()
        for source, st, fm, done in s["sources"]:
            name = escape(names.get(source, source)) if source else "без метки"
            lines.append(f"<b>{name}</b> — {st} → {fm} → {done} ({pct(done, st)})")
    lines += [
        "",
        "<i>Блокировку бот узнаёт только при попытке написать пользователю "
        "(например, во время рассылки), поэтому число может быть занижено.</i>",
    ]
    return "\n".join(lines)


@staff.message(Command("stats"), IsAdmin())
async def on_stats(message: Message) -> None:
    await message.answer(build_stats_text())


@staff.callback_query(F.data == "ap:stats", IsAdmin())
async def on_stats_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(build_stats_text())


async def send_export(bot: Bot, chat_id: int, filters: dict | None = None, caption: str = "") -> None:
    from datetime import datetime
    buf = build_export_xlsx(filters)
    filename = f"leads_{datetime.now(LOCAL_TZ):%Y-%m-%d_%H%M}.xlsx"
    await bot.send_document(chat_id, BufferedInputFile(buf.read(), filename=filename), caption=caption)


@staff.message(Command("export"), IsAdmin())
async def on_export(message: Message, bot: Bot) -> None:
    total, _, _ = archive_page({}, 0)
    if not total:
        await message.answer("Заявок пока нет.")
        return
    await send_export(bot, message.chat.id, None, f"📊 Заявки: {total} шт.")


@staff.callback_query(F.data == "ap:export", IsAdmin())
async def on_export_cb(cb: CallbackQuery, bot: Bot) -> None:
    total, _, _ = archive_page({}, 0)
    if not total:
        await cb.answer("Заявок пока нет", show_alert=True)
        return
    await cb.answer()
    await send_export(bot, cb.message.chat.id, None, f"📊 Заявки: {total} шт.")


# ---------- админ: модераторы ----------

def moderator_title(m: dict) -> str:
    name = m["name"] or f"ID {m['user_id']}"
    return escape(name) + (f" (@{escape(m['username'])})" if m["username"] else "")


def moderators_view() -> tuple[str, InlineKeyboardMarkup]:
    mods = list_moderators()
    stats = {e["user_id"]: e for e in moderator_stats()}
    lines = [f"👥 <b>Модераторы ({len(mods)})</b>"]
    if not mods:
        lines += ["", "Пока нет ни одного модератора. Нажмите «Добавить модератора»."]
    rows = []
    for m in mods:
        e = stats.get(m["user_id"], {})
        info = []
        if e.get("active"):
            info.append(f"🔧 в работе #{e['active']}")
        if e.get("parked"):
            info.append(f"🔁 перезвонить: {e['parked']}")
        lines += ["", f"• {moderator_title(m)} · ID <code>{m['user_id']}</code>" + (" · " + " · ".join(info) if info else "")]
        rows.append([btn(f"🗑 Убрать: {(m['name'] or m['user_id'])}"[:40], f"md:del:{m['user_id']}")])
    rows.append([btn("➕ Добавить модератора", "md:add")])
    rows.append([btn("🏠 Меню", "mp:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@staff.message(Command("moderators"), IsAdmin())
async def on_moderators_cmd(message: Message) -> None:
    text, markup = moderators_view()
    await message.answer(text, reply_markup=markup)


@staff.callback_query(F.data == "ap:mods", IsAdmin())
async def on_moderators_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    text, markup = moderators_view()
    await cb.message.answer(text, reply_markup=markup)


@staff.callback_query(F.data == "md:add", IsAdmin())
async def on_mod_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(AddMod.waiting)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(
                text="👤 Выбрать человека из контактов",
                request_users=KeyboardButtonRequestUsers(
                    request_id=1, user_is_bot=False, max_quantity=1,
                    request_name=True, request_username=True,
                ),
            )],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await cb.message.answer(
        "➕ <b>Новый модератор</b>\n\nНажмите кнопку ниже и выберите человека из своих контактов. "
        "Или отправьте его числовой Telegram ID (узнать можно в @userinfobot).\n\n"
        "<i>Модератору нужно один раз нажать /start у этого бота, иначе бот не сможет ему писать.</i>",
        reply_markup=keyboard,
    )


async def set_moderator_commands(bot: Bot, user_id: int) -> None:
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.set_my_commands(
            [BotCommand(command="start", description="Панель модератора")],
            scope=BotCommandScopeChat(chat_id=user_id),
        )


async def register_moderator(message: Message, bot: Bot, user_id: int, name: str | None,
                             username: str | None) -> str:
    if user_id in ADMIN_USER_IDS:
        pass  # админ может быть и модератором
    is_new = add_moderator(user_id, name, username, message.from_user.id)
    await set_moderator_commands(bot, user_id)
    delivered = True
    try:
        await bot.send_message(user_id, "👷 Вас назначили модератором. Нажмите /start, чтобы открыть панель.")
    except Exception:
        delivered = False
    title = escape(name or f"ID {user_id}")
    line = f"✅ Модератор <b>{title}</b> {'добавлен' if is_new else 'уже был в списке'}."
    if not delivered:
        line += " Он ещё не запускал бота — попросите его нажать /start."
    return line


@staff.message(StateFilter(AddMod.waiting), F.users_shared, IsAdmin())
async def on_mod_shared(message: Message, state: FSMContext, bot: Bot) -> None:
    lines = []
    for u in message.users_shared.users:
        name = " ".join(x for x in (u.first_name, u.last_name) if x) or None
        lines.append(await register_moderator(message, bot, u.user_id, name, u.username))
    await state.set_state(None)
    await message.answer("\n".join(lines), reply_markup=ReplyKeyboardRemove())
    text, markup = moderators_view()
    await message.answer(text, reply_markup=markup)


@staff.message(StateFilter(AddMod.waiting), F.text, IsAdmin())
async def on_mod_text_add(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text.strip()
    if text.lower() in ("отмена", "/cancel"):
        await state.set_state(None)
        await message.answer("🚫 Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    if not text.lstrip("-").isdigit():
        await message.answer("Отправьте числовой ID или выберите человека кнопкой.")
        return
    line = await register_moderator(message, bot, int(text), None, None)
    await state.set_state(None)
    await message.answer(line, reply_markup=ReplyKeyboardRemove())
    text, markup = moderators_view()
    await message.answer(text, reply_markup=markup)


@staff.callback_query(F.data.startswith("md:del:"), IsAdmin())
async def on_mod_del(cb: CallbackQuery) -> None:
    user_id = int(cb.data.rsplit(":", 1)[1])
    mod = next((m for m in list_moderators() if m["user_id"] == user_id), None)
    if not mod:
        await cb.answer("Такого модератора уже нет", show_alert=True)
        return
    await cb.answer()
    e = next((x for x in moderator_stats() if x["user_id"] == user_id), {})
    open_leads = (1 if e.get("active") else 0) + e.get("parked", 0)
    await cb.message.answer(
        f"Убрать модератора <b>{moderator_title(mod)}</b>?\n\n"
        f"Его заявки в работе и «Перезвонить» (<b>{open_leads}</b>) вернутся в очередь. "
        "Обработанные заявки останутся в архиве.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Убрать", f"md:ok:{user_id}")], [btn("↩️ Отмена", "ap:mods")],
        ]),
    )


@staff.callback_query(F.data.startswith("md:ok:"), IsAdmin())
async def on_mod_del_ok(cb: CallbackQuery, bot: Bot) -> None:
    user_id = int(cb.data.rsplit(":", 1)[1])
    returned = remove_moderator(user_id)
    await cb.answer("Модератор убран")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    for app_id in returned:
        await refresh_cards(bot, app_id)
    with suppress(Exception):
        await bot.send_message(user_id, "Доступ модератора отозван.")
    text = "✅ Модератор убран." + (f" Заявок возвращено в очередь: {len(returned)}." if returned else "")
    await cb.message.answer(text)
    if returned:
        await notify_moderators_queue(bot, "↩️ Заявки вернулись в очередь")
    text, markup = moderators_view()
    await cb.message.answer(text, reply_markup=markup)


# ---------- архив заявок: фильтры, поиск, карточка ----------

STATUS_FILTERS = [
    ("all", "Все"), ("new", "🆕 Новые"), ("work", "🔧 В работе"), ("nocall", "📵 Не дозвонились"),
    ("agreed", "✅ Согласился"), ("refused", "❌ Отказался"), ("junk", "🗑 Мусор"),
]
PERIOD_FILTERS = [
    ("all", "Всё время"), ("today", "Сегодня"), ("yesterday", "Вчера"),
    ("7d", "7 дней"), ("30d", "30 дней"),
]
HISTORY_LABELS = {
    "take": "взял в работу", "take_cb": "взял на перезвон", "nocall": "не дозвонился",
    "release": "вернул в очередь", "timeout": "автовозврат: время вышло",
    "cb_timeout": "автовозврат: перезвон просрочен", "admin": "вернул админ",
    "remove_mod": "модератор убран → в очередь", "migrate": "перенесена в очередь",
}


def scope_for(user_id: int) -> int | None:
    """Админ видит все заявки, модератор — только свои."""
    return None if is_admin(user_id) else user_id


async def get_arch(state: FSMContext) -> dict:
    return (await state.get_data()).get("arch") or {"filters": {}, "page": 0}


def filters_text(filters: dict) -> str:
    parts = []
    if filters.get("status"):
        parts.append(f"статус — {STATUSES[filters['status']][1]}")
    if filters.get("period"):
        parts.append("период — " + dict(PERIOD_FILTERS)[filters["period"]])
    if filters.get("mod") is not None:
        names = dict(archive_moderators())
        parts.append(f"модератор — {escape(names.get(filters['mod'], str(filters['mod'])))}")
    if filters.get("source"):
        parts.append(f"источник — {escape(source_label(filters['source']))}")
    if filters.get("q"):
        parts.append(f"поиск — «{escape(filters['q'])}»")
    return " · ".join(parts) if parts else "не заданы"


def archive_view(user_id: int, arch: dict) -> tuple[str, InlineKeyboardMarkup]:
    admin = is_admin(user_id)
    total, page, rows = archive_page(arch["filters"], arch["page"], scope_for(user_id))
    arch["page"] = page
    pages = max(1, -(-total // PER_PAGE))
    title = "🗂 <b>Все заявки</b>" if admin else "🗂 <b>Мои заявки</b>"
    text = (
        f"{title}: {total}\n"
        f"Фильтры: {filters_text(arch['filters'])}\n\n"
        + ("Нажмите на заявку, чтобы открыть карточку." if total else "По этим фильтрам заявок нет.")
    )
    keyboard = []
    for r in rows:
        icon = STATUSES.get(r["status"], STATUSES["new"])[0]
        when = fmt_ts(r["status_at"] or r["created_at"], "%d.%m")
        label = f"{icon} #{r['id']} {(r['name'] or 'без имени')[:14]}"
        if admin and r["assigned_name"]:
            label += f" · {r['assigned_name'][:10]}"
        keyboard.append([btn(f"{label} · {when}", f"ar:o:{r['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"ar:p:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("➡️", f"ar:p:{page + 1}"))
        keyboard.append(nav)
    keyboard.append([btn("📋 Статус", "ar:f:status"), btn("📅 Период", "ar:f:period")])
    if admin:
        keyboard.append([btn("👷 Модератор", "ar:f:mod"), btn("🔖 Источник", "ar:f:src")])
    keyboard.append([btn("🔍 Поиск", "ar:q"), btn("♻️ Сбросить", "ar:reset")])
    if admin and total:
        keyboard.append([btn("📤 Excel по фильтру", "ar:x")])
    keyboard.append([btn("🏠 Меню", "mp:menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_archive(target: Message, user_id: int, state: FSMContext, edit: bool) -> None:
    arch = await get_arch(state)
    text, markup = archive_view(user_id, arch)
    await state.update_data(arch=arch)
    if edit:
        with suppress(TelegramBadRequest):
            await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@staff.callback_query(F.data == "ap:arch", IsAdmin())
@staff.callback_query(F.data == "mp:arch", IsModerator())
async def on_archive_open(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(arch={"filters": {}, "page": 0})
    await show_archive(cb.message, cb.from_user.id, state, edit=False)


@staff.message(Command("archive"), IsStaff())
async def on_archive_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(arch={"filters": {}, "page": 0})
    await show_archive(message, message.from_user.id, state, edit=False)


@staff.callback_query(F.data.startswith("ar:p:"), IsStaff())
async def on_archive_page(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    arch = await get_arch(state)
    arch["page"] = int(cb.data.rsplit(":", 1)[1])
    await state.update_data(arch=arch)
    await show_archive(cb.message, cb.from_user.id, state, edit=True)


@staff.callback_query(F.data == "ar:b", IsStaff())
async def on_archive_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await show_archive(cb.message, cb.from_user.id, state, edit=True)


@staff.callback_query(F.data == "ar:reset", IsStaff())
async def on_archive_reset(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Фильтры сброшены")
    await state.update_data(arch={"filters": {}, "page": 0})
    await show_archive(cb.message, cb.from_user.id, state, edit=True)


def options_markup(rows: list[tuple[str, str]], per_row: int = 2) -> InlineKeyboardMarkup:
    buttons = [btn(label, data) for data, label in rows]
    keyboard = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    keyboard.append([btn("⬅️ К списку", "ar:b")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@staff.callback_query(F.data.startswith("ar:f:"), IsStaff())
async def on_archive_filter_menu(cb: CallbackQuery, state: FSMContext) -> None:
    kind, admin = cb.data.rsplit(":", 1)[1], is_admin(cb.from_user.id)
    await cb.answer()
    if kind == "status":
        options = [(f"ar:s:{k}", v) for k, v in STATUS_FILTERS if admin or k not in ("new",)]
        title = "📋 Выберите статус"
    elif kind == "period":
        options = [(f"ar:pe:{k}", v) for k, v in PERIOD_FILTERS]
        title = "📅 Выберите период (по дате последнего изменения заявки)"
    elif kind == "mod" and admin:
        options = [("ar:m:all", "Все")] + [(f"ar:m:{i}", n[:24]) for i, n in archive_moderators()]
        title = "👷 Выберите модератора"
    elif kind == "src" and admin:
        sources = used_sources()
        arch = await get_arch(state)
        arch["sources"] = [code for code, _ in sources]
        await state.update_data(arch=arch)
        options = [("ar:so:all", "Все")] + [(f"ar:so:{i}", n[:24]) for i, (_, n) in enumerate(sources)]
        title = "🔖 Выберите источник"
    else:
        return
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(title, reply_markup=options_markup(options))


async def set_filter(cb: CallbackQuery, state: FSMContext, key: str, value) -> None:
    arch = await get_arch(state)
    if value in (None, "all"):
        arch["filters"].pop(key, None)
    else:
        arch["filters"][key] = value
    arch["page"] = 0
    await state.update_data(arch=arch)
    await cb.answer()
    await show_archive(cb.message, cb.from_user.id, state, edit=True)


@staff.callback_query(F.data.startswith("ar:s:"), IsStaff())
async def on_filter_status(cb: CallbackQuery, state: FSMContext) -> None:
    await set_filter(cb, state, "status", cb.data.rsplit(":", 1)[1])


@staff.callback_query(F.data.startswith("ar:pe:"), IsStaff())
async def on_filter_period(cb: CallbackQuery, state: FSMContext) -> None:
    await set_filter(cb, state, "period", cb.data.rsplit(":", 1)[1])


@staff.callback_query(F.data.startswith("ar:m:"), IsAdmin())
async def on_filter_mod(cb: CallbackQuery, state: FSMContext) -> None:
    raw = cb.data.rsplit(":", 1)[1]
    await set_filter(cb, state, "mod", None if raw == "all" else int(raw))


@staff.callback_query(F.data.startswith("ar:so:"), IsAdmin())
async def on_filter_source(cb: CallbackQuery, state: FSMContext) -> None:
    raw = cb.data.rsplit(":", 1)[1]
    arch = await get_arch(state)
    sources = arch.get("sources") or []
    value = None if raw == "all" or not raw.isdigit() or int(raw) >= len(sources) else sources[int(raw)]
    await set_filter(cb, state, "source", value)


@staff.callback_query(F.data == "ar:q", IsStaff())
async def on_archive_search(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(ArchiveSearch.query)
    await cb.message.answer(
        "🔍 <b>Поиск</b>\n\nНапишите имя, телефон, город, @username или номер заявки (например, "
        "<code>#12</code>).\n\n/cancel — отмена"
    )


@staff.message(StateFilter(ArchiveSearch.query), F.text, ~F.text.startswith("/"), IsStaff())
async def on_archive_query(message: Message, state: FSMContext) -> None:
    arch = await get_arch(state)
    arch["filters"]["q"] = message.text.strip()[:60]
    arch["page"] = 0
    await state.set_state(None)
    await state.update_data(arch=arch)
    await show_archive(message, message.from_user.id, state, edit=False)


@staff.callback_query(F.data == "ar:x", IsAdmin())
async def on_archive_export(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    arch = await get_arch(state)
    total, _, _ = archive_page(arch["filters"], 0)
    if not total:
        await cb.answer("По фильтру заявок нет", show_alert=True)
        return
    await cb.answer()
    await send_export(bot, cb.message.chat.id, arch["filters"],
                      f"📤 Заявки по фильтру ({total} шт.)\nФильтры: {filters_text(arch['filters'])}")


def history_text(app_id: int) -> str:
    lines = []
    for h in get_history(app_id)[-12:]:
        kind = h["kind"]
        action = (STATUSES[h["status"]][1] if kind == "final" else HISTORY_LABELS.get(kind, h["status"]))
        lines.append(f"{fmt_ts(h['created_at'])} — {escape(h['by_name'] or '—')}: {action}")
    return ("\n\n🕘 <b>История:</b>\n" + "\n".join(lines)) if lines else ""


@staff.callback_query(F.data.startswith("ar:o:"), IsStaff())
async def on_archive_open_card(cb: CallbackQuery, state: FSMContext) -> None:
    app = get_application(int(cb.data.rsplit(":", 1)[1]))
    scope = scope_for(cb.from_user.id)
    if not app or (scope is not None and app["assigned_to"] != scope):
        await cb.answer("Заявка недоступна", show_alert=True)
        return
    await cb.answer()
    admin = is_admin(cb.from_user.id)
    text = lead_text(app, "admin" if admin else "mod", cb.from_user.id)
    if admin:
        text += history_text(app["id"])
    rows = []
    markup = lead_keyboard(app, "admin") if admin else None
    if markup:
        rows += markup.inline_keyboard
    rows.append([btn("⬅️ К списку", "ar:b")])
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@staff.callback_query(F.data == "noop", IsStaff())
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ---------- админ: напоминания ----------

def schedule_text() -> str:
    total, hours = 0, []
    for d in REMINDER_DELAYS_HOURS:
        total += d
        hours.append(str(total))
    return ", ".join(hours[:-1]) + f" и {hours[-1]} ч после запуска бота"


@staff.callback_query(F.data == "ap:rem", IsAdmin())
async def on_reminders_toggle(cb: CallbackQuery, bot: Bot) -> None:
    if reminders_enabled():
        set_setting("reminders", "0")
        await cb.answer("🔔 Напоминания выключены")
        await send_panel(bot, cb.message.chat.id, cb.from_user.id)
        return
    await cb.answer()
    people, due = pending_counts()
    start, end = REMINDER_WINDOW_MSK
    await cb.message.answer(
        "🔔 <b>Включить напоминания?</b>\n\n"
        "Бот напишет тем, кто запустил его, но не оставил заявку: "
        f"через {schedule_text()}. Отправка — только с {start}:00 до {end}:00 по Москве.\n\n"
        f"Сейчас серия положена <b>{people}</b> чел., из них <b>{due}</b> получат первое сообщение "
        "в ближайшие тихие часы. Каждое сообщение содержит кнопку «Не напоминать».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Включить", "ap:remon")], [btn("↩️ Отмена", "mp:menu")],
        ]),
    )


@staff.callback_query(F.data == "ap:remon", IsAdmin())
async def on_reminders_on(cb: CallbackQuery, bot: Bot) -> None:
    set_setting("reminders", "1")
    await cb.answer("🔔 Напоминания включены")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    await send_panel(bot, cb.message.chat.id, cb.from_user.id)


# ---------- админ: пользователи бота и журнал запусков ----------

USER_FILTER_TITLES = [
    ("all", "Все"), ("not_applied", "Не подали"), ("abandoned", "Бросили анкету"),
    ("applied", "Подали заявку"), ("optout", "Отписались"), ("blocked", "Заблокировали"),
]


def users_view(kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    staff = staff_ids()
    counts = users_counts(staff)
    total, page, rows = users_page(kind, page, staff)
    pages = max(1, -(-total // 8))
    title = dict(USER_FILTER_TITLES)[kind]
    lines = [
        "👤 <b>Пользователи бота</b>",
        "",
        f"Запустили: <b>{counts['all']}</b> · не подали: <b>{counts['not_applied']}</b> "
        f"(бросили анкету: {counts['abandoned']}) · подали: <b>{counts['applied']}</b>",
        f"Отписались от напоминаний: {counts['optout']} · заблокировали бота: {counts['blocked']}",
        "",
        f"<b>{title}</b>: {total}" + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""),
        "",
    ]
    names = source_names()
    for r in rows:
        icon = "✅" if r["completed_at"] else ("✍️" if r["started_form_at"] else "👋")
        who = escape(r["full_name"] or "—") + (f" (@{escape(r['username'])})" if r["username"] else "")
        extra = []
        if r["source"]:
            extra.append(escape(names.get(r["source"], r["source"])))
        if r["rem_count"]:
            extra.append(f"🔔{r['rem_count']}")
        if r["rem_off"]:
            extra.append("🔕")
        if r["blocked_at"]:
            extra.append("🚫")
        when = fmt_ts(r["last_start_at"] or r["started_at"])
        lines.append(f"{icon} {when} · {who}" + (" · " + " ".join(extra) if extra else ""))
    if not rows:
        lines.append("Пока никого нет.")
    keyboard = []
    buttons = [btn(("• " if k == kind else "") + t, f"us:{k}:0") for k, t in USER_FILTER_TITLES]
    keyboard += [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"us:{kind}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("➡️", f"us:{kind}:{page + 1}"))
        keyboard.append(nav)
    keyboard.append([btn("📤 Excel по фильтру", f"us:x:{kind}")])
    keyboard.append([btn("🏠 Меню", "mp:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@staff.callback_query(F.data == "ap:users", IsAdmin())
async def on_users_open(cb: CallbackQuery) -> None:
    await cb.answer()
    text, markup = users_view("all", 0)
    await cb.message.answer(text, reply_markup=markup)


@staff.callback_query(F.data.regexp(r"^us:(all|not_applied|abandoned|applied|optout|blocked):\d+$"), IsAdmin())
async def on_users_page(cb: CallbackQuery) -> None:
    _, kind, page = cb.data.split(":")
    await cb.answer()
    text, markup = users_view(kind, int(page))
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(text, reply_markup=markup)


@staff.callback_query(F.data.startswith("us:x:"), IsAdmin())
async def on_users_export(cb: CallbackQuery, bot: Bot) -> None:
    from datetime import datetime
    kind = cb.data.rsplit(":", 1)[1]
    if kind not in dict(USER_FILTER_TITLES):
        await cb.answer()
        return
    await cb.answer()
    buf = build_users_xlsx(kind, staff_ids())
    filename = f"users_{datetime.now(LOCAL_TZ):%Y-%m-%d_%H%M}.xlsx"
    await bot.send_document(
        cb.message.chat.id, BufferedInputFile(buf.read(), filename=filename),
        caption=f"👤 Пользователи бота — {dict(USER_FILTER_TITLES)[kind]}",
    )


# ---------- рекламные ссылки и рассылка (админ) ----------

async def links_view(bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    username = (await bot.me()).username
    total, rows = list_sources()
    lines = [
        "🔗 <b>Ссылки для рекламы</b>",
        "",
        "Каждая ссылка помечает людей, пришедших по ней: в заявках, статистике и Excel "
        "будет видно название источника. Нажмите на ссылку, чтобы скопировать.",
    ]
    if not rows:
        lines += ["", "Пока нет ни одной ссылки — нажмите «Создать ссылку»."]
    for code, name, started, form, done in rows:
        lines += [
            "",
            f"<b>{escape(name)}</b> — {started} → {form} → {done}",
            f"<code>https://t.me/{username}?start={code}</code>",
        ]
    if rows:
        lines += ["", "<i>Числа: запустили → начали заявку → подали.</i>"]
    if total > len(rows):
        lines.append(f"<i>Показаны последние {len(rows)} из {total}.</i>")
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("➕ Создать ссылку", "lk_new")]])
    return "\n".join(lines), markup


@staff.message(Command("links"))
async def on_links(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user.id not in ADMIN_USER_IDS:
        return
    await state.clear()
    text, markup = await links_view(bot)
    await message.answer(text, reply_markup=markup)


@staff.callback_query(F.data == "lk_list")
async def on_links_list(cb: CallbackQuery, bot: Bot) -> None:
    if cb.from_user.id not in ADMIN_USER_IDS:
        await cb.answer()
        return
    await cb.answer()
    text, markup = await links_view(bot)
    await cb.message.answer(text, reply_markup=markup)


@staff.callback_query(F.data == "lk_new")
async def on_link_new(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user.id not in ADMIN_USER_IDS:
        await cb.answer()
        return
    await cb.answer()
    await state.set_state(SourceForm.name)
    await cb.message.answer(
        "✍️ <b>Название источника</b>\n\n"
        "Напишите, как назвать эту ссылку, например: <code>залив 1</code>. "
        "Название видите только вы.\n\n/cancel — отмена"
    )


@staff.message(StateFilter(SourceForm.name), F.text, ~F.text.startswith("/"))
async def on_link_name(message: Message, state: FSMContext, bot: Bot) -> None:
    name = " ".join(message.text.split())
    if not 1 <= len(name) <= 50:
        await message.answer("Название должно быть от 1 до 50 символов. Попробуйте ещё раз.")
        return
    if source_name_taken(name):
        await message.answer("Источник с таким названием уже есть — придумайте другое.")
        return
    code = create_source(name, message.from_user.id)
    await state.clear()
    username = (await bot.me()).username
    await message.answer(
        f"✅ Ссылка для источника <b>{escape(name)}</b> создана:\n\n"
        f"<code>https://t.me/{username}?start={code}</code>\n\n"
        "Вставьте её в рекламу — всех, кто придёт по ней, бот пометит этим названием.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("➕ Создать ещё", "lk_new")],
            [btn("📋 Все ссылки", "lk_list")],
        ]),
    )


@staff.message(StateFilter(SourceForm.name))
async def on_link_name_other(message: Message) -> None:
    await message.answer("Отправьте название текстом, например: <code>залив 1</code>.")


@staff.message(Command("broadcast"))
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


@staff.message(StateFilter(Broadcast.waiting))
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


@staff.callback_query(StateFilter(Broadcast.confirm), F.data == "bc_cancel")
async def on_broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("🚫 Рассылка отменена.")


@staff.callback_query(StateFilter(Broadcast.confirm), F.data == "bc_send")
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


@staff.message(StateFilter(Broadcast.confirm))
async def on_broadcast_confirm_other(message: Message) -> None:
    await message.answer("Воспользуйтесь кнопками под сообщением выше — «Разослать» или «Отмена».")


@staff.callback_query(F.data == "ap:links", IsAdmin())
async def on_links_panel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    await state.clear()
    text, markup = await links_view(bot)
    await cb.message.answer(text, reply_markup=markup)


@staff.callback_query(F.data == "ap:bc", IsAdmin())
async def on_broadcast_panel(cb: CallbackQuery, state: FSMContext) -> None:
    recipients = get_broadcast_recipients()
    if not recipients:
        await cb.answer("Пока нет ни одного пользователя, который запускал бота", show_alert=True)
        return
    await cb.answer()
    await state.set_state(Broadcast.waiting)
    await cb.message.answer(
        f"📣 <b>Рассылка</b> ({len(recipients)} получателей)\n\n"
        "Отправьте сообщение, которое разослать — текст, фото, видео или документ. "
        "Оно уйдёт пользователям в точности так, как вы его отправите.\n\n"
        "/cancel — чтобы отменить."
    )


# ---------- запасные обработчики для персонала ----------

@staff.message(StateFilter(AddMod.waiting), IsAdmin())
async def on_mod_add_other(message: Message) -> None:
    await message.answer("Выберите человека кнопкой или отправьте числовой ID. «Отмена» — выйти.")


@staff.message(StateFilter(ArchiveSearch.query), IsStaff())
async def on_archive_query_other(message: Message) -> None:
    await message.answer("Напишите поисковый запрос текстом. /cancel — отмена.")


@staff.message(IsStaff(), StateFilter(default_state))
async def on_staff_anything(message: Message, bot: Bot) -> None:
    """Любое другое сообщение персонала — просто показываем панель."""
    await send_panel(bot, message.chat.id, message.from_user.id)
