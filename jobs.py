"""Фоновые проверки: время на обработку заявки, перезвоны, напоминания клиентам."""
import asyncio
import logging
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from config import HOLD_MINUTES
from db import (
    due_callbacks, expired_holds, get_application, hold_warnings, mark_cb_notified, mark_warned,
    overdue_callbacks, system_requeue,
)
from notify import notify_admins, notify_moderators_queue, refresh_cards
from reminders import run_reminders
from ui import btn
from utils import fmt_hhmm, utc_now

CHECK_EVERY_SECONDS = 30


async def say(bot: Bot, chat_id: int | None, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
    except Exception:
        logging.info("Не удалось отправить сообщение %s", chat_id)


async def run_maintenance(bot: Bot, now: datetime | None = None) -> None:
    now = now or utc_now()
    returned = False

    expired = expired_holds(now)
    expired_ids = {app_id for app_id, _ in expired}

    # 1. Предупреждение за 10 минут до возврата заявки в очередь.
    for app_id, mod_id, left in hold_warnings(now):
        if app_id in expired_ids:
            continue
        mark_warned(app_id)
        await say(bot, mod_id,
                  f"⏳ <b>Заявка #{app_id}</b>: осталось {left} мин на обработку. "
                  "Если не успеете, она вернётся в список необработанных заявок.")

    # 2. Время на обработку вышло — заявка возвращается в очередь.
    for app_id, mod_id in expired:
        app = get_application(app_id)
        if app and system_requeue(app_id, "timeout", mod_id):
            returned = True
            await say(bot, mod_id,
                      f"⏰ Время на заявку #{app_id} вышло — она возвращена в список необработанных заявок.")
            await notify_admins(
                bot,
                f"↩️ Заявка #{app_id} возвращена в очередь: модератор "
                f"<b>{escape(app['assigned_name'] or str(mod_id))}</b> не обработал её за {fmt_hhmm(HOLD_MINUTES)}.",
            )
            await refresh_cards(bot, app_id)

    # 3. Пора перезвонить по недозвонившейся заявке.
    for app_id, mod_id in due_callbacks(now):
        mark_cb_notified(app_id)
        app = get_application(app_id)
        await say(
            bot, mod_id,
            f"📞 Пора перезвонить по заявке #{app_id} ({escape(app['name'] or 'без имени')}).",
            InlineKeyboardMarkup(inline_keyboard=[[btn("📞 Позвонить сейчас", f"mt:{app_id}")]]),
        )

    # 4. Перезвон просрочен слишком сильно — заявка возвращается в общую очередь.
    for app_id, mod_id in overdue_callbacks(now):
        app = get_application(app_id)
        if app and system_requeue(app_id, "cb_timeout", mod_id):
            returned = True
            await say(bot, mod_id,
                      f"⏰ Перезвон по заявке #{app_id} просрочен — она возвращена в общую очередь.")
            await notify_admins(
                bot,
                f"↩️ Заявка #{app_id} возвращена в очередь: модератор "
                f"<b>{escape(app['assigned_name'] or str(mod_id))}</b> не перезвонил вовремя.",
            )
            await refresh_cards(bot, app_id)

    if returned:
        await notify_moderators_queue(bot, "↩️ Заявка вернулась в очередь")

    # 5. Напоминания тем, кто запустил бота, но не оставил заявку (если включены админом).
    await run_reminders(bot, now)


async def maintenance_loop(bot: Bot) -> None:
    while True:
        try:
            await run_maintenance(bot)
        except Exception:
            logging.exception("Ошибка фоновой проверки")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
