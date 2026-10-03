"""Сообщения персоналу: карточки заявок, уведомления модераторам и админам."""
import logging
from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from config import ADMIN_USER_IDS
from db import (
    get_application, get_notice_id, lead_message_refs, moderator_ids, queue_count,
    save_lead_message, set_notice_id,
)
from ui import btn, lead_keyboard, lead_text


async def send_lead_cards(bot: Bot, app_id: int) -> None:
    """Новая заявка целиком приходит каждому админу; карточка потом обновляется сама."""
    app = get_application(app_id)
    text, markup = lead_text(app, "admin"), lead_keyboard(app, "admin")
    for admin_id in ADMIN_USER_IDS:
        try:
            msg = await bot.send_message(admin_id, text, reply_markup=markup)
            save_lead_message(app_id, admin_id, msg.message_id, "admin")
        except Exception:
            logging.exception("Не удалось отправить карточку заявки админу %s", admin_id)


async def refresh_cards(bot: Bot, app_id: int) -> None:
    """Обновляет все карточки заявки (у админов и у модераторов)."""
    app = get_application(app_id)
    if app is None:
        return
    for chat_id, message_id, view in lead_message_refs(app_id):
        if view == "admin" and chat_id not in ADMIN_USER_IDS:
            continue  # старые карточки из времён группы менеджеров
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                lead_text(app, view, chat_id), chat_id=chat_id, message_id=message_id,
                reply_markup=lead_keyboard(app, view, chat_id),
            )


async def notify_moderators_queue(bot: Bot, headline: str = "🔔 Поступила новая заявка") -> None:
    """Модераторам — только факт и число заявок в очереди, без данных клиента."""
    text = f"{headline}\n\n📥 В очереди необработанных заявок: <b>{queue_count()}</b>"
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("📥 Взять заявку", "mp:take")]])
    for mod_id in moderator_ids():
        old = get_notice_id(mod_id)
        if old:  # одно «живое» уведомление вместо вороха сообщений
            with suppress(TelegramBadRequest):
                await bot.delete_message(mod_id, old)
        try:
            msg = await bot.send_message(mod_id, text, reply_markup=markup)
            set_notice_id(mod_id, msg.message_id)
        except Exception:
            logging.info("Модератор %s ещё не запускал бота — уведомление не доставлено", mod_id)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logging.exception("Не удалось отправить сообщение админу %s", admin_id)
