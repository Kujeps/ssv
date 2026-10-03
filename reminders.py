"""Напоминания тем, кто запустил бота, но не оставил заявку."""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from config import ADMIN_USER_IDS, MSK, REMINDER_BATCH, REMINDER_DELAYS_HOURS, REMINDER_WINDOW_MSK
from db import (
    mark_blocked, mark_reminder_sent, moderator_ids, reminder_candidates, reminders_enabled,
)
from ui import btn
from utils import utc_now

# Тексты по порядку напоминаний. Две группы: только запустили бота («start»)
# и начали анкету, но не закончили («form»).
TEXTS = {
    "start": [
        "👋 <b>Вы заглянули к нам, но заявку пока не оставили.</b>\n\n"
        "Это займёт пару минут — несколько простых вопросов, и мы свяжемся с вами "
        "в удобное время. 📞",
        "🕒 <b>Мы пока не получили вашу заявку.</b>\n\n"
        "Оставьте её — мы позвоним, когда вам удобно, и ответим на все вопросы.",
        "💬 <b>Остались вопросы?</b>\n\n"
        "Оставьте заявку — и мы ответим на все ваши вопросы.",
        "📞 <b>Мы на связи.</b>\n\n"
        "Расскажите о себе в короткой анкете — мы свяжемся с вами и подберём подходящий вариант.",
        "🙂 <b>Не откладывайте на потом.</b>\n\n"
        "Заявка занимает пару минут, а звонок мы назначим на удобное для вас время.",
        "✉️ <b>Последнее напоминание.</b>\n\n"
        "Если вам это интересно — оставьте заявку, мы всё расскажем. "
        "Если нет — просто нажмите «Не напоминать».",
    ],
    "form": [
        "✍️ <b>Вы почти закончили!</b>\n\nОсталось пару вопросов — заполним заявку?",
        "📝 <b>Заявка осталась незавершённой.</b>\n\n"
        "Завершите её за пару минут — и мы свяжемся с вами.",
        "💬 <b>Остались вопросы?</b>\n\n"
        "Оставьте заявку — и мы ответим на все ваши вопросы. Анкета займёт пару минут.",
        "📞 <b>Мы всё ещё ждём вашу заявку.</b>\n\n"
        "Заполните анкету — и мы позвоним в удобное для вас время.",
        "🙂 <b>Пара минут — и готово.</b>\n\n"
        "Давайте закончим заявку, чтобы мы могли с вами связаться.",
        "✉️ <b>Последнее напоминание.</b>\n\n"
        "Хотите закончить заявку? Если нет — просто нажмите «Не напоминать».",
    ],
}
assert all(len(t) == len(REMINDER_DELAYS_HOURS) for t in TEXTS.values()), "тексты и график не совпадают"

BUTTON_TEXT = {"start": "📝 Оставить заявку", "form": "📝 Заполнить заявку"}
OPTOUT_DONE = "🔕 Хорошо, больше не будем напоминать. Если захотите оставить заявку — нажмите /start."


def reminder_keyboard(group: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(BUTTON_TEXT[group], "apply")],
        [btn("🔕 Не напоминать", "rem:off")],
    ])


def in_window(now: datetime | None = None) -> bool:
    """Напоминаем только днём по Москве — ночью людей не беспокоим."""
    hour = (now or utc_now()).astimezone(MSK).hour
    return REMINDER_WINDOW_MSK[0] <= hour < REMINDER_WINDOW_MSK[1]


def staff_ids() -> list[int]:
    return list({*ADMIN_USER_IDS, *moderator_ids()})


def pending_counts(now: datetime | None = None) -> tuple[int, int]:
    """(кому положена серия, из них кому пора слать уже сейчас) — для подтверждения включения."""
    now = now or utc_now()
    people = reminder_candidates(staff_ids())
    return len(people), sum(1 for c in people if c["due_at"] <= now)


async def run_reminders(bot: Bot, now: datetime | None = None) -> int:
    """Отправляет напоминания, срок которых наступил. Возвращает число отправленных."""
    if not reminders_enabled():
        return 0
    now = now or utc_now()
    if not in_window(now):
        return 0
    sent = 0
    for c in reminder_candidates(staff_ids()):
        if c["due_at"] > now:
            break  # список отсортирован по сроку
        if sent >= REMINDER_BATCH:
            break
        try:
            await bot.send_message(
                c["user_id"], TEXTS[c["group"]][c["stage"]], reply_markup=reminder_keyboard(c["group"])
            )
            mark_reminder_sent(c["user_id"], c["stage"], c["group"])
            sent += 1
        except TelegramForbiddenError:
            mark_blocked(c["user_id"])  # заблокировал бота — больше не пишем
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            break
        except TelegramBadRequest as e:
            if any(k in str(e).lower() for k in ("chat not found", "deactivated", "user not found")):
                mark_blocked(c["user_id"])  # аккаунт удалён или чат недоступен
            else:
                logging.exception("Не удалось отправить напоминание %s", c["user_id"])
        except Exception:
            logging.exception("Не удалось отправить напоминание %s", c["user_id"])
        await asyncio.sleep(0.05)
    return sent
