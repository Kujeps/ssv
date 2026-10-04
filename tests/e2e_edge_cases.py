import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("n6", FORM_VARIANT="full")
from h import ok, run

async def main():
    w = h.World(); B = w.B
    import db as D
    D.init_db()
    # --- админ проходит анкету клиента (проверка формы) ---
    await w.msg(1, "/start"); await w.press(1, "apply"); await w.press(1, "use:name"); await w.msg(1, "+7 999 555-66-77")
    await w.press(1, "skip"); await w.press(1, "skip"); await w.press(1, "skip")
    await w.press(1, "pick:0"); await w.msg(1, "40"); await w.msg(1, "Казань"); await w.press(1, "pick:0")
    await w.press(1, "skip"); await w.press(1, "pick:1"); await w.press(1, "pick:0"); await w.press(1, "skip")
    assert "Проверьте вашу заявку" in w.last_to(1)["text"]; ok("админ может заполнить анкету клиента до конца (состояния формы не перехватываются панелью)")
    await w.press(1, "send")
    assert D.get_application(1)["status"] == "new" and D.get_application(1)["tz_offset"] == 0; ok("заявка из проверки формы попадает в очередь как обычная")
    w.clear(); await w.msg(1, "/start"); assert "Панель администратора" in w.last_to(1)["text"]; ok("/start админа: панель (даже с ожидающей заявкой)")
    # --- /cancel внутри анкеты у админа работает как у клиента ---
    await w.press(1, "apply")
    assert "уже на рассмотрении" in str(w.answers[-1]); ok("админ с ожидающей заявкой тоже не может подать дубль")
    # --- админ как модератор ---
    D.add_moderator(1, "Админ", None, 1)
    await w.msg(1, "/start"); t = w.last_to(1)
    assert "Взять заявку" in str(w.buttons(t["kb"])) and "Все заявки" in str(w.buttons(t["kb"])); ok("админ-модератор: в панели и «Все заявки», и «Взять заявку»")
    await w.press(1, "mp:take"); card = w.last_to(1)
    assert "+79995556677" in card["text"] and "01:00 на обработку" in card["text"]; ok("админ-модератор берёт заявку как обычный модератор")
    app = D.active_lead(1)["id"]
    await w.msg(1, "/start"); t = w.last_to(1); assert f"Моя заявка #{app}" in str(w.buttons(t["kb"])); ok("в панели вместо «Взять» — «Моя заявка»")
    await w.press(1, "ap:arch"); assert "Все заявки</b>: 1" in w.last_to(1)["text"]; ok("админ-модератор видит в архиве ВСЕ заявки (не только свои)")
    # --- модератор после удаления не получает уведомлений ---
    D.add_moderator(51, "Пётр", None, 1); D.remove_moderator(51)
    import notify as N
    w.clear(); await N.notify_moderators_queue(w.bot); assert not w.to(51) and w.to(1); ok("убранному модератору уведомления не приходят")
    # --- уведомление о новой заявке заменяет прошлое ---
    w.clear(); await N.notify_moderators_queue(w.bot)
    assert any(d[0] == 1 for d in w.deleted); ok("прошлое «Поступила заявка» удаляется — в чате одно живое уведомление")
    print("\nN6: пограничные случаи — ВСЁ ПРОШЛО")
run(main())
