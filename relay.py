"""Переписка менеджера с кандидатом через бота.

Одна общая группа с включёнными темами (форум), бот — её администратор.
- Менеджер берёт заявку → бот создаёт тему «#N Имя · Город» с карточкой клиента и кнопками статусов.
- Всё, что менеджер пишет в теме, бот отправляет клиенту: «Менеджер Дмитрий: …» (имя — команда /name).
  Сообщение, начинающееся с «#», — заметка для себя: клиенту не уходит, сохраняется в заявке.
- Ответы клиента бот пишет в тему: «👤 Имя: …». Вложения (фото, видео, голосовые, файлы, стикеры,
  геопозиции, контакты) пересылаются в обе стороны; всё записывается в журнал relay_log.
- Вне рабочего времени (WORK_HOURS, по Москве) клиент получает автоответ.
- Заявка вернулась в очередь или закрыта → тема закрывается; клиент написал снова → тема открывается.
Если группа не подключена, бот работает по-старому. Подключение: /relay_here в группе (админ).
"""
import logging
import time
from contextlib import suppress
from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReactionTypeEmoji

import config
from config import AUTO_REPLY_EVERY_HOURS, CALLBACK_OPTIONS, MSK, NOCALL_ALERT_ATTEMPTS, OPEN_STATUSES
from config import REASON_REQUIRED, STATUSES, WORK_HOURS_MSK
from db import (
    add_moderator, add_note, callback_time, change_status, claim_lead, is_moderator, finish_lead, get_application, latest_application, manager_display_name,
    get_setting, mark_blocked, open_without_topic, postpone_lead, taken_without_topic, release_lead, relay_chat_id, relay_log_add,
    relay_log_by_group_msg, relay_log_pending, relay_log_set, relay_topic, relay_topic_by_thread,
    relay_topic_delete, relay_topic_save, relay_topic_update, set_setting,
)
from roles import IsStaff as IsStaffFilter, is_admin, is_staff
from ui import btn, lead_text
from utils import parse_utc, utc_now, utc_str

CAPTION_KINDS = {"photo", "video", "document", "audio", "voice", "animation"}
RELAYABLE = CAPTION_KINDS | {"text", "video_note", "sticker", "location", "venue", "contact", "poll", "dice"}
KIND_TITLES = {
    "photo": "фото", "video": "видео", "document": "файл", "audio": "аудио", "voice": "голосовое",
    "animation": "анимация", "video_note": "кружок", "sticker": "стикер", "location": "геопозиция",
    "venue": "место", "contact": "контакт", "poll": "опрос", "dice": "кубик",
}

AUTO_TEXT = (
    "Сообщение получил и передал менеджеру. Сейчас нерабочее время — ответим утром, "
    "после {start}:00 по Москве."
)
REOPEN_ACK = "Сообщение получил и передал менеджеру."

# Ожидание причины/комментария от менеджера в теме: (чат, тема, пользователь) -> (действие, номер заявки)
PENDING: dict[tuple[int, int, int], tuple[str, int]] = {}
_last_warn = 0.0
_flood_until = 0.0  # Telegram ограничил создание тем — до этого момента не пытаемся


# ---------- вспомогательное ----------

def file_id_of(m: Message) -> str | None:
    kind = m.content_type.value
    if kind == "photo" and m.photo:
        return m.photo[-1].file_id
    obj = getattr(m, kind, None)
    return getattr(obj, "file_id", None)


def is_work_time(now=None) -> bool:
    hour = (now or utc_now()).astimezone(MSK).hour
    return WORK_HOURS_MSK[0] <= hour < WORK_HOURS_MSK[1]


def topic_title(app: dict) -> str:
    title = f"#{app['id']} {app['name'] or 'Без имени'}"
    if app.get("city"):
        title += f" · {app['city']}"
    return title[:120]


def manager_label(app: dict) -> str:
    if not app.get("assigned_to"):
        return "Менеджер"
    return "Менеджер " + manager_display_name(app["assigned_to"], app.get("assigned_name"))


def card_text(app: dict) -> str:
    text = lead_text(app, "admin")
    if app["status"] in OPEN_STATUSES:
        text += (
            f"\n\n💬 Всё, что вы пишете в этой теме, уходит клиенту как «<b>{escape(manager_label(app))}</b>». "
            "Заметка для себя — сообщение, начинающееся с <code>#</code>. Имя меняется в боте: «✏️ Имя для клиентов»."
        )
    return text[:4000]


def card_keyboard(app: dict) -> InlineKeyboardMarkup | None:
    if app["status"] not in OPEN_STATUSES:
        return None
    i = app["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Согласился", f"rt:{i}:agreed"), btn("❌ Отказался", f"rt:{i}:refused")],
        [btn("⏳ В работе", f"rt:{i}:reached"), btn("📵 Не дозвонился", f"rt:{i}:nocall")],
        [btn("🗑 Мусор", f"rt:{i}:junk"), btn("↩️ Вернуть в очередь", f"rt:{i}:release")],
        [btn("🔀 Передать другому менеджеру", f"rt:{i}:tr")],
    ])


async def deliver(bot: Bot, to_chat: int, from_chat: int, msg_id: int, kind: str, caption: str,
                  label: str, thread: int | None = None) -> int:
    """Копирует не-текстовое сообщение с подписью-меткой («Менеджер Дмитрий:» / «👤 Имя:»).
    Возвращает id нового сообщения."""
    extra = {"message_thread_id": thread} if thread else {}
    if kind in CAPTION_KINDS:
        full = label + (("\n" + escape(caption)) if caption else "")
        if len(full) <= 1000:
            return (await bot.copy_message(to_chat, from_chat, msg_id, caption=full, **extra)).message_id
        await bot.send_message(to_chat, full[:4000], **extra)  # длинная подпись — отдельным сообщением
        return (await bot.copy_message(to_chat, from_chat, msg_id, caption="", **extra)).message_id
    # стикер, кружок, геопозиция и т. п. подписи не имеют — метка отдельным сообщением
    await bot.send_message(to_chat, f"{label} <i>({KIND_TITLES.get(kind, kind)})</i>", **extra)
    return (await bot.copy_message(to_chat, from_chat, msg_id, **extra)).message_id


async def warn_admins(bot: Bot, text: str) -> None:
    """Предупреждение админам о проблеме с группой — не чаще раза в час."""
    global _last_warn
    if time.time() - _last_warn < 3600:
        return
    _last_warn = time.time()
    from notify import notify_admins
    await notify_admins(bot, text)


# ---------- темы: создание, обновление, закрытие ----------

async def sync_topic(bot: Bot, app_id: int) -> None:
    """Приводит тему в соответствие с заявкой. Вызывается после любого изменения заявки; ошибок не бросает."""
    chat = relay_chat_id()
    if not chat:
        return
    try:
        await _sync(bot, chat, app_id)
    except TelegramRetryAfter as e:
        # Лимит Telegram на создание тем: не ждём (бот не должен вставать), повторим позже сами.
        global _flood_until
        _flood_until = time.time() + e.retry_after
        logging.warning("Telegram просит подождать %s с перед созданием темы заявки #%s", e.retry_after, app_id)
    except Exception as e:
        logging.exception("Тема заявки #%s: не удалось обновить", app_id)
        await warn_admins(
            bot, "⚠️ Не получилось создать или обновить тему в группе переписки. Проверьте, что в группе включены "
                 "темы, а бот — администратор с правом «Управление темами».\n\n" f"<code>{escape(str(e))[:300]}</code>")


async def _sync(bot: Bot, chat: int, app_id: int) -> None:
    app = get_application(app_id)
    if not app:
        return
    topic = relay_topic(app_id)
    if topic and topic["chat_id"] != chat:
        topic = None  # группу сменили — тема в новой группе создаётся заново
    active = app["status"] in OPEN_STATUSES and app["assigned_to"] is not None
    if active:
        if topic is None:
            await _create_topic(bot, chat, app)
            return
        if topic["closed"]:
            with suppress(TelegramBadRequest):
                await bot.reopen_forum_topic(chat, topic["thread_id"])
            relay_topic_update(app_id, closed=0)
            await _say(bot, topic, f"👷 Заявку взял: <b>{escape(app['assigned_name'] or '—')}</b>")
            await _replay_pending(bot, chat, topic["thread_id"], app)  # что клиент прислал, пока заявка была в очереди
    await _update_card(bot, topic, app)
    if topic and not active and not topic["closed"]:
        icon, label = STATUSES.get(app["status"], STATUSES["new"])
        if app["status"] == "new":
            line = "↩️ Заявка вернулась в очередь — тема закрыта."
        else:
            line = f"{icon} Заявка закрыта: <b>{label}</b> — тема закрыта. Клиент напишет — откроется снова."
        await _say(bot, topic, line)
        with suppress(TelegramBadRequest):
            await bot.close_forum_topic(chat, topic["thread_id"])
        relay_topic_update(app_id, closed=1)


async def post_in_topic(bot: Bot, app_id: int, text: str) -> None:
    """Служебная запись в тему заявки (если тема есть)."""
    topic = relay_topic(app_id)
    if topic and relay_chat_id() == topic["chat_id"]:
        await _say(bot, topic, text)


async def _say(bot: Bot, topic: dict, text: str) -> None:
    with suppress(TelegramBadRequest):
        await bot.send_message(topic["chat_id"], text, message_thread_id=topic["thread_id"])


async def _update_card(bot: Bot, topic: dict | None, app: dict) -> None:
    if not topic or not topic.get("card_msg_id"):
        return
    with suppress(TelegramBadRequest):
        await bot.edit_message_text(
            card_text(app), chat_id=topic["chat_id"], message_id=topic["card_msg_id"],
            reply_markup=card_keyboard(app),
        )


async def _create_topic(bot: Bot, chat: int, app: dict) -> None:
    if time.time() < _flood_until:
        return  # подождём окончания лимита; тему создаст следующая проверка
    created = await bot.create_forum_topic(chat, topic_title(app))
    thread = created.message_thread_id
    card = await bot.send_message(chat, card_text(app), reply_markup=card_keyboard(app), message_thread_id=thread)
    relay_topic_save(app["id"], chat, thread, card.message_id)
    with suppress(TelegramBadRequest):
        await bot.pin_chat_message(chat, card.message_id, disable_notification=True)
    await _replay_pending(bot, chat, thread, app)


async def _replay_pending(bot: Bot, chat: int, thread: int, app: dict) -> None:
    """Вложения, которые клиент прислал, пока заявка ждала в очереди, — в новую тему."""
    pending = relay_log_pending(app["id"])
    media = [p for p in pending if p["kind"] != "text"]
    if media:
        await bot.send_message(chat, "📎 <b>Клиент прислал до того, как заявку взяли:</b>", message_thread_id=thread)
    for row in pending:
        if row["kind"] == "text":
            relay_log_set(row["id"], group_msg_id=0)  # текст уже в карточке («Клиент пишет»)
            continue
        try:
            gid = await deliver(bot, chat, app["user_id"], row["client_msg_id"], row["kind"], row["text"] or "",
                                f"👤 <b>{escape(app['name'] or 'Клиент')}:</b>", thread)
            relay_log_set(row["id"], group_msg_id=gid)
        except Exception:
            logging.exception("Не удалось перенести вложение клиента %s в тему", row["id"])


# ---------- клиент -> тема ----------

async def from_client(bot: Bot, message: Message) -> bool:
    """Сообщение клиента. True — передано менеджеру в тему (дальше обрабатывать не нужно)."""
    chat = relay_chat_id()
    if not chat or message.from_user is None:
        return False
    kind = message.content_type.value
    text = message.text or message.caption or ""
    if kind not in RELAYABLE or (kind == "text" and text.startswith("/")):
        return False
    user = message.from_user
    app = latest_application(user.id)
    if not app or app["status"] == "junk":
        return False
    if app["status"] == "new":  # ещё в очереди: запоминаем, менеджер увидит, когда заявку возьмут
        relay_log_add(app["id"], "in", kind, text, file_id_of(message), None, message.message_id)
        return False
    topic = relay_topic(app["id"])
    if topic is None and app["status"] in OPEN_STATUSES:
        await sync_topic(bot, app["id"])  # тема создаётся, когда клиент пишет по заявке, взятой до подключения группы
        topic = relay_topic(app["id"])
    if topic is None or topic["chat_id"] != chat:
        relay_log_add(app["id"], "in", kind, text, file_id_of(message), None, message.message_id)
        return False
    reopened = False
    if topic["closed"]:
        if app["status"] not in ("agreed", "refused"):
            return False
        with suppress(TelegramBadRequest):
            await bot.reopen_forum_topic(chat, topic["thread_id"])
        relay_topic_update(app["id"], closed=0)
        reopened = True
    log_id = relay_log_add(app["id"], "in", kind, text, file_id_of(message), None, message.message_id)
    label = f"👤 <b>{escape(user.full_name)}:</b>"
    try:
        if reopened:
            await _say(bot, topic, "💬 Клиент написал после закрытия заявки.")
        if kind == "text":
            sent = await bot.send_message(
                chat, f"{label}\n{escape(text[:3900])}", message_thread_id=topic["thread_id"])
            gid = sent.message_id
        else:
            gid = await deliver(bot, chat, message.chat.id, message.message_id, kind, text, label,
                                topic["thread_id"])
    except TelegramBadRequest as e:
        logging.exception("Не удалось передать сообщение клиента в тему")
        if "thread" in str(e).lower():  # тему удалили вручную — создадим заново при следующем событии
            relay_topic_delete(app["id"])
        return False
    except Exception:
        logging.exception("Не удалось передать сообщение клиента в тему")
        return False
    relay_log_set(log_id, group_msg_id=gid)
    if reopened:
        with suppress(Exception):
            await bot.send_message(user.id, REOPEN_ACK)
    elif not is_work_time() and _auto_reply_due(topic):
        relay_topic_update(app["id"], auto_at=utc_str())
        with suppress(Exception):
            await bot.send_message(user.id, AUTO_TEXT.format(start=WORK_HOURS_MSK[0]))
    return True


async def retry_topics(bot: Bot) -> None:
    """Фоновая попытка создать темы для только что взятых заявок (если раньше мешал лимит Telegram)."""
    if not relay_chat_id() or time.time() < _flood_until:
        return
    ids = taken_without_topic(3)
    backfill = get_setting("relay_backfill") == "1"
    if backfill:
        ids += [i for i in open_without_topic(3) if i not in ids][: max(0, 3 - len(ids))]
    for app_id in ids:
        await sync_topic(bot, app_id)
        if time.time() < _flood_until:
            return
    if backfill and not open_without_topic(1):
        set_setting("relay_backfill", "0")
        from notify import notify_admins
        await notify_admins(bot, "✅ Темы для всех открытых заявок созданы.")


def _auto_reply_due(topic: dict) -> bool:
    last = parse_utc(topic.get("auto_at"))
    return last is None or utc_now() - last >= timedelta(hours=AUTO_REPLY_EVERY_HOURS)


# ---------- менеджер -> клиент ----------

class InRelay(BaseFilter):
    async def __call__(self, event) -> bool:
        chat = relay_chat_id()
        msg = event.message if isinstance(event, CallbackQuery) else event
        return bool(chat and msg and msg.chat.id == chat)


relay = Router(name="relay")
relay.message.filter(InRelay())
relay.edited_message.filter(InRelay())
relay.callback_query.filter(InRelay())

setup = Router(name="relay_setup")


async def react(bot: Bot, message: Message, emoji: str = "👍") -> None:
    with suppress(Exception):
        await bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji=emoji)])


@relay.message(F.message_thread_id)
async def on_manager_message(message: Message, bot: Bot) -> None:
    topic = relay_topic_by_thread(message.chat.id, message.message_thread_id)
    user = message.from_user
    if not topic or user is None or user.is_bot or not is_staff(user.id):
        return
    kind = message.content_type.value
    if kind not in RELAYABLE:
        return
    text = message.text or ""
    if text.startswith("/"):
        return
    app = get_application(topic["app_id"])
    if not app:
        return
    key = (message.chat.id, message.message_thread_id, user.id)
    if key in PENDING and kind == "text":
        await finish_pending(bot, message, key, app)
        return
    caption = message.caption or ""
    if (text or caption).startswith("#"):  # заметка для себя — клиенту не уходит
        note = (text or caption).lstrip("#").strip()
        if note:
            add_note(app["id"], user.id, user.full_name, note[:500])
            relay_log_add(app["id"], "note", kind, note, file_id_of(message), message.message_id, None, user.full_name)
            from notify import refresh_cards
            await refresh_cards(bot, app["id"])
            await react(bot, message, "✍")
        return
    if not (app["assigned_to"] == user.id and app["status"] in OPEN_STATUSES):
        app = await claim_by_writing(bot, message, app)
        if app is None:
            return
    name = manager_display_name(user.id, user.full_name)
    label = f"<b>Менеджер {escape(name)}:</b>"
    log_id = relay_log_add(app["id"], "out", kind, text or caption, file_id_of(message), message.message_id,
                           None, name)
    try:
        if kind == "text":
            sent = await bot.send_message(app["user_id"], f"{label}\n{escape(text[:3900])}")
            cid = sent.message_id
        else:
            cid = await deliver(bot, app["user_id"], message.chat.id, message.message_id, kind, caption, label)
    except TelegramForbiddenError:
        mark_blocked(app["user_id"])
        await message.reply("⚠️ <b>Не доставлено:</b> клиент заблокировал бота. Свяжитесь с ним по телефону или другим способом.")
        return
    except TelegramBadRequest as e:
        await message.reply(f"⚠️ <b>Не доставлено:</b> {escape(str(e))[:200]}")
        return
    relay_log_set(log_id, client_msg_id=cid)
    await react(bot, message)


@relay.edited_message(F.message_thread_id)
async def on_manager_edit(message: Message, bot: Bot) -> None:
    topic = relay_topic_by_thread(message.chat.id, message.message_thread_id)
    user = message.from_user
    if not topic or user is None or not is_staff(user.id):
        return
    app = get_application(topic["app_id"])
    row = relay_log_by_group_msg(topic["app_id"], message.message_id)
    if not app or not row or row["direction"] != "out" or not row["client_msg_id"]:
        return
    label = f"<b>Менеджер {escape(row['by_name'] or manager_display_name(user.id))}:</b>"
    with suppress(TelegramBadRequest):
        if message.text:
            await bot.edit_message_text(f"{label}\n{escape(message.text[:3900])}", chat_id=app["user_id"],
                                        message_id=row["client_msg_id"])
        elif message.caption and row["kind"] in CAPTION_KINDS:
            await bot.edit_message_caption(chat_id=app["user_id"], message_id=row["client_msg_id"],
                                           caption=f"{label}\n{escape(message.caption)}"[:1000])


async def claim_by_writing(bot: Bot, message: Message, app: dict) -> dict | None:
    """Написал в теме чужой/свободной/закрытой заявки — заявка становится твоей («В работе»).
    Модератор забирает заявку из очереди или уже обработанную; заявку в работе у другого — только админ."""
    user = message.from_user
    if is_admin(user.id) and not is_moderator(user.id):
        add_moderator(user.id, user.full_name, user.username, user.id)  # админ начал вести заявки сам
    res = claim_lead(app["id"], user.id, user.full_name, take_over=is_admin(user.id))
    if res is None:
        await message.reply("Эта заявка в работе у другого менеджера — клиенту не отправлено. "
                            "Попросите его передать заявку вам (кнопка «🔀 Передать»).")
        return None
    from notify import notify_admins, refresh_cards
    await refresh_cards(bot, app["id"])  # откроет тему, если была закрыта, и обновит карточки
    if res["prev_status"] in OPEN_STATUSES and res["prev_holder"]:
        await post_in_topic(bot, app["id"], f"🔀 Заявку забрал себе: <b>{escape(user.full_name)}</b>")
        with suppress(Exception):
            await bot.send_message(res["prev_holder"], f"🔀 Заявку #{app['id']} забрал себе {escape(user.full_name)}.")
    elif res["prev_status"] != "new":
        await post_in_topic(bot, app["id"], f"♻️ Повторная обработка: заявку взял <b>{escape(user.full_name)}</b>")
    if not is_admin(user.id):
        await notify_admins(bot, f"👷 {escape(user.full_name)} взял заявку #{app['id']}, написав в теме.")
    return get_application(app["id"])


# ---------- кнопки статусов в теме ----------

ACTION_PROMPTS = {
    "refused": "❌ <b>Причина отказа.</b> Напишите её следующим сообщением в этой теме (обязательно).",
    "junk": "🗑 <b>Почему это мусор?</b> Напишите причину следующим сообщением в этой теме (обязательно).",
    "release": "↩️ <b>Причина возврата в очередь.</b> Напишите её следующим сообщением в этой теме (обязательно).",
}


def set_final(app: dict, user, status: str, note: str | None = None) -> bool:
    """Итоговый статус: из «Взята» — обычным закрытием, из «В работе» и «Перезвонить» — как правка в таблице."""
    if app["status"] == "work":
        return finish_lead(app["id"], user.id, user.full_name, status, note)
    return change_status(app["id"], user.id, user.full_name, status, note) is not None


def set_nocall(app: dict, user, option: str) -> int | None:
    """Недозвон с отложенным перезвоном. Возвращает число попыток или None."""
    if app["status"] == "work":
        return postpone_lead(app["id"], user.id, user.full_name, option)
    res = change_status(app["id"], user.id, user.full_name, "nocall",
                        callback_dt=callback_time(option, app["tz_offset"]))
    return res["attempts"] if res else None


def owned(app: dict | None, user_id: int) -> bool:
    return bool(app and app["assigned_to"] == user_id and app["status"] in OPEN_STATUSES)


@relay.callback_query(F.data.startswith("rt:"))
async def on_topic_button(cb: CallbackQuery, bot: Bot) -> None:
    parts = cb.data.split(":")
    app_id, act, user = int(parts[1]), parts[2], cb.from_user
    if not is_staff(user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    app = get_application(app_id)
    from transfer import can_transfer
    allowed = can_transfer(app, user.id) if act in ("tr", "to", "tg") else owned(app, user.id)
    if not allowed:
        await cb.answer("Эта заявка не закреплена за вами", show_alert=True)
        return
    topic = relay_topic(app_id)
    thread = topic["thread_id"] if topic else cb.message.message_thread_id
    key = (cb.message.chat.id, thread, user.id)
    from notify import notify_moderators_queue, notify_admins, refresh_cards
    if act == "agreed":
        ok = set_final(app, user, "agreed")
        await cb.answer("✅ Согласился" if ok else "Не получилось", show_alert=not ok)
        await refresh_cards(bot, app_id)
    elif act == "reached":
        ok = change_status(app_id, user.id, user.full_name, "reached") is not None
        await cb.answer("⏳ В работе" if ok else "Не получилось", show_alert=not ok)
        await refresh_cards(bot, app_id)
    elif act == "nocall":
        await cb.answer()
        rows = [[btn(label, f"rt:{app_id}:c:{k}")] for k, label in CALLBACK_OPTIONS.items()]
        rows.append([btn("⬅️ Назад", f"rt:{app_id}:back")])
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif act == "back":
        await cb.answer()
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=card_keyboard(app))
    elif act == "c":
        attempts = set_nocall(app, user, parts[3])
        if attempts is None:
            await cb.answer("Не получилось", show_alert=True)
            return
        await cb.answer("📵 Отложено")
        await refresh_cards(bot, app_id)
        when = get_application(app_id)["callback_at"]
        from utils import fmt_ts
        await bot.send_message(cb.message.chat.id, f"📵 Перезвонить: <b>{fmt_ts(when)} МСК</b>.", message_thread_id=thread)
        if attempts >= NOCALL_ALERT_ATTEMPTS:
            await notify_admins(bot, f"⚠️ По заявке #{app_id} уже <b>{attempts}</b> недозвона(ов) ({escape(user.full_name)}).")
    elif act in ACTION_PROMPTS:
        await cb.answer()
        PENDING[key] = (act, app_id)
        await bot.send_message(
            cb.message.chat.id, ACTION_PROMPTS[act], message_thread_id=thread,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("↩️ Отмена", f"rt:{app_id}:x")]]),
        )
    elif act == "tr":  # кому передать
        from transfer import pick_rows
        rows = pick_rows(app, f"rt:{app_id}:to:")
        if not rows:
            await cb.answer("Передать некому: других модераторов нет.", show_alert=True)
            return
        await cb.answer()
        rows.append([btn("⬅️ Назад", f"rt:{app_id}:back")])
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif act == "to":  # получатель выбран — ждём комментарий «что уже выяснили»
        from transfer import NOTE_PROMPT, list_moderators, mod_name
        to = next((m for m in list_moderators() if m["user_id"] == int(parts[3])), None)
        if to is None:
            await cb.answer("Этого менеджера уже нет", show_alert=True)
            return
        await cb.answer()
        PENDING[key] = (f"transfer:{to['user_id']}", app_id)
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=card_keyboard(app))
        await bot.send_message(
            cb.message.chat.id,
            NOTE_PROMPT.format(id=app_id, name=escape(mod_name(to))).replace("Напишите,", "Напишите следующим сообщением в теме,"),
            message_thread_id=thread,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("➡️ Передать без комментария", f"rt:{app_id}:tg:{to['user_id']}")],
                [btn("↩️ Отмена", f"rt:{app_id}:x")],
            ]),
        )
    elif act == "tg":  # передать без комментария
        PENDING.pop(key, None)
        from transfer import do_transfer
        name = await do_transfer(bot, app_id, user, int(parts[3]), None)
        await cb.answer(f"🔀 Передано: {name}" if name else "Передать не получилось", show_alert=not name)
        with suppress(TelegramBadRequest):
            await cb.message.delete()
    elif act == "x":
        PENDING.pop(key, None)
        await cb.answer("Отменено")
        with suppress(TelegramBadRequest):
            await cb.message.delete()
    else:
        await cb.answer()


async def finish_pending(bot: Bot, message: Message, key: tuple, app: dict) -> None:
    kind, app_id = PENDING[key]
    user = message.from_user
    text = message.text.strip()[:300]
    if len(text) < 2:
        await message.reply("Напишите хотя бы пару слов.")
        return
    PENDING.pop(key, None)
    if kind.startswith("transfer:"):
        from transfer import do_transfer
        name = await do_transfer(bot, app_id, user, int(kind.split(":")[1]), text)
        if name:
            await react(bot, message)
        else:
            await message.reply("Передать не получилось: заявка уже не у вас или менеджер недоступен.")
        return
    app = get_application(app_id)
    if not owned(app, user.id):
        await message.reply("Эта заявка уже не закреплена за вами.")
        return
    if kind in REASON_REQUIRED:
        ok = set_final(app, user, kind, f"Причина: {text}")
    else:
        ok = release_lead(app_id, user.id, user.full_name, text)
    if not ok:
        await message.reply("Эта заявка уже не закреплена за вами.")
        return
    from notify import notify_moderators_queue, refresh_cards
    await refresh_cards(bot, app_id)
    await react(bot, message)
    if kind == "release":
        await notify_moderators_queue(bot, "↩️ Заявка вернулась в очередь")


# ---------- подключение группы и имя менеджера ----------

@setup.message(Command("relay_here"))
async def on_relay_here(message: Message, bot: Bot) -> None:
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("Эту команду нужно отправить в группе: создайте группу, включите в ней «Темы», "
                             "добавьте бота администратором (право «Управление темами») и напишите /relay_here там.")
        return
    if not message.chat.is_forum:
        await message.answer("В этой группе не включены темы. Включите их в настройках группы (Тема → Темы) "
                             "и повторите /relay_here.")
        return
    set_setting("relay_chat", str(message.chat.id))
    await message.answer(
        "✅ Группа подключена. Когда менеджер берёт заявку, здесь появляется тема с карточкой клиента; "
        "всё, что менеджер пишет в теме, уходит клиенту от имени бота. Бот должен быть администратором "
        "с правом «Управление темами», а менеджер — участником группы.\n\n"
        "Темы для заявок, взятых раньше, не создаются разом (Telegram ограничивает частоту): тема появится, когда "
        "менеджер возьмёт новую заявку или клиент по старой напишет.")


@setup.message(Command("relay_sync"))
async def on_relay_sync(message: Message) -> None:
    """Постепенно создаёт темы для всех открытых заявок, взятых до подключения группы."""
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    if not relay_chat_id():
        await message.answer("Сначала подключите группу: отправьте /relay_here в группе с темами.")
        return
    total = len(open_without_topic())
    if not total:
        await message.answer("Все открытые заявки уже в темах — создавать нечего.")
        return
    set_setting("relay_backfill", "1")
    minutes = max(1, -(-total // 6))
    await message.answer(
        f"🔄 Создаю темы для открытых заявок: <b>{total}</b>. Бот делает это постепенно — по 3 каждые 30 секунд "
        f"(лимит Telegram), примерно {minutes} мин. Когда закончу, напишу. Остановить: /relay_off.")


@setup.message(Command("relay_off"))
async def on_relay_off(message: Message) -> None:
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    set_setting("relay_chat", "0")
    set_setting("relay_backfill", "0")
    await message.answer("Переписка через группу отключена. Существующие темы остаются, но бот их больше не обслуживает.")


class NameFlow(StatesGroup):
    text = State()


PREVIEW_SAMPLE = "Здравствуйте! Это по поводу вашей заявки. Вам удобно сейчас поговорить?"


def name_preview(user_id: int, fallback: str | None, name: str | None = None) -> str:
    """Как клиент увидит сообщение менеджера."""
    shown = name or manager_display_name(user_id, fallback)
    return (
        f"Сейчас клиенты видят вас как «<b>Менеджер {escape(shown)}</b>».\n\n"
        "👀 <b>Так выглядит ваше сообщение у клиента:</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"<b>Менеджер {escape(shown)}:</b>\n{PREVIEW_SAMPLE}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
    )


NAME_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [btn("✏️ Изменить имя", "nm:open")], [btn("🏠 Меню", "mp:menu")],
])


def clean_name(text: str) -> str | None:
    name = " ".join(text.split())
    if name.lower().startswith("менеджер "):
        name = name[9:].strip()
    return name if 1 <= len(name) <= 30 else None


@setup.callback_query(F.data == "nm:open", IsStaffFilter())
async def on_name_open(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(NameFlow.text)
    await cb.message.answer(
        name_preview(cb.from_user.id, cb.from_user.full_name)
        + "\n\n✏️ Напишите имя, под которым клиенты будут видеть вас (до 30 символов), например: <code>Дмитрий</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("↩️ Отмена", "nm:cancel")]]),
    )


@setup.callback_query(F.data == "nm:cancel", IsStaffFilter())
async def on_name_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await cb.answer("Отменено")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)


@setup.message(StateFilter(NameFlow.text), F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def on_name_text(message: Message, state: FSMContext) -> None:
    user = message.from_user
    name = clean_name(message.text)
    if name is None:
        await message.answer("Имя должно быть от 1 до 30 символов. Напишите ещё раз.")
        return
    set_setting(f"mgr_name:{user.id}", name)
    await state.set_state(None)
    await message.answer("✅ Имя сохранено.\n\n" + name_preview(user.id, user.full_name), reply_markup=NAME_KEYBOARD)


@setup.message(Command("name"))
async def on_name(message: Message, command: CommandObject, state: FSMContext) -> None:
    user = message.from_user
    if user is None or not is_staff(user.id):
        return
    await state.set_state(None)
    name = clean_name(command.args or "") if command.args else None
    if command.args and name is None:
        await message.answer("Имя должно быть от 1 до 30 символов.")
        return
    if name:
        set_setting(f"mgr_name:{user.id}", name)
    await message.answer(("✅ Имя сохранено.\n\n" if name else "") + name_preview(user.id, user.full_name),
                         reply_markup=NAME_KEYBOARD)
