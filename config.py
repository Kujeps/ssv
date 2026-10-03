"""Настройки бота: переменные окружения и константы, которые можно править."""
import os
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


def env_ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x]


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


BOT_TOKEN = os.environ["BOT_TOKEN"]
# Админы (владельцы): статистика, все заявки, модераторы, рассылка, ссылки, Excel.
ADMIN_USER_IDS = env_ids("ADMIN_USER_IDS")
DB_PATH = os.getenv("DB_PATH", "leads.db")

# Московское время — единое время работы: по нему считаются окна для звонков.
MSK = timezone(timedelta(hours=3))
# Часовой пояс для времени в карточках и Excel (смещение от UTC, по умолчанию Москва).
LOCAL_TZ = timezone(timedelta(hours=env_int("TZ_OFFSET_HOURS", 3)))

# Сколько минут у модератора на обработку взятой заявки; потом она вернётся в очередь.
HOLD_MINUTES = env_int("HOLD_MINUTES", 60)
# За сколько минут до возврата предупредить модератора.
HOLD_WARN_MINUTES = 10
# Через сколько часов после назначенного времени перезвона заявка вернётся в очередь.
CALLBACK_RETURN_HOURS = env_int("CALLBACK_RETURN_HOURS", 24)
# Напоминать админу о заявке, ждущей в очереди, через N минут (0 — выключить), не более REMIND_MAX раз.
REMIND_AFTER_MIN = env_int("REMIND_AFTER_MIN", 120)
REMIND_MAX = 3
# После скольких недозвонов сообщить админу.
NOCALL_ALERT_ATTEMPTS = 3

# ---------- настройки анкеты ----------

MIN_AGE, MAX_AGE = 18, 65
SOURCE_CODE_LEN = 24  # длина случайного кода в рекламной ссылке
PER_PAGE = 5  # строк на странице списков (архив, перезвоны)

UNITS = [
    "Сухопутные войска",
    "ВДВ",
    "ВМФ",
    "ВКС",
    "Артиллерия / ракетные",
    "Беспилотные системы",
    "Связь / РЭБ",
    "Медицинская служба",
    "Не определился — подскажите",
]
GENDERS = ["Мужской", "Женский"]
SERVED = ["Не служил(а)", "Срочная служба", "Служба по контракту"]
MEDICAL = ["Да", "Нет"]

# Часовые пояса: (город, смещение от Москвы в часах).
TZ_OPTIONS = [
    ("Калининград", -1), ("Москва", 0), ("Самара", 1), ("Екатеринбург", 2),
    ("Омск", 3), ("Красноярск", 4), ("Иркутск", 5), ("Якутск", 6),
    ("Владивосток", 7), ("Магадан", 8), ("Камчатка", 9),
]


def _tz_label(city: str, offset: int) -> str:
    return "Москва (МСК)" if offset == 0 else f"{city} (МСК{offset:+d})"


TZ_LABELS = [_tz_label(c, o) for c, o in TZ_OPTIONS]
TZ_BY_LABEL = {_tz_label(c, o): o for c, o in TZ_OPTIONS}
TZ_LABEL_BY_OFFSET = {o: _tz_label(c, o) for c, o in TZ_OPTIONS}

# Удобное время звонка: вариант -> окно (часы) по времени клиента; None — в любое время.
CALL_WINDOWS = {
    "Утро (9–12)": (9, 12),
    "День (12–17)": (12, 17),
    "Вечер (17–21)": (17, 21),
    "В любое время": None,
}
CALL_TIMES = list(CALL_WINDOWS)
# Ночью (по времени клиента) заявки выдаются в последнюю очередь.
NIGHT_FROM, NIGHT_TO = 22, 8

CHOICES = {
    "gender": GENDERS, "medical": MEDICAL, "unit": UNITS,
    "served": SERVED, "tz": TZ_LABELS, "call_time": CALL_TIMES,
}

# Шаги, которые нельзя пропустить. Остальные можно (но нужен хотя бы один контакт).
REQUIRED_STEPS = {"name", "gender", "medical", "age", "city", "unit", "tz"}

# ---------- статусы заявок ----------

# Пока заявка человека в одном из этих статусов, новую он подать не может.
BLOCKING_STATUSES = ("new", "work", "nocall")
FINAL_STATUSES = ("agreed", "refused", "junk")
# Для этих итогов модератор обязан указать причину.
REASON_REQUIRED = ("refused", "junk")

# Статусы заявок: ключ -> (значок, название).
STATUSES = {
    "new": ("🆕", "Новая"),
    "work": ("🔧", "В работе"),
    "nocall": ("📵", "Не дозвонились"),
    "agreed": ("✅", "Согласился"),
    "refused": ("❌", "Отказался"),
    "junk": ("🗑", "Мусор"),
}

# Когда перезвонить после недозвона: ключ -> название кнопки.
CALLBACK_OPTIONS = {
    "1h": "Через 1 час",
    "3h": "Через 3 часа",
    "tom": "Завтра утром (10:00 у клиента)",
}
