"""Оформление: клавиатуры, карточки заявок, панели модератора и админа."""
from datetime import timedelta
from html import escape

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    User,
)

from config import CHOICES, HOLD_MINUTES, LOCAL_TZ, REQUIRED_STEPS, STATUSES
from db import (
    active_lead, is_moderator, list_moderators, parked_leads, queue_summary, reminders_enabled,
    source_label,
)
from form import (
    BTN_BACK, BTN_SHARE, BTN_SKIP, PENDING_REVIEW, PENDING_WORK, active_steps, card,
)
from utils import (
    client_local, fmt_hhmm, fmt_ts, fmt_wait, parse_utc, tz_text, utc_now, utc_str,
    call_time_display,
)


def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def choice_rows(step: str) -> list[list[InlineKeyboardButton]]:
    """Варианты ответа: короткие по два в ряд, длинные — каждый в своей строке."""
    rows, pair = [], []
    for n, label in enumerate(CHOICES[step]):
        b = btn(label, f"pick:{n}")
        if len(label) > 22:
            if pair:
                rows.append(pair)
                pair = []
            rows.append([b])
        else:
            pair.append(b)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
    if pair:
        rows.append(pair)
    return rows


def step_keyboard(step: str, data: dict, user: User) -> InlineKeyboardMarkup:
    i = active_steps(data).index(step)
    rows = []
    if step == "name" and user.first_name:
        rows.append([btn(f"👋 Меня зовут {user.first_name}", "use:name")])
    if step == "tg" and user.username:
        rows.append([btn(f"✈️ Мой @{user.username}", "use:tg")])
    if step in ("max", "wa") and data.get("phone"):
        rows.append([btn(f"📞 Тот же номер {data['phone']}", "use:phone")])
    if step in CHOICES:
        rows.extend(choice_rows(step))
    nav = []
    if i > 0:
        nav.append(btn(BTN_BACK, "back"))
    if step not in REQUIRED_STEPS:
        nav.append(btn(BTN_SKIP, "skip"))
    if nav:
        rows.append(nav)
    rows.append([btn("✖️ Отмена", "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SHARE, request_contact=True)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_SKIP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="+7 999 123-45-67",
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Отправить заявку", "send")],
            [btn("✏️ Заполнить заново", "restart"), btn(BTN_BACK, "back")],
            [btn("✖️ Отмена", "cancel")],
        ]
    )


def apply_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("📝 Оставить заявку", "apply")]])


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📣 Разослать", "bc_send")],
            [btn("✖️ Отмена", "bc_cancel")],
        ]
    )


def pending_notice(app: dict) -> str:
    template = PENDING_WORK if app["status"] == "work" else PENDING_REVIEW
    return template.format(when=fmt_ts(app["created_at"]))



# ---------- карточка заявки ----------

def _deadline(app: dict):
    taken = parse_utc(app["taken_at"])
    return (taken + timedelta(minutes=HOLD_MINUTES)).astimezone(LOCAL_TZ) if taken else None


def minutes_left(app: dict) -> int:
    """Сколько минут осталось на обработку взятой заявки."""
    taken = parse_utc(app["taken_at"])
    if not taken:
        return 0
    return max(0, HOLD_MINUTES - int((utc_now() - taken).total_seconds() // 60))


def lead_text(app: dict, view: str = "admin", viewer_id: int | None = None) -> str:
    """Карточка заявки. view='admin' — полная; view='mod' — полная только владельцу заявки:
    у модератора, с которого заявка снята, остаётся лишь заглушка без данных."""
    status = app["status"]
    icon, label = STATUSES.get(status, STATUSES["new"])
    if view == "mod" and app["assigned_to"] != viewer_id:
        what = "возвращена в очередь" if status == "new" else "больше не закреплена за вами"
        return f"📋 <b>Заявка #{app['id']}</b>\n\n↩️ Заявка {what}."

    tz_offset = app["tz_offset"]
    data = {
        "name": app["name"], "phone": app["phone"], "tg": app["telegram"],
        "max": app["max_contact"], "wa": app["whatsapp"], "gender": app["gender"],
        "medical": app["medical"], "age": app["age"], "city": app["city"],
        "unit": app["unit"], "served": app["served"], "tz": tz_text(tz_offset),
        "call_time": call_time_display(app["call_time"], tz_offset), "comment": app["comment"],
    }
    username = f"@{app['username']}" if app["username"] else "нет username"
    lines = [
        f"📋 <b>Заявка #{app['id']}</b> · {icon} {label}",
        "",
        card(data, skip_empty=True),
        "",
        f"🔗 <a href=\"tg://user?id={app['user_id']}\">{escape(app['full_name'] or '—')}</a>"
        f" · {escape(username)} · ID <code>{app['user_id']}</code>",
    ]
    if tz_offset is not None and status in ("new", "work", "nocall"):
        lines.append(f"🕐 У клиента сейчас: <b>{client_local(tz_offset):%H:%M}</b>")
    if app["source"]:
        lines.append(f"🔖 Источник: <b>{escape(source_label(app['source']))}</b>")

    lines.append("")
    if app["assigned_name"] and status != "new":
        lines.append(f"👷 Модератор: <b>{escape(app['assigned_name'])}</b>")
    if status == "work":
        deadline = _deadline(app)
        if view == "mod":
            lines.append(
                f"⏱ <b>У вас есть {fmt_hhmm(HOLD_MINUTES)} на обработку этой заявки</b> "
                f"(до {deadline:%H:%M} МСК). После этого она будет возвращена "
                "в список необработанных заявок."
            )
        elif deadline:
            lines.append(
                f"⏱ Взята {fmt_ts(app['taken_at'])}; вернётся в очередь в {deadline:%H:%M}, "
                "если не будет обработана"
            )
    elif status == "nocall":
        lines.append(
            f"📵 Недозвонов: <b>{app['attempts'] or 1}</b> · перезвонить: "
            f"<b>{fmt_ts(app['callback_at'])}</b>"
        )
    elif status in ("agreed", "refused", "junk"):
        lines.append(f"{icon} <b>{label}</b> — {escape(app['status_by'] or '—')} · {fmt_ts(app['status_at'])}")
    elif (app["attempts"] or 0) > 0:
        lines.append(f"📵 Ранее не дозвонились: {app['attempts']} раз")
    notes = app["notes"][-5:]
    if notes:
        lines += ["", "📝 <b>Заметки:</b>"]
        for n in notes:
            lines.append(
                f"• <b>{escape(n['by_name'] or '—')}</b> ({fmt_ts(n['created_at'])}): {escape(n['text'])}"
            )
    return "\n".join(line for line in lines if line is not None)


def lead_keyboard(app: dict, view: str = "admin", viewer_id: int | None = None):
    """Кнопки под карточкой. None — кнопок нет."""
    status, app_id = app["status"], app["id"]
    menu = btn("🏠 Меню", "mp:menu")
    if view == "admin":
        if status == "new":
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[btn("↩️ Вернуть в очередь", f"ad:{app_id}:requeue")]])
    if app["assigned_to"] != viewer_id:
        return InlineKeyboardMarkup(inline_keyboard=[[menu]])
    if status == "work":
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Согласился", f"mv:{app_id}:agreed"), btn("❌ Отказался", f"mv:{app_id}:refused")],
            [btn("📵 Не дозвонился", f"mv:{app_id}:nocall"), btn("🗑 Мусор", f"mv:{app_id}:junk")],
            [btn("✍️ Заметка", f"mv:{app_id}:note"), btn("↩️ Вернуть в очередь", f"mv:{app_id}:release")],
        ])
    if status == "nocall":
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("📞 Позвонить сейчас", f"mt:{app_id}")],
            [btn("✍️ Заметка", f"mv:{app_id}:note"), btn("↩️ Вернуть в очередь", f"mv:{app_id}:release")],
            [menu],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[[btn("📥 Взять следующую заявку", "mp:take")], [menu]])


# ---------- панели ----------

def mod_panel(mod_id: int) -> tuple[str, InlineKeyboardMarkup]:
    summary = queue_summary()
    active = active_lead(mod_id)
    parked = parked_leads(mod_id)
    now = utc_str()
    due = sum(1 for p in parked if p["callback_at"] and p["callback_at"] <= now)
    lines = [
        "👷 <b>Панель модератора</b>",
        "",
        f"📥 В очереди необработанных заявок: <b>{summary.get('new', 0)}</b>",
    ]
    if active:
        lines.append(
            f"🔧 У вас в работе: заявка #{active['id']} (осталось {fmt_wait(minutes_left(active))})"
        )
    if parked:
        lines.append(
            f"🔁 Перезвонить позже: <b>{len(parked)}</b>" + (f" (пора звонить: {due})" if due else "")
        )
    rows = [[btn(f"📂 Моя заявка #{active['id']}", "mp:active")] if active
            else [btn("📥 Взять заявку", "mp:take")]]
    rows.append([btn(f"🔁 Перезвонить ({len(parked)})", "mp:cb")])
    rows.append([btn("🗂 Мои заявки", "mp:arch"), btn("📊 Моя статистика", "mp:stats")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    summary = queue_summary()
    lines = [
        "🛠 <b>Панель администратора</b>",
        "",
        f"📥 В очереди: <b>{summary.get('new', 0)}</b> · 🔧 В работе: <b>{summary.get('work', 0)}</b>"
        f" · 📵 Перезвонить: <b>{summary.get('nocall', 0)}</b>",
        f"👥 Модераторов: <b>{len(list_moderators())}</b>",
    ]
    rows = [
        [btn("🗂 Все заявки", "ap:arch"), btn("👥 Модераторы", "ap:mods")],
        [btn("👤 Пользователи", "ap:users"), btn("📊 Статистика", "ap:stats")],
        [btn("📤 Excel", "ap:export"), btn("🔗 Ссылки", "ap:links")],
        [btn("📣 Рассылка", "ap:bc"),
         btn(f"🔔 Напоминания: {'ВКЛ' if reminders_enabled() else 'ВЫКЛ'}", "ap:rem")],
    ]
    if is_moderator(admin_id):
        active = active_lead(admin_id)
        rows.append([btn(f"📂 Моя заявка #{active['id']}", "mp:active")] if active
                    else [btn("📥 Взять заявку", "mp:take")])
    rows.append([btn("📝 Подать заявку (проверка формы)", "apply")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
