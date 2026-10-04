"""Клиентская часть: приветствие, пошаговая анкета, защита от повторных заявок."""
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove, User

import config
from config import CHOICES, GENDERS, REQUIRED_STEPS
from db import (
    active_application, append_client_comment, had_started_before, log_start, save_application,
    set_reminders_off, touch_funnel,
)
from form import (
    BTN_BACK, BTN_SKIP, CONTACT_KEYS, GREETING, LAST_CONTACT_STEP, NORMALIZERS, PROMPTS,
    ERRORS, STEP_STATES, Form, active_steps, card, header, norm_phone, parse_source,
)
from notify import notify_moderators_queue, refresh_cards, send_lead_cards
from quick import TEXTS as QUICK_TEXTS, begin_quick, say
from reminders import OPTOUT_DONE
from ui import (
    apply_keyboard, confirm_keyboard, pending_notice, phone_keyboard, step_keyboard,
)

client = Router(name="client")
client.message.filter(F.chat.type == ChatType.PRIVATE)
client.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


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
    text = f"{header(step, data)}\n\n{PROMPTS[step]}"
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
    if step == "gender" and value != GENDERS[1]:
        await state.update_data(medical=None)  # вопрос только для женщин
    data = await state.get_data()
    if step == LAST_CONTACT_STEP:
        if not any(data.get(k) for k in CONTACT_KEYS):
            await bot.send_message(
                chat_id,
                "⚠️ Нужен хотя бы один способ связи — укажите телефон, Telegram, MAX или WhatsApp.",
            )
            await show_step(bot, chat_id, state, "phone", user)
            return
    steps = active_steps(data)
    i = steps.index(step)
    if i + 1 < len(steps):
        await show_step(bot, chat_id, state, steps[i + 1], user)
    else:
        await show_confirm(bot, chat_id, state)


async def go_back(bot: Bot, chat_id: int, state: FSMContext, user: User, step: str) -> None:
    steps = active_steps(await state.get_data())
    prev = steps[-1] if step == "confirm" else steps[max(steps.index(step) - 1, 0)]
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


# ---------- обработчики ----------

@client.message(CommandStart())
async def on_start(
    message: Message, state: FSMContext, bot: Bot, command: CommandObject
) -> None:
    source = parse_source(command.args)
    is_new = not had_started_before(message.from_user.id)
    touch_funnel(message.from_user, "started_at", source)
    log_start(message.from_user, source, is_new)  # в журнал, а не админу в личку
    pending = active_application(message.from_user.id)
    await clear_prev(bot, message.chat.id, await state.get_data())
    await state.clear()
    if pending:
        await message.answer(pending_notice(pending))
    elif config.FORM_VARIANT == "short":
        await begin_quick(bot, message.chat.id, state)  # сразу короткий диалог
    else:
        await message.answer(GREETING, reply_markup=apply_keyboard())


@client.message(Command("cancel"))
async def on_cancel_cmd(message: Message, state: FSMContext, bot: Bot) -> None:
    await clear_prev(bot, message.chat.id, await state.get_data())
    await state.clear()
    await message.answer("🚫 Заявка отменена.", reply_markup=apply_keyboard())


@client.callback_query(F.data == "apply")
async def on_apply(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    pending = active_application(cb.from_user.id)
    if pending:
        await cb.answer("Ваша заявка уже на рассмотрении", show_alert=True)
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(pending_notice(pending), reply_markup=None)
        return
    await cb.answer()
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    if config.FORM_VARIANT == "short":
        await begin_quick(bot, cb.message.chat.id, state)
        return
    touch_funnel(cb.from_user, "started_form_at")
    await start_form(bot, cb.message.chat.id, state, cb.from_user)


@client.callback_query(F.data == "cancel")
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    with suppress(TelegramBadRequest):
        await cb.message.edit_text("🚫 Заявка отменена.", reply_markup=apply_keyboard())


@client.callback_query(StateFilter(Form.confirm), F.data == "send")
async def on_send(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    await state.clear()
    pending = active_application(cb.from_user.id)
    if pending:
        await cb.answer("Ваша заявка уже на рассмотрении", show_alert=True)
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(pending_notice(pending), reply_markup=None)
        return
    app_id = save_application(cb.from_user, data)
    touch_funnel(cb.from_user, "completed_at")
    name = f", {escape(data['name'])}" if data.get("name") else ""
    await cb.answer("Заявка отправлена!")
    await cb.message.edit_text(
        f"🎉 <b>Заявка отправлена!</b>\n\nСпасибо{name}! Мы свяжемся с вами в ближайшее время.",
        reply_markup=None,
    )
    # Админам — карточка целиком, модераторам — только «поступила заявка» и число в очереди.
    await send_lead_cards(bot, app_id)
    await notify_moderators_queue(bot)


@client.callback_query(StateFilter(Form.confirm), F.data == "restart")
async def on_restart(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await guard(cb, state) is None:
        return
    await cb.answer()
    await start_form(bot, cb.message.chat.id, state, cb.from_user)


STEP_FILTER = StateFilter(*STEP_STATES.values(), Form.confirm)


@client.callback_query(STEP_FILTER, F.data == "back")
async def on_back(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    await cb.answer()
    await go_back(bot, cb.message.chat.id, state, cb.from_user, data["step"])


@client.callback_query(StateFilter(*STEP_STATES.values()), F.data == "skip")
async def on_skip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    if data["step"] in REQUIRED_STEPS:
        await cb.answer("Этот вопрос обязательный", show_alert=True)
        return
    await cb.answer()
    await save_and_next(bot, cb.message.chat.id, state, cb.from_user, data["step"], None)


@client.callback_query(StateFilter(*STEP_STATES.values()), F.data.startswith("use:"))
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


@client.callback_query(StateFilter(*STEP_STATES.values()), F.data.startswith("pick:"))
async def on_pick(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await guard(cb, state)
    if data is None:
        return
    options = CHOICES.get(data["step"])
    try:
        value = options[int(cb.data.split(":", 1)[1])]
    except (TypeError, ValueError, IndexError):
        await cb.answer("Эта кнопка уже неактуальна")
        return
    await cb.answer()
    await save_and_next(bot, cb.message.chat.id, state, cb.from_user, data["step"], value)


@client.message(StateFilter(Form.phone), F.contact)
async def on_contact(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = norm_phone(message.contact.phone_number) or message.contact.phone_number
    await save_and_next(bot, message.chat.id, state, message.from_user, "phone", phone)


@client.message(StateFilter(*STEP_STATES.values()), F.text, ~F.text.startswith("/"))
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


@client.callback_query(F.data == "rem:off")
async def on_reminders_off(cb: CallbackQuery) -> None:
    set_reminders_off(cb.from_user.id)
    await cb.answer("Хорошо, больше не напомним")
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(OPTOUT_DONE, reply_markup=None)


@client.callback_query()
async def on_stale_callback(cb: CallbackQuery) -> None:
    await cb.answer("Эта кнопка уже неактуальна. Нажмите /start")


@client.message(STEP_FILTER)
async def on_step_other(message: Message) -> None:
    await message.answer("Пожалуйста, ответьте текстом или воспользуйтесь кнопками под вопросом.")


@client.message()
async def on_anything(message: Message, bot: Bot) -> None:
    pending = active_application(message.from_user.id)
    if pending:
        text = (message.text or "").strip()
        if len(text) >= 3 and not text.startswith("/"):
            # Заявка уже подана: всё, что человек пишет дальше, — его дополнение к заявке.
            app_id = append_client_comment(message.from_user.id, text[:500])
            if app_id:
                await refresh_cards(bot, app_id)
                await say(bot, message.chat.id, QUICK_TEXTS["extra_ack"])
                return
        await message.answer(pending_notice(pending))
        return
    await message.answer(
        "Чтобы оставить заявку, нажмите кнопку ниже 👇", reply_markup=apply_keyboard()
    )
