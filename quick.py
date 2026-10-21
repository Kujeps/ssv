"""Быстрая заявка: короткий диалог вместо анкеты.

1. Способ связи кнопкой: звонок, WhatsApp, Telegram или Max (для тех, у кого нет российского номера).
2. Контакт: номер кнопкой «Поделиться номером» или текстом (для Telegram — берётся @username).
   Как только контакт получен, заявка создаётся и уходит в очередь — даже если человек дальше не ответит.
3. Возраст (диапазон кнопкой или числом), город (текстом), пол (кнопкой). Ответы дописываются в заявку,
   карточки у админа и модератора обновляются.
4. Готово. Имя и @username берутся из профиля Telegram. Дополнительную информацию о себе
   клиент может дописать обычным сообщением в любой момент (см. client.on_anything).

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
from db import (
    active_application, append_client_comment, save_application, touch_funnel,
    update_client_details,
)
from form import parse_intro
from notify import notify_moderators_queue, refresh_cards, send_lead_cards
from relay import from_client as relay_from_client
from ui import btn, pending_notice
from utils import norm_phone

# Способы связи: ключ -> (текст кнопки, как записывается в заявку)
METHODS = {
    "call": ("📞 Позвонить", "Звонок"),
    "wa": ("🟢 WhatsApp", "WhatsApp"),
    "tg": ("✈️ Telegram", "Telegram"),
    "max": ("💬 Max", "Max"),
}
AGE_RANGES = ["18–24", "25–34", "35–44", "45–55", "56–65"]
CONSENT = "<i>Отправляя контакт, вы соглашаетесь на обработку персональных данных.</i>"

TEXTS = {
    "hello": (
        "Здравствуйте! Я помощник по приёму заявок на контрактную службу. "
        "Оставить заявку можно за минуту."
    ),
    "ask_method": "Как вам удобнее, чтобы с вами связались?",
    "pick_method": "Выберите, пожалуйста, способ связи кнопкой под вопросом.",
    "ask_call": "Отправьте номер телефона: нажмите кнопку или напишите его сообщением.\n\n" + CONSENT,
    "ask_wa": (
        "Напишите номер, на котором у вас есть WhatsApp. Можно с кодом любой страны, "
        "например <code>+380 50 123 45 67</code>. Если это номер вашего Telegram — нажмите кнопку.\n\n" + CONSENT
    ),
    "ask_max": (
        "Напишите номер, к которому привязан Max, или ваш ник в Max. "
        "Если это номер вашего Telegram — нажмите кнопку.\n\n" + CONSENT
    ),
    "ask_tg_phone": (
        "У вас скрыт @username, и написать вам в Telegram будет сложно. "
        "Оставьте, пожалуйста, номер телефона: нажмите кнопку или напишите его сообщением.\n\n" + CONSENT
    ),
    "bad_contact": (
        "Не получилось распознать контакт. Проверьте цифры и отправьте ещё раз, "
        "например: <code>+7 999 123-45-67</code>."
    ),
    "contact_other": "Нажмите кнопку «Поделиться номером» или напишите контакт сообщением.",
    "got_contact": "Спасибо, контакт получил. Осталось пара вопросов, чтобы специалист был готов к разговору.",
    "ask_age": "Сколько вам лет? Выберите вариант или напишите число.",
    "ask_city": "Из какого вы города или региона?",
    "ask_gender": "Ваш пол?",
    "unparsed": "Не получилось разобрать. Ответьте, пожалуйста, на вопрос выше или нажмите кнопку.",
    "age_range": (
        "Служба по контракту доступна от {min} до {max} лет. "
        "Проверьте, пожалуйста, возраст и напишите его числом."
    ),
    "done": "Спасибо, заявка принята! Специалист свяжется с вами в ближайшее время.",
    "extra_invite": (
        "Пока ждёте звонок, можете рассказать о себе подробнее: опыт, образование, пожелания по службе. "
        "Просто напишите сообщением — я передам специалисту, и звонок пройдёт быстрее."
    ),
    "extra_ack": "Записал и передам специалисту. Спасибо!",
    "skipped": "Хорошо, остальные вопросы пропускаем. Заявка принята — специалист свяжется с вами.",
}

quick = Router(name="quick")
quick.message.filter(F.chat.type == ChatType.PRIVATE)
quick.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class Quick(StatesGroup):
    method = State()    # выбор способа связи
    contact = State()   # ждём номер / контакт
    details = State()   # заявка уже создана, уточняем возраст, город, пол


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


def method_keyboard() -> InlineKeyboardMarkup:
    keys = list(METHODS)
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(METHODS[k][0], f"qm:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)
    ])


def age_keyboard() -> InlineKeyboardMarkup:
    rows = [[btn(r, f"qa:{i}") for i, r in enumerate(AGE_RANGES)][j:j + 3] for j in (0, 3)]
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def parse_phone(text: str) -> str | None:
    """Номер из текста. С «+» принимается любой международный номер (WhatsApp за рубежом)."""
    t = text.strip()
    digits = re.sub(r"\D", "", t)
    if t.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return norm_phone(t)


def parse_contact(method: str, text: str) -> str | None:
    phone = parse_phone(text)
    if phone or method != "max":
        return phone
    t = text.strip()
    return t if 3 <= len(t) <= 100 and re.search(r"[A-Za-zА-Яа-яЁё0-9]", t) else None


async def begin_quick(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Quick.method)
    await state.update_data(q={}, method=None, app_id=None)
    await say(bot, chat_id, TEXTS["hello"])
    await say(bot, chat_id, TEXTS["ask_method"], method_keyboard())


async def ask_contact(bot: Bot, chat_id: int, state: FSMContext, method: str, no_username: bool = False) -> None:
    await state.set_state(Quick.contact)
    await state.update_data(method=method)
    key = "ask_tg_phone" if no_username else f"ask_{method}"
    await say(bot, chat_id, TEXTS[key], share_keyboard())


@quick.callback_query(StateFilter(Quick.method), F.data.regexp(r"^qm:(call|wa|tg|max)$"))
async def on_method(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    method = cb.data.split(":")[1]
    touch_funnel(cb.from_user, "started_form_at")  # человек начал отвечать
    touch_funnel(cb.from_user, "contact_at")       # дошёл до контакта — для напоминаний
    with suppress(Exception):
        await cb.message.edit_reply_markup(reply_markup=None)
    chat_id = cb.message.chat.id
    if method == "tg":
        if cb.from_user.username:
            await create_lead(bot, chat_id, cb.from_user, state, "tg", f"@{cb.from_user.username}")
        else:
            await ask_contact(bot, chat_id, state, "call", no_username=True)
        return
    await ask_contact(bot, chat_id, state, method)


@quick.message(StateFilter(Quick.method), F.text, ~F.text.startswith("/"))
async def on_method_text(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = parse_phone(message.text)
    if phone:  # человек сразу прислал номер — считаем, что удобен звонок
        touch_funnel(message.from_user, "started_form_at")
        await create_lead(bot, message.chat.id, message.from_user, state, "call", phone)
    else:
        await say(bot, message.chat.id, TEXTS["pick_method"], method_keyboard())


@quick.message(StateFilter(Quick.method))
async def on_method_other(message: Message, bot: Bot) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler
    await say(bot, message.chat.id, TEXTS["pick_method"], method_keyboard())


async def create_lead(bot: Bot, chat_id: int, user: User, state: FSMContext, method: str, value: str) -> None:
    """Контакт получен: создаём заявку сразу, остальное клиент допишет в диалоге."""
    await state.clear()
    pending = active_application(user.id)
    if pending:
        await bot.send_message(chat_id, pending_notice(pending), reply_markup=ReplyKeyboardRemove())
        return
    column = {"call": "phone", "wa": "wa", "max": "max", "tg": "tg"}[method]
    data = {
        "name": user.full_name,
        "tg": f"@{user.username}" if user.username else None,  # по нему можно написать в Telegram
        "pref": METHODS[method][1],
    }
    data[column] = value
    app_id = save_application(user, data)
    touch_funnel(user, "completed_at")
    # Админам — карточка целиком, модераторам — только «поступила заявка» и число в очереди.
    await send_lead_cards(bot, app_id)
    await notify_moderators_queue(bot)
    await state.set_state(Quick.details)
    await state.update_data(q={}, app_id=app_id)
    await say(bot, chat_id, TEXTS["got_contact"], ReplyKeyboardRemove())
    await ask_next(bot, chat_id, state, {})


@quick.message(StateFilter(Quick.contact), F.contact)
async def on_contact_shared(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = message.contact.phone_number
    phone = parse_phone(raw) or ("+" + re.sub(r"\D", "", raw))
    method = (await state.get_data()).get("method") or "call"
    await create_lead(bot, message.chat.id, message.from_user, state, method, phone)


@quick.message(StateFilter(Quick.contact), F.text, ~F.text.startswith("/"))
async def on_contact_text(message: Message, state: FSMContext, bot: Bot) -> None:
    method = (await state.get_data()).get("method") or "call"
    value = parse_contact(method, message.text)
    if value is None:
        await say(bot, message.chat.id, TEXTS["bad_contact"])
        return
    await create_lead(bot, message.chat.id, message.from_user, state, method, value)


@quick.message(StateFilter(Quick.contact))
async def on_contact_other(message: Message, bot: Bot) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler
    await say(bot, message.chat.id, TEXTS["contact_other"], share_keyboard())


# ---------- уточнения: заявка уже создана ----------

async def ask_next(bot: Bot, chat_id: int, state: FSMContext, q: dict) -> None:
    """Спрашивает то, чего ещё не хватает; когда всё есть — завершает диалог."""
    if not (q.get("age") or q.get("age_range")):
        await say(bot, chat_id, TEXTS["ask_age"], age_keyboard())
    elif not q.get("city"):
        await say(bot, chat_id, TEXTS["ask_city"])
    elif not q.get("gender"):
        await say(bot, chat_id, TEXTS["ask_gender"], gender_keyboard())
    else:
        await state.clear()
        await say(bot, chat_id, TEXTS["done"])
        await say(bot, chat_id, TEXTS["extra_invite"])


async def save_details(bot: Bot, state: FSMContext, user_id: int, fields: dict) -> dict:
    data = await state.get_data()
    q = {**(data.get("q") or {}), **fields}
    await state.update_data(q=q)
    if data.get("app_id") and update_client_details(data["app_id"], user_id, fields):
        await refresh_cards(bot, data["app_id"])
    return q


async def drop_markup(cb: CallbackQuery) -> None:
    with suppress(Exception):
        await cb.message.edit_reply_markup(reply_markup=None)


@quick.callback_query(StateFilter(Quick.details), F.data.regexp(r"^qa:[0-4]$"))
async def on_age_range(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    await drop_markup(cb)
    q = await save_details(bot, state, cb.from_user.id, {"age_range": AGE_RANGES[int(cb.data[3:])]})
    await ask_next(bot, cb.message.chat.id, state, q)


@quick.callback_query(StateFilter(Quick.details), F.data.regexp(r"^qg:[01]$"))
async def on_gender(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    await drop_markup(cb)
    q = await save_details(bot, state, cb.from_user.id, {"gender": GENDERS[int(cb.data.split(":")[1])]})
    await ask_next(bot, cb.message.chat.id, state, q)


@quick.message(StateFilter(Quick.details), F.text, ~F.text.startswith("/"))
async def on_details_text(message: Message, state: FSMContext, bot: Bot) -> None:
    q = dict((await state.get_data()).get("q") or {})
    parsed = parse_intro(message.text)
    new: dict = {}
    bad_age = False
    if parsed["age"] is not None and not (q.get("age") or q.get("age_range")):
        if MIN_AGE <= parsed["age"] <= MAX_AGE:
            new["age"] = parsed["age"]
        else:
            bad_age = True
    asking_city = bool(q.get("age") or q.get("age_range")) and not q.get("city")
    if parsed["gender"] is not None and not q.get("gender"):
        new["gender"] = parsed["gender"]
    # Город принимаем только коротким ответом: длинная фраза о себе — это не название города.
    short = len(parsed["city"] or "") <= 40 and len((parsed["city"] or "").split()) <= 4
    if parsed["city"] is not None and not q.get("city") and short:
        if asking_city or parsed["age"] is not None or parsed["gender"] is not None:
            new["city"] = parsed["city"]
    chat_id = message.chat.id
    if new:
        q = await save_details(bot, state, message.from_user.id, new)
        await ask_next(bot, chat_id, state, q)
    elif bad_age:
        await say(bot, chat_id, TEXTS["age_range"].format(min=MIN_AGE, max=MAX_AGE))
    else:
        if await relay_from_client(bot, message):  # менеджер уже переписывается с клиентом
            return
        # Не ответ на вопрос — возможно, человек пишет о себе: сохраняем в заявку и повторяем вопрос.
        text = message.text.strip()
        if len(text) >= 6:
            app_id = append_client_comment(message.from_user.id, text[:500])
            if app_id:
                await refresh_cards(bot, app_id)
        await say(bot, chat_id, TEXTS["unparsed"])
        await ask_next(bot, chat_id, state, q)


@quick.message(StateFilter(Quick.details))
async def on_details_other(message: Message, bot: Bot) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler  # команды (/start, /cancel) обрабатывает клиентская часть
    if await relay_from_client(bot, message):
        return
    await say(bot, message.chat.id, TEXTS["unparsed"])
