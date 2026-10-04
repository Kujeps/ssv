"""Точка входа: Telegram-бот приёма заявок.

Запуск: python bot.py
Модули: config (настройки), db (база), form (анкета), ui (оформление), notify (сообщения
персоналу), client (клиентская часть), staff (админ и модераторы), jobs (таймеры).
"""
import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from client import client
from config import ADMIN_USER_IDS, BOT_TOKEN, DB_PATH
from db import init_db, moderator_ids
from jobs import maintenance_loop
from quick import quick
from staff import staff
from table import table

bot_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
dp = Dispatcher()
# Порядок важен: сначала таблица модератора и персонал (админ, модераторы), затем клиенты.
dp.include_router(table)
dp.include_router(staff)
dp.include_router(quick)
dp.include_router(client)


MODERATOR_COMMANDS = [
    BotCommand(command="start", description="Панель модератора"),
    BotCommand(command="table", description="Моя таблица"),
]


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Оставить заявку")],
        scope=BotCommandScopeDefault(),
    )
    admin_commands = [
        BotCommand(command="start", description="Панель администратора"),
        BotCommand(command="archive", description="Все заявки и поиск"),
        BotCommand(command="moderators", description="Модераторы"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="export", description="Выгрузить заявки в Excel"),
        BotCommand(command="links", description="Ссылки для рекламы"),
        BotCommand(command="broadcast", description="Рассылка пользователям"),
    ]
    for admin_id in ADMIN_USER_IDS:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
    for mod_id in moderator_ids():
        if mod_id in ADMIN_USER_IDS:
            continue
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.set_my_commands(MODERATOR_COMMANDS, scope=BotCommandScopeChat(chat_id=mod_id))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not ADMIN_USER_IDS:
        logging.warning("ADMIN_USER_IDS не задан: никто не сможет управлять ботом")
    init_db()
    bot = Bot(BOT_TOKEN, default=bot_props)
    await setup_commands(bot)
    jobs = asyncio.create_task(maintenance_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        jobs.cancel()
        logging.info("База данных: %s", DB_PATH)


if __name__ == "__main__":
    asyncio.run(main())
