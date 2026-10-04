import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("inprog")
from h import ok, run
import sqlite3

async def main():
    w = h.World(); B = w.B
    import db as D, notify as N, jobs as J, config as C
    from aiogram.types import User
    D.init_db(); D.add_moderator(50, "Иван", None, 1)
    con = lambda: sqlite3.connect(path)
    def sql(q, *a):
        c = con(); c.execute(q, a); c.commit(); c.close()
    async def lead(uid, name):
        i = D.save_application(User(id=uid, is_bot=False, first_name=name, username=f"c{uid}"), {"name": name, "phone": f"+7999{uid}", "tz": "Москва (МСК)", "call_time": "В любое время"})
        await N.send_lead_cards(w.bot, i); return i
    a = await lead(101, "Клиент1"); b = await lead(102, "Клиент2")

    assert C.STATUSES["work"][1] == "Взята" and C.STATUSES["reached"] == ("⏳", "В работе"); ok("названия: «Взята» (час на заявку) и «⏳ В работе» (долгая обработка)")
    await w.press(50, "mp:take"); card = w.last_to(50)
    assert "Взята" in card["text"] and "В работе" not in card["text"].split("\n")[0], card["text"]; ok("только что взятая заявка называется «Взята»")
    assert f"mv:{a}:reached" in w.datas(card["kb"]) and "⏳ В работе" in w.buttons(card["kb"]); ok("под ней кнопка «⏳ В работе»")

    w.clear(); await w.press(50, f"mv:{a}:reached", mid=w.with_kb(50) if False else None)
    app = D.get_application(a)
    assert app["status"] == "reached" and app["assigned_to"] == 50 and app["taken_at"] is None; ok("кнопка: статус «В работе», заявка осталась у модератора, таймера нет")
    assert D.active_lead(50) is None; ok("модератор свободен — может брать следующую")
    m = w.last_to(50); assert "в работе" in m["text"] and "без таймера" in m["text"] and "mp:take" in w.datas(m["kb"]); ok("модератору: «остаётся у вас без таймера… можно взять следующую»")
    t = [x for x in w.edits if x["chat"] == 1][-1]["text"]; assert "⏳ В работе" in t and "Иван" in t and "без таймера" in t; ok("у админа в карточке: «⏳ В работе», модератор Иван")
    ed = [x for x in w.edits if x["chat"] == 50][-1]; assert "В работе" in ed["text"] and f"mt:{a}" in w.datas(ed["kb"]); ok("карточка модератора: «В работе» и кнопки для ведения заявки")

    # часы идут, а заявка остаётся у модератора
    sql("UPDATE applications SET taken_at = datetime('now','-30 hours'), status_at = datetime('now','-30 hours') WHERE id=?", a)
    w.clear(); await J.run_maintenance(w.bot)
    assert D.get_application(a)["status"] == "reached" and not [s for s in w.to(50) if "вернулась" in (s["text"] or "") or "Время на заявку" in (s["text"] or "")]; ok("через 30 часов заявку «В работе» ничто не возвращает в очередь")

    # следующую взять можно, это независимая заявка
    await w.press(50, "mp:take"); assert D.active_lead(50)["id"] == b; ok("вторая заявка выдана, первая продолжает «В работе»")
    # в таблице
    w.clear(); await w.msg(50, "/table"); t = w.last_to(50)["text"]
    assert f"#{a}" in t and "⏳ В работе" in t and "🔧 Взята" in t; ok("в таблице обе: «🔧 Взята» и «⏳ В работе»")
    # решение клиента: согласие позже
    await w.press(50, f"tb:e:{a}", mid=w.with_kb(50)); await w.press(50, f"tb:s:{a}", mid=w.with_kb(50))
    assert f"tb:st:{a}:agreed" in w.datas(w.markups[-1]["kb"]) and f"tb:st:{a}:reached" not in w.datas(w.markups[-1]["kb"]); ok("в меню статусов «В работе» нет, пока он уже стоит; доступны итоги")
    await w.press(50, f"tb:st:{a}:agreed", mid=w.with_kb(50))
    assert D.get_application(a)["status"] == "agreed"; ok("через время клиент согласился → «Согласился»")
    hist = [h_["kind"] for h_ in D.get_history(a)]; assert hist == ["take", "reached", "final"], hist; ok("история: " + ", ".join(hist))
    # из «Взята» можно вернуться в «В работе» через таблицу
    await w.press(50, f"mv:{b}:agreed")      # закрываем вторую
    # админская панель
    D.save_application(User(id=999, is_bot=False, first_name="X"), {"name": "X", "phone": "+7999"})
    sql("UPDATE applications SET status='reached', assigned_to=50, assigned_name='Иван' WHERE id=?", D.get_application(3)["id"])
    w.clear(); await w.msg(1, "/start"); p = w.last_to(1)["text"]
    assert "🔧 Взято:" in p and "⏳ В работе: <b>1</b>" in p and "🔁 Перезвонить:" in p; ok("панель админа: «Взято», «⏳ В работе», «Перезвонить» раздельно")
    # архив: фильтр
    await w.press(1, "ap:arch"); lid = w.with_kb(1); await w.press(1, "ar:f:status", mid=lid)
    labels = w.buttons(w.edits[-1]["kb"]); assert "🔧 Взята" in labels and "⏳ В работе" in labels; ok("фильтр архива: «Взята» и «В работе» — разные")
    await w.press(1, "ar:s:reached", mid=lid); assert "Все заявки</b>: 1" in w.edits[-1]["text"] and "статус — В работе" in w.edits[-1]["text"]; ok("фильтр «В работе»: находит долгие заявки")
    # клиент
    w.clear(); await w.msg(999, "/start"); assert "уже в работе" in w.last_to(999)["text"]; ok("клиент видит «Ваша заявка уже в работе», повторно подать нельзя")
    # статистика
    w.clear(); await w.msg(1, "/stats"); st = w.last_to(1)["text"]; assert "⏳ В работе: <b>1</b>" in st and "🔧 Взята:" in st; ok("/stats: отдельные строки «Взята» и «В работе»")
    print("\nВ РАБОТЕ — ВСЁ ПРОШЛО")
run(main())
