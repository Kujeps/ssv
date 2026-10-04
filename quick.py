"""Быстрая заявка: короткий диалог вместо анкеты.

1. Возраст, город и пол — одним сообщением («27, Иркутск, мужчина»); чего не хватает — бот переспросит.
2. Телефон — кнопкой «Поделиться номером» или текстом.
3. Готово. Имя и @username берутся из профиля Telegram. Дополнительную информацию о себе
   и пожелания клиент может дописать обычным сообщением в любой момент (см. client.on_anything).

Перед каждым сообщением бот на 1–2 секунды показывает «печатает…».
Тексты — в словаре TEXTS ниже, их можно менять без правки логики.
"""
import asyncio
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove, User,
)

import config
from config import GENDERS, MAX_AGE, MIN_AGE
from db import active_application, save_application, touch_funnel
from form import parse_intro
from notify import notify_moderators_queue, send_lead_cards
from ui import btn, pending_notice
from utils import norm_phone

TEXTS = {
    "hello": "Здравствуйте! Я помощник по приёму заявок на контрактную службу.",
    "ask_info": (
        "Подскажите, сколько вам лет, из какого вы города и какого вы пола? "
        "Можно одним сообщением, например: <code>27, Иркутск, мужчина</code>."
    ),
    "ask_age": "Сколько вам полных лет?",
    "ask_city": "Из какого вы города или региона?",
    "ask_gender": "И ваш пол?",
    "unparsed": "Не получилось разобрать. Напишите, пожалуйста, например: <code>27, Иркутск, мужчина</code>.",
    "age_range": (
        "Служба по контракту доступна от {min} до {max} лет. "
        "Проверьте, пожалуйста, возраст и напишите его числом."
    ),
    "ask_phone": (
        "Отлично, остался последний шаг — номер телефона. "
        "Он нужен только для связи: специалист позвонит и ответит на ваши вопросы.\n\n"
        "<i>Нажимая кнопку или отправляя номер, вы соглашаетесь на обработку персональных данных.</i>"
    ),
    "bad_phone": (
        "Не получилось распознать номер. Проверьте цифры и отправьте ещё раз, "
        "например: <code>+7 999 123-45-67</code>."
    ),
    "phone_other": "Нажмите кнопку «Поделиться номером» или напишите номер телефона сообщением.",
    "done": "Спасибо, заявка принята! Специалист свяжется с вами в ближайшее время.",
    "extra_invite": (
        "Пока ждёте звонок, можете рассказать о себе подробнее: опыт, образование, пожелания по службе. "
        "Просто напишите сообщением — я передам специалисту, и звонок пройдёт быстрее."
    ),
    "extra_ack": "Записал и передам специалисту. Спасибо!",
}

quick = Router(name="quick")
quick.message.filter(F.chat.type == ChatType.PRIVATE)
quick.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class Quick(StatesGroup):
    info = State()
    phone = State()


async def say(bot: Bot, chat_id: int, text: str, markup=None) -> Message:
    """Сообщение с «печатает…»: пауза зависит от длины текста, но не больше TYPING_MAX_SECONDS."""
    if config.TYPING_MAX_SECONDS > 0:
        with suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        pause = 0.4 + len(re.sub(r"<[^>]+>", "", text)) / 150
        await asyncio.sleep(min(config.TYPING_MAX_SECONDS, pause))
    return await bot.send_message(chat_id, text, reply_markup=markup)


def share_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="+7 999 123-45-67",
    )


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(g, f"qg:{i}") for i, g in enumerate(GENDERS)]])


async def begin_quick(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Quick.info)
    await state.update_data(q={})
    await say(bot, chat_id, TEXTS["hello"])
    await say(bot, chat_id, TEXTS["ask_info"])


async def ask_next(bot: Bot, chat_id: int, state: FSMContext, q: dict) -> None:
    """Спрашивает то, чего ещё не хватает; когда всё есть — просит телефон."""
    if not q.get("age"):
        await say(bot, chat_id, TEXTS["ask_age"])
    elif not q.get("city"):
        await say(bot, chat_id, TEXTS["ask_city"])
    elif not q.get("gender"):
        await say(bot, chat_id, TEXTS["ask_gender"], gender_keyboard())
    else:
        await state.set_state(Quick.phone)
        await say(bot, chat_id, TEXTS["ask_phone"], share_keyboard())


@quick.message(StateFilter(Quick.info), F.text, ~F.text.startswith("/"))
async def on_info(message: Message, state: FSMContext, bot: Bot) -> None:
    q = dict((await state.get_data()).get("q") or {})
    touch_funnel(message.from_user, "started_form_at")  # человек начал отвечать
    parsed = parse_intro(message.text)
    bad_age = parsed["age"] is not None and not MIN_AGE <= parsed["age"] <= MAX_AGE
    if bad_age:
        parsed["age"] = None
    for key in ("age", "city", "gender"):
        if parsed[key] is not None and not q.get(key):
            q[key] = parsed[key]
    await state.update_data(q=q)
    chat_id = message.chat.id
    if bad_age:
        await say(bot, chat_id, TEXTS["age_range"].format(min=MIN_AGE, max=MAX_AGE))
    elif not any(parsed.values()):
        await say(bot, chat_id, TEXTS["unparsed"])
    else:
        await ask_next(bot, chat_id, state, q)


@quick.callback_query(StateFilter(Quick.info), F.data.regexp(r"^qg:[01]$"))
async def on_gender(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    q = dict((await state.get_data()).get("q") or {})
    q["gender"] = GENDERS[int(cb.data.split(":")[1])]
    await state.update_data(q=q)
    with suppress(Exception):
        await cb.message.edit_reply_markup(reply_markup=None)
    await ask_next(bot, cb.message.chat.id, state, q)


@quick.message(StateFilter(Quick.info))
async def on_info_other(message: Message, bot: Bot) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler  # команды (/start, /cancel) обрабатывает клиентская часть
    await say(bot, message.chat.id, TEXTS["unparsed"])


async def finish_quick(message: Message, state: FSMContext, bot: Bot, phone: str) -> None:
    user: User = message.from_user
    q = (await state.get_data()).get("q") or {}
    await state.clear()
    pending = active_application(user.id)
    if pending:
        await message.answer(pending_notice(pending), reply_markup=ReplyKeyboardRemove())
        return
    app_id = save_application(user, {
        "name": user.full_name,
        "phone": phone,
        "tg": f"@{user.username}" if user.username else None,  # по нему можно написать или позвонить в Telegram
        "gender": q.get("gender"),
        "age": str(q["age"]) if q.get("age") else None,
        "city": q.get("city"),
    })
    touch_funnel(user, "completed_at")
    # Админам — карточка целиком, модераторам — только «поступила заявка» и число в очереди.
    await send_lead_cards(bot, app_id)
    await notify_moderators_queue(bot)
    await say(bot, message.chat.id, TEXTS["done"], ReplyKeyboardRemove())
    await say(bot, message.chat.id, TEXTS["extra_invite"])


@quick.message(StateFilter(Quick.phone), F.contact)
async def on_phone_contact(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = message.contact.phone_number
    phone = norm_phone(raw) or ("+" + re.sub(r"\D", "", raw))
    await finish_quick(message, state, bot, phone)


@quick.message(StateFilter(Quick.phone), F.text, ~F.text.startswith("/"))
async def on_phone_text(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = norm_phone(message.text)
    if phone is None:
        await say(bot, message.chat.id, TEXTS["bad_phone"])
        return
    await finish_quick(message, state, bot, phone)


@quick.message(StateFilter(Quick.phone))
async def on_phone_other(message: Message, bot: Bot) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler
    await say(bot, message.chat.id, TEXTS["phone_other"], share_keyboard())
