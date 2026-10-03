import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("n2")
from h import ok, run
import sqlite3

async def main():
    w = h.World(); B = w.B
    import db as D, notify as N
    from aiogram.types import User
    D.init_db(); D.add_moderator(50, "Иван", "ivan", 1); D.add_moderator(51, "Пётр", "petr", 1)
    async def lead(uid, name, phone):
        i = D.save_application(User(id=uid, is_bot=False, first_name=name, username=f"u{uid}"),
                               {"name": name, "phone": phone, "city": "Омск", "age": "30", "tz": "Москва (МСК)", "call_time": "В любое время"})
        await N.send_lead_cards(w.bot, i); return i
    def admin_card_text(app_id):  # последняя правка карточки админа
        e = [x for x in w.edits if x["chat"] == 1]
        return e[-1]["text"] if e else None

    await w.press(50, "mp:take")
    assert w.answers[-1] == ("Очередь пуста — необработанных заявок нет", True); ok("пустая очередь: модератор получает «очередь пуста»")
    L = [await lead(100 + i, f"Клиент{i}", f"+7999000000{i}") for i in range(1, 7)]
    w.clear()

    # --- взять заявку ---
    await w.press(50, "mp:take")
    card = w.last_to(50); a1 = L[0]
    assert w.answers[-1][0] == "Заявка взята"
    for part in ("Заявка #%d" % a1, "+79990000001", "01:00 на обработку", "будет возвращена в список необработанных"):
        assert part in card["text"], (part, card["text"])
    ok("взял заявку: видит контакты и предупреждение «У вас есть 01:00 на обработку…»")
    assert set(w.datas(card["kb"])) == {f"mv:{a1}:{x}" for x in ("agreed", "refused", "nocall", "junk", "note", "release")}; ok("кнопки: согласился / отказался / недозвонился / мусор / заметка / вернуть")
    t = admin_card_text(a1); assert "В работе" in t and "Иван" in t and "Взята" in t; ok("карточка админа обновилась: в работе у Ивана")
    # --- нельзя взять вторую ---
    w.clear(); await w.press(50, "mp:take")
    assert w.answers[-1][0] == "Сначала завершите текущую заявку" and f"Заявка #{a1}" in w.last_to(50)["text"]; ok("пока не закрыта, вторую не выдают (карточка показывается снова)")
    # --- другой модератор ---
    await w.press(51, "mp:take"); a2 = int(w.last_to(51)["text"].split("#")[1].split("<")[0].split(" ")[0]); assert a2 != a1; ok(f"второй модератор получил другую заявку (#{a2})")
    await w.press(51, f"mv:{a1}:agreed"); assert w.answers[-1] == ("Эта заявка уже не закреплена за вами", True); ok("чужую заявку закрыть нельзя")
    assert D.get_application(a1)["status"] == "work"

    # --- согласился ---
    w.clear(); await w.press(50, f"mv:{a1}:agreed")
    assert D.get_application(a1)["status"] == "agreed"
    assert "✅ Согласился" in admin_card_text(a1); ok("«Согласился»: статус и итог у админа")
    m = w.last_to(50); assert "закрыта" in m["text"] and "mp:take" in w.datas(m["kb"]); ok("модератору сразу предложено «Взять следующую»")
    ed = [x for x in w.edits if x["chat"] == 50][-1]; assert "Согласился" in ed["text"] and "mp:take" in w.datas(ed["kb"]) and f"mv:{a1}:agreed" not in w.datas(ed["kb"]); ok("карточка модератора стала итоговой, кнопки действий убраны")

    # --- отказ: причина обязательна ---
    await w.press(50, "mp:take"); a3 = D.active_lead(50)["id"]
    w.clear(); await w.press(50, f"mv:{a3}:refused")
    assert "Причина отказа" in w.last_to(50)["text"]; ok("«Отказался» требует причину")
    await w.msg(50, "а"); assert "хотя бы пару слов" in w.last_to(50)["text"] and D.get_application(a3)["status"] == "work"; ok("слишком короткая причина не принимается")
    await w.msg(50, "Не хочет служить"); app = D.get_application(a3)
    assert app["status"] == "refused" and app["notes"][-1]["text"] == "Причина: Не хочет служить"; ok("причина сохранена в заметку, заявка закрыта")
    assert "❌ Отказался" in admin_card_text(a3) and "Причина: Не хочет служить" in admin_card_text(a3); ok("у админа видны итог и причина")
    # --- отмена причины ---
    await w.press(50, "mp:take"); a4 = D.active_lead(50)["id"]
    await w.press(50, f"mv:{a4}:junk"); await w.press(50, "mx:cancel", mid=w.with_kb(50))
    assert D.get_application(a4)["status"] == "work"; w.clear(); await w.msg(50, "просто текст")
    assert "Панель модератора" in w.last_to(50)["text"] and D.get_application(a4)["status"] == "work"; ok("отмена причины: заявка осталась в работе, текст не съеден")

    # --- недозвон / перезвонить позже ---
    card_id = [s for s in w.to(50) if f"Заявка #{a4}" in (s["text"] or "")][-1]["id"] if False else None
    w.clear(); await w.press(50, f"mv:{a4}:nocall", mid=w.last[50] if False else None)
    opts = w.markups[-1]["kb"]; ds = w.datas(opts)
    assert ds == [f"mc:{a4}:1h", f"mc:{a4}:3h", f"mc:{a4}:tom", f"mc:{a4}:back"], ds; ok("недозвон: выбор времени (1 час / 3 часа / завтра утром / назад)")
    await w.press(50, f"mc:{a4}:back"); assert f"mv:{a4}:agreed" in w.datas(w.markups[-1]["kb"]); ok("«Назад» возвращает кнопки действий")
    w.clear(); await w.press(50, f"mc:{a4}:1h")
    app = D.get_application(a4)
    assert app["status"] == "nocall" and app["attempts"] == 1 and app["assigned_to"] == 50 and app["callback_at"]; ok("заявка ушла в «Перезвонить позже» и осталась за модератором")
    t = admin_card_text(a4); assert "Недозвонов: <b>1</b>" in t and "Иван" in t; ok("у админа: «Недозвонов: 1», перезвонить во столько-то")
    assert D.active_lead(50) is None; ok("модератор свободен и может брать следующую")
    w.clear(); await w.msg(50, "/start"); p = w.last_to(50)
    assert "Перезвонить позже: <b>1</b>" in p["text"] and "Взять заявку" in str(w.buttons(p["kb"])); ok("панель: «Перезвонить позже: 1»")
    await w.press(50, "mp:cb"); lst = w.last_to(50); assert f"mt:{a4}" in w.datas(lst["kb"]); ok("раздел «Перезвонить» с заявкой")
    await w.press(50, "mp:take"); a5 = D.active_lead(50)["id"]
    w.clear(); await w.press(50, f"mt:{a4}", mid=w.with_kb(50)); assert w.answers[-1][0] == "Сначала завершите текущую заявку"; ok("перезвон нельзя взять, пока в работе другая заявка")
    await w.press(50, f"mv:{a5}:agreed")
    w.clear(); await w.press(50, f"mt:{a4}")
    assert D.get_application(a4)["status"] == "work" and "01:00 на обработку" in w.last_to(50)["text"]; ok("после закрытия текущей перезвон берётся в работу с новым таймером")

    # --- возврат в очередь с причиной ---
    q_before = D.queue_count()
    w.clear(); await w.press(50, f"mv:{a4}:release")
    assert "Причина возврата" in w.last_to(50)["text"]
    await w.msg(50, "не успеваю"); app = D.get_application(a4)
    assert app["status"] == "new" and app["assigned_to"] is None and D.queue_count() == q_before + 1; ok("возврат: заявка снова в очереди, причина записана")
    assert "Возврат в очередь: не успеваю" in app["notes"][-1]["text"]
    mod_edit = [x for x in w.edits if x["chat"] == 50][-1]
    assert "возвращена в очередь" in mod_edit["text"] and "+7999" not in mod_edit["text"]; ok("карточка у модератора превратилась в заглушку без данных клиента")
    note = [s for s in w.to(51) if "вернулась в очередь" in (s["text"] or "")]; assert note; ok("модераторам: «Заявка вернулась в очередь»")
    # --- заметка ---
    await w.press(50, "mp:take"); cur = D.active_lead(50)["id"]
    w.clear(); await w.press(50, f"mv:{cur}:note"); await w.msg(50, "Просил перезвонить после 18")
    assert "Заметка сохранена" in w.last_to(50)["text"] and "Просил перезвонить после 18" in admin_card_text(cur); ok("заметка к заявке: видна админу")
    # --- роли ---
    w.clear(); await w.press(21, "mp:take"); assert w.answers[-1][0] and "неактуальна" in w.answers[-1][0]; ok("клиент не может нажать кнопки модератора")
    hist = [(x["kind"]) for x in D.get_history(a4)]; assert hist.count("take") == 2 and "nocall" in hist and "release" in hist and "take_cb" in hist; ok("история: " + ", ".join(hist))
    print("\nN2: модератор — ВСЁ ПРОШЛО")
run(main())
