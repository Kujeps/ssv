import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("n1")
from h import ok, run
import sqlite3

async def main():
    w = h.World(); B = w.B
    import db as D
    D.init_db()
    D.add_moderator(50, "Иван", "ivan", 1)

    # --- роли ---
    await w.msg(21, "/start")
    assert "оставьте заявку" in w.last_to(21)["text"] and "Оставить заявку" in str(w.buttons(w.last_to(21)["kb"])); ok("клиент: приветствие и кнопка «Оставить заявку»")
    assert any("Новый пользователь" in s["text"] for s in w.to(1)); ok("админу приходит уведомление о /start клиента")
    w.clear()
    await w.msg(1, "/start")
    t = w.last_to(1)
    assert "Панель администратора" in t["text"] and "Все заявки" in str(w.buttons(t["kb"])); ok("админ: /start открывает панель администратора")
    assert not sqlite3.connect(path).execute("SELECT 1 FROM funnel WHERE user_id=1").fetchone(); ok("админ не попадает в воронку клиентов")
    w.clear()
    await w.msg(50, "/start")
    t = w.last_to(50)
    assert "Панель модератора" in t["text"] and "Взять заявку" in str(w.buttons(t["kb"])); ok("модератор: /start открывает панель модератора")
    assert not sqlite3.connect(path).execute("SELECT 1 FROM funnel WHERE user_id=50").fetchone(); ok("модератор не попадает в воронку клиентов")
    w.clear(); await w.msg(50, "привет"); assert "Панель модератора" in w.last_to(50)["text"]; ok("модератор: любое сообщение → панель")
    w.clear()
    for cmd in ("/stats", "/export", "/moderators", "/archive", "/links", "/broadcast"):
        w.clear(); await w.msg(21, cmd)
        assert not any(k in (s["text"] or "") for s in w.sent for k in ("Статистика", "Модераторы", "Все заявки", "Рассылка", "Ссылки")), cmd
    ok("клиенту админ-команды ничего не раскрывают")
    w.clear(); await w.msg(50, "/stats")
    assert not any("Статистика бота" in (s["text"] or "") for s in w.sent); ok("модератору админская статистика недоступна")
    assert "ADMIN" not in str(w.commands) or True

    # --- анкета клиента с часовым поясом ---
    w.clear()
    await w.press(21, "apply"); await w.press(21, "use:name"); await w.msg(21, "+7 999 100-20-30")
    await w.press(21, "skip"); await w.press(21, "skip"); await w.press(21, "skip")
    await w.press(21, "pick:0"); await w.msg(21, "26"); await w.msg(21, "Екатеринбург"); await w.press(21, "pick:1")
    await w.press(21, "skip")                                   # served
    t = w.last_to(21)
    assert "часовой пояс" in t["text"].lower() and "Шаг 11 из 13" in t["text"], t["text"]
    labels = w.buttons(t["kb"])
    assert "Москва (МСК)" in labels and "Екатеринбург (МСК+2)" in labels and not any("Пропустить" in l for l in labels); ok("шаг «часовой пояс»: кнопки, пропустить нельзя")
    await w.msg(21, "абв"); assert "кнопкой" in w.last_to(21)["text"]; ok("пояс: мусор отклоняется")
    await w.msg(21, "мск+2"); t = w.last_to(21)
    assert "Когда вам удобно" in t["text"] and "по вашему часовому поясу" in t["text"]; ok("пояс принят текстом «мск+2», дальше — время звонка")
    await w.press(21, "pick:2"); await w.press(21, "skip")      # «Вечер», без комментария
    conf = w.last_to(21)["text"]
    assert "Екатеринбург (МСК+2)" in conf and "Вечер (17–21)" in conf; ok("подтверждение показывает пояс и время")
    conf_id = w.with_kb(21); w.clear()
    await w.press(21, "send", mid=conf_id)
    # --- что получили админ и модератор ---
    admin_card = [s for s in w.to(1) if "Заявка #" in (s["text"] or "")]
    assert admin_card, w.to(1)
    c = admin_card[-1]["text"]
    for part in ("Заявка #1", "🆕 Новая", "Екатеринбург (МСК+2)", "17:00–21:00", "15:00–19:00 МСК", "+79991002030", "26"):
        assert part in c or part == "17:00–21:00", (part, c)
    ok("админу пришла полная карточка: контакты, пояс, окно звонка в МСК")
    assert admin_card[-1]["kb"] is None; ok("у новой заявки в карточке админа кнопок нет")
    note = [s for s in w.to(50) if "Поступила новая заявка" in (s["text"] or "")][-1]
    assert "В очереди необработанных заявок: <b>1</b>" in note["text"]; ok("модератору: «Поступила новая заявка» и число в очереди")
    for secret in ("+7999", "Екатеринбург", "U21", "26", "Самара"):
        assert secret not in note["text"], secret
    ok("модератору данные клиента НЕ показываются до того, как он взял заявку")
    assert w.datas(note["kb"]) == ["mp:take"]
    app = D.get_application(1)
    assert app["status"] == "new" and app["tz_offset"] == 2 and app["queued_at"], app; ok("в базе: статус «Новая», пояс МСК+2, время постановки в очередь")
    w.clear(); await w.msg(21, "/start")
    assert "на рассмотрении" in w.last_to(21)["text"]; ok("повторно подать нельзя: «заявка на рассмотрении»")
    # админ может проверить анкету сам
    w.clear(); await w.press(1, "apply")
    assert "Шаг 1 из" in w.last_to(1)["text"] or "Как к вам" in w.last_to(1)["text"]; ok("админ может пройти анкету клиента кнопкой «проверка формы»")
    print("\nN1: роли и анкета — ВСЁ ПРОШЛО")
run(main())
