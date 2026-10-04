"""Анкета клиента: шаги, тексты, проверка введённых значений."""
import re
from html import escape

from aiogram.fsm.state import State, StatesGroup

from config import GENDERS, MAX_AGE, MIN_AGE, TZ_BY_LABEL, TZ_LABEL_BY_OFFSET
from utils import norm_phone


class Form(StatesGroup):
    name = State()
    phone = State()
    tg = State()
    max = State()
    wa = State()
    gender = State()
    medical = State()
    age = State()
    city = State()
    unit = State()
    served = State()
    tz = State()
    call_time = State()
    comment = State()
    confirm = State()


# Порядок шагов анкеты и соответствие шаг -> состояние.
STEPS = [
    "name", "phone", "tg", "max", "wa",
    "gender", "medical", "age", "city", "unit", "served", "tz", "call_time", "comment",
]
# Шаги, которые задаются не всем: шаг -> условие по уже введённым данным.
CONDITIONAL_STEPS = {"medical": lambda data: data.get("gender") == GENDERS[1]}
STEP_STATES = {step: getattr(Form, step) for step in STEPS}
CONTACT_KEYS = ["phone", "tg", "max", "wa"]
LAST_CONTACT_STEP = "wa"


def active_steps(data: dict) -> list[str]:
    """Шаги анкеты для конкретного человека (без неподходящих по условию)."""
    return [s for s in STEPS if s not in CONDITIONAL_STEPS or CONDITIONAL_STEPS[s](data)]


FIELDS = [
    ("name", "👤", "Имя"),
    ("phone", "📞", "Телефон"),
    ("tg", "✈️", "Telegram"),
    ("max", "💬", "MAX"),
    ("wa", "🟢", "WhatsApp"),
    ("gender", "⚥", "Пол"),
    ("medical", "🩺", "Мед. образование"),
    ("age", "🎂", "Возраст"),
    ("city", "📍", "Город"),
    ("unit", "🎖", "Подразделение"),
    ("served", "🪖", "Служба ранее"),
    ("tz", "🌍", "Часовой пояс"),
    ("call_time", "🕒", "Удобное время для звонка"),
    ("comment", "💭", "Комментарий"),
]

GREETING = (
    "👋 <b>Здравствуйте, оставьте заявку и мы свяжемся с вами!</b>\n\n"
    "Это займёт пару минут — несколько простых вопросов.\n\n"
    "<i>Нажимая «Оставить заявку», вы соглашаетесь на обработку персональных данных.</i>"
)

PROMPTS = {
    "name": "👤 <b>Как к вам обращаться?</b>\n\nНапишите ваше имя.",
    "phone": (
        "📞 <b>Номер телефона</b>\n\n"
        "Отправьте номер кнопкой ниже или напишите его вручную.\n"
        "<i>Достаточно указать любой один способ связи — остальные можно пропустить.</i>"
    ),
    "tg": "✈️ <b>Telegram</b>\n\nНапишите ваш @ник или ссылку на профиль.",
    "max": "💬 <b>MAX</b>\n\nУкажите номер телефона или ссылку на ваш профиль в MAX.",
    "wa": "🟢 <b>WhatsApp</b>\n\nУкажите номер, на котором есть WhatsApp.",
    "gender": "⚥ <b>Ваш пол</b>\n\nВыберите вариант.",
    "medical": (
        "🩺 <b>Есть ли у вас медицинское образование?</b>\n\n"
        "Выберите вариант или напишите подробнее — например, «фельдшер» или «медсестра»."
    ),
    "age": "🎂 <b>Сколько вам полных лет?</b>\n\nНапишите число, например: <code>27</code>",
    "city": "📍 <b>Город или регион, где вы находитесь</b>\n\nНапример: <code>Самара</code>",
    "unit": (
        "🎖 <b>Желаемое подразделение</b>\n\n"
        "Выберите из списка или напишите своё. "
        "Если ещё не определились — выберите последний пункт, мы подскажем."
    ),
    "served": "🪖 <b>Служили ли вы ранее?</b>\n\nВыберите вариант или напишите подробнее.",
    "tz": (
        "🌍 <b>Ваш часовой пояс</b>\n\n"
        "Выберите, сколько у вас времени относительно Москвы — "
        "так мы позвоним, когда вам удобно."
    ),
    "call_time": (
        "🕒 <b>Когда вам удобно принять звонок?</b>\n\n"
        "Выберите вариант — время указывайте <b>по вашему часовому поясу</b>."
    ),
    "comment": (
        "💭 <b>Хотите что-то добавить?</b>\n\n"
        "Вопросы, пожелания, особенности — всё, что поможет нам. Можно пропустить."
    ),
}

ERRORS = {
    "name": "Пожалуйста, напишите имя текстом (до 60 символов).",
    "phone": "Не похоже на номер телефона. Пример: <code>+7 999 123-45-67</code>",
    "tg": "Не получилось разобрать ник. Пример: <code>@username</code>",
    "max": "Укажите номер телефона или ссылку/ник в MAX.",
    "wa": "Не похоже на номер телефона. Пример: <code>+7 999 123-45-67</code>",
    "gender": "Выберите вариант кнопкой под вопросом: «Мужской» или «Женский».",
    "medical": "Слишком длинно — сократите, пожалуйста, до 100 символов.",
    "age": (
        f"Служба по контракту доступна от {MIN_AGE} до {MAX_AGE} лет. "
        "Проверьте, пожалуйста, возраст и напишите число."
    ),
    "city": "Напишите название города или региона (от 2 до 100 символов).",
    "unit": "Выберите подразделение из списка или напишите его название (до 100 символов).",
    "served": "Слишком длинно — сократите, пожалуйста, до 200 символов.",
    "tz": "Выберите часовой пояс кнопкой под вопросом.",
    "call_time": "Слишком длинно — сократите, пожалуйста, до 100 символов.",
    "comment": "Слишком длинно — сократите, пожалуйста, до 500 символов.",
}

PENDING_REVIEW = (
    "⏳ <b>Ваша заявка на рассмотрении</b>\n\n"
    "Мы получили вашу заявку от {when} и скоро свяжемся с вами. "
    "Пожалуйста, подождите — подавать заявку повторно не нужно."
)
PENDING_WORK = (
    "🔧 <b>Ваша заявка уже в работе</b>\n\n"
    "Наш специалист занимается вашей заявкой от {when} и свяжется с вами. "
    "Пожалуйста, подождите — подавать заявку повторно не нужно."
)

BTN_SHARE = "📱 Поделиться номером"
BTN_SKIP = "⏭ Пропустить"
BTN_BACK = "⬅️ Назад"


# ---------- проверка введённых значений ----------

def norm_tg(text: str) -> str | None:
    m = re.fullmatch(
        r"(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{4,31})/?", text.strip()
    )
    if m:
        return "@" + m.group(1)
    return norm_phone(text)


def norm_max(text: str) -> str | None:
    phone = norm_phone(text)
    if phone:
        return phone
    return text.strip() if 3 <= len(text.strip()) <= 100 else None


def norm_wa(text: str) -> str | None:
    m = re.search(r"wa\.me/\+?(\d{10,15})", text)
    if m:
        return "+" + m.group(1)
    return norm_phone(text)


def norm_name(text: str) -> str | None:
    text = text.strip()
    return text if 1 <= len(text) <= 60 else None


def norm_gender(text: str) -> str | None:
    t = text.strip().lower()
    if t in ("м", "муж", "мужской", "мужчина"):
        return GENDERS[0]
    if t in ("ж", "жен", "женский", "женщина"):
        return GENDERS[1]
    return None


def norm_age(text: str) -> str | None:
    m = re.fullmatch(r"\D*(\d{1,3})\D*", text.strip())
    if not m:
        return None
    age = int(m.group(1))
    return str(age) if MIN_AGE <= age <= MAX_AGE else None


def norm_tz(text: str) -> str | None:
    """Часовой пояс словами («Самара», «москва») или числом («+2», «мск+2»)."""
    t = text.strip().lower()
    for label in TZ_BY_LABEL:
        if t == label.lower() or t == label.split(" (")[0].lower():
            return label
    m = re.fullmatch(r"(?:мск\s*)?([+-]?\d{1,2})", t)
    if m and int(m.group(1)) in TZ_LABEL_BY_OFFSET:
        return TZ_LABEL_BY_OFFSET[int(m.group(1))]
    if t in ("мск", "мск+0"):
        return TZ_LABEL_BY_OFFSET[0]
    return None


def text_normalizer(min_len: int, max_len: int):
    def norm(text: str) -> str | None:
        text = text.strip()
        return text if min_len <= len(text) <= max_len else None
    return norm


NORMALIZERS = {
    "name": norm_name,
    "phone": norm_phone,
    "tg": norm_tg,
    "max": norm_max,
    "wa": norm_wa,
    "gender": norm_gender,
    "medical": text_normalizer(1, 100),
    "age": norm_age,
    "city": text_normalizer(2, 100),
    "unit": text_normalizer(2, 100),
    "served": text_normalizer(1, 200),
    "tz": norm_tz,
    "call_time": text_normalizer(1, 100),
    "comment": text_normalizer(1, 500),
}


def parse_source(arg: str | None) -> str | None:
    """Метка рекламы из ссылки t.me/бот?start=метка."""
    return arg if arg and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arg) else None


# ---------- оформление анкеты ----------

def header(step: str, data: dict) -> str:
    steps = active_steps(data)
    i = steps.index(step)
    bar = "●" * (i + 1) + "○" * (len(steps) - i - 1)
    return f"<b>Шаг {i + 1} из {len(steps)}</b>  {bar}"


def card(data: dict, skip_empty: bool = False) -> str:
    lines = []
    for key, icon, label in FIELDS:
        if key in CONDITIONAL_STEPS and not CONDITIONAL_STEPS[key](data):
            continue
        value = data.get(key)
        if value:
            lines.append(f"{icon} <b>{label}:</b> {escape(str(value))}")
        elif not skip_empty:
            lines.append(f"{icon} <b>{label}:</b> —")
    return "\n".join(lines)


# ---------- короткий диалог: «27, Иркутск, мужчина» ----------

GENDER_WORDS = {
    "м": GENDERS[0], "муж": GENDERS[0], "мужской": GENDERS[0], "мужчина": GENDERS[0],
    "парень": GENDERS[0], "юноша": GENDERS[0],
    "ж": GENDERS[1], "жен": GENDERS[1], "женский": GENDERS[1], "женщина": GENDERS[1],
    "девушка": GENDERS[1],
}
INTRO_FILLER = {
    "мне", "лет", "года", "год", "я", "из", "в", "г", "город", "живу", "пол", "возраст", "и",
    "меня", "мой", "моя", "родом", "проживаю", "из-за", "по",
}


def parse_intro(text: str) -> dict:
    """Из свободного текста достаёт возраст, пол и город. Чего нет — None.
    «27, Иркутск, мужчина», «Иркутск 27 муж», «мне 27 лет, живу в Омске, девушка»."""
    text = re.sub(r"\bг\.\s*", " ", text, flags=re.I)
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9.\-]*", text)
    age = gender = None
    rest = []
    for tok in tokens:
        low = tok.lower().strip(".")
        m = re.fullmatch(r"(\d{1,3})(?:лет|года|год)?", low)
        if m and age is None:
            age = int(m.group(1))
        elif low in GENDER_WORDS and gender is None:
            gender = GENDER_WORDS[low]
        elif low in INTRO_FILLER or re.fullmatch(r"\d+", low):
            continue
        else:
            rest.append(tok)
    city = " ".join(rest).strip(" .,-")
    if city and city == city.lower():
        city = "-".join(part.capitalize() for part in city.split("-")) if "-" in city and " " not in city \
            else " ".join(w.capitalize() for w in city.split())
    if not (2 <= len(city) <= 60 and re.search(r"[A-Za-zА-Яа-яЁё]", city)):
        city = None
    return {"age": age, "gender": gender, "city": city}
