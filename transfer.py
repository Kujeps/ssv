"""Передача заявки другому менеджеру.

Например, модератор провёл первичный опрос, а дальше с кандидатом лучше работать более опытному менеджеру.
Кнопка «🔀 Передать» — в карточке заявки, в «Моей таблице», в карточке админа и в теме группы.
Выбирается получатель (любой модератор, кроме текущего), затем — необязательный комментарий «что уже выяснили».
Получатель сразу получает карточку и комментарий; тема в группе остаётся та же, клиенту теперь пишет он.
"""
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

from config import ADMIN_USER_IDS, OPEN_STATUSES
from db import get_application, list_moderators, save_lead_message, transfer_lead
from notify import refresh_cards
from roles import IsStaff, is_admin
from ui import btn, lead_keyboard, lead_text

transfer = Router(name="transfer")
transfer.message.filter(F.chat.type == ChatType.PRIVATE)
transfer.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class TransferFlow(StatesGroup):
    note = State()


NOTE_PROMPT = (
    "🔀 Передаёте заявку #{id} → <b>{name}</b>.\n\n"
    "Напишите, что уже выяснили (опыт, пожелания, договорённости) — {name} увидит это в заявке. "
    "Или нажмите «Передать без комментария»."
)


def can_transfer(app: dict | None, user_id: int) -> bool:
    """Свою заявку в работе передаёт менеджер; любую взятую — админ."""
    return bool(app and app["status"] in OPEN_STATUSES and app["assigned_to"] is not None
                and (app["assigned_to"] == user_id or is_admin(user_id)))


def recipients(app: dict) -> list[dict]:
    return [m for m in list_moderators() if m["user_id"] != app["assigned_to"]]


def mod_name(m: dict) -> str:
    return m["name"] or (f"@{m['username']}" if m["username"] else str(m["user_id"]))


def pick_rows(app: dict, prefix: str) -> list[list]:
    return [[btn(f"👷 {mod_name(m)}"[:40], f"{prefix}{m['user_id']}")] for m in recipients(app)]


async def do_transfer(bot: Bot, app_id: int, actor: User, to_id: int, note: str | None) -> str | None:
    """Передаёт заявку и всех уведомляет. Возвращает имя получателя или None, если передать нельзя."""
    to = next((m for m in list_moderators() if m["user_id"] == to_id), None)
    if to is None:
        return None
    to_name = mod_name(to)
    res = transfer_lead(app_id, actor.id, actor.full_name, to_id, to_name, note, force=is_admin(actor.id))
    if res is None:
        return None
    await refresh_cards(bot, app_id)  # у прежнего менеджера карточка станет заглушкой, у админа — новый менеджер
    from relay import post_in_topic
    await post_in_topic(
        bot, app_id,
        f"🔀 <b>Заявка передана:</b> {escape(res['from_name'] or '—')} → <b>{escape(to_name)}</b>"
        + (f"\n💬 {escape(note)}" if note else "")
        + "\nТеперь клиенту пишет новый менеджер.",
    )
    app = get_application(app_id)
    head = f"🔀 <b>Вам передана заявка #{app_id}</b> от {escape(actor.full_name)}."
    if note:
        head += f"\n💬 <b>Что уже выяснили:</b> {escape(note)}"
    with suppress(Exception):
        await bot.send_message(to_id, head)
        msg = await bot.send_message(
            to_id, lead_text(app, "mod", to_id), reply_markup=lead_keyboard(app, "mod", to_id))
        save_lead_message(app_id, to_id, msg.message_id, "mod")
    if res["from_id"] not in (actor.id, to_id):  # передал админ — прежнему менеджеру сообщаем
        with suppress(Exception):
            await bot.send_message(
                res["from_id"], f"🔀 Заявку #{app_id} администратор передал менеджеру {escape(to_name)}.")
    for admin_id in ADMIN_USER_IDS:
        if admin_id not in (actor.id, to_id):
            with suppress(Exception):
                await bot.send_message(
                    admin_id, f"🔀 Заявка #{app_id}: {escape(actor.full_name)} → {escape(to_name)}"
                    + (f"\n💬 {escape(note)}" if note else ""))
    return to_name


DONE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [btn("📥 Взять следующую заявку", "mp:take")], [btn("🏠 Меню", "mp:menu")],
])


@transfer.callback_query(F.data.regexp(r"^tr:\d+$"), IsStaff())
async def on_transfer_open(cb: CallbackQuery, state: FSMContext) -> None:
    app = get_application(int(cb.data[3:]))
    if not can_transfer(app, cb.from_user.id):
        await cb.answer("Эта заявка не закреплена за вами", show_alert=True)
        return
    rows = pick_rows(app, f"tr:{app['id']}:")
    if not rows:
        await cb.answer("Передать некому: других модераторов нет. Админ добавляет их в «Модераторы».", show_alert=True)
        return
    await cb.answer()
    await state.set_state(None)
    rows.append([btn("↩️ Отмена", "tr:x")])
    await cb.message.answer(f"🔀 <b>Кому передать заявку #{app['id']}?</b>",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@transfer.callback_query(F.data.regexp(r"^tr:\d+:\d+$"), IsStaff())
async def on_transfer_pick(cb: CallbackQuery, state: FSMContext) -> None:
    _, app_id, to_id = cb.data.split(":")
    app = get_application(int(app_id))
    to = next((m for m in list_moderators() if m["user_id"] == int(to_id)), None)
    if not can_transfer(app, cb.from_user.id) or to is None:
        await cb.answer("Передать не получится — заявка или менеджер недоступны", show_alert=True)
        return
    await cb.answer()
    await state.set_state(TransferFlow.note)
    await state.update_data(app_id=int(app_id), to_id=int(to_id))
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(
            NOTE_PROMPT.format(id=app_id, name=escape(mod_name(to))),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("➡️ Передать без комментария", f"trg:{app_id}:{to_id}")], [btn("↩️ Отмена", "tr:x")],
            ]),
        )


async def finish(bot: Bot, chat_id: int, actor: User, app_id: int, to_id: int, note: str | None) -> None:
    name = await do_transfer(bot, app_id, actor, to_id, note)
    if name is None:
        await bot.send_message(chat_id, "Передать не получилось: заявка уже не у вас или менеджер недоступен.")
        return
    await bot.send_message(chat_id, f"✅ Заявка #{app_id} передана: <b>{escape(name)}</b>.", reply_markup=DONE_KEYBOARD)


@transfer.callback_query(F.data.regexp(r"^trg:\d+:\d+$"), IsStaff())
async def on_transfer_go(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, app_id, to_id = cb.data.split(":")
    await state.set_state(None)
    await cb.answer()
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    await finish(bot, cb.message.chat.id, cb.from_user, int(app_id), int(to_id), None)


@transfer.message(StateFilter(TransferFlow.note), F.text, ~F.text.startswith("/"), IsStaff())
async def on_transfer_note(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.set_state(None)
    await finish(bot, message.chat.id, message.from_user, data["app_id"], data["to_id"], message.text.strip()[:500])


@transfer.callback_query(F.data == "tr:x", IsStaff())
async def on_transfer_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await cb.answer("Отменено")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
