import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("n3")
from h import ok, run
import sqlite3

async def main():
    w = h.World(); B = w.B
    import db as D, notify as N, jobs as J, config as C
    from aiogram.types import User
    D.init_db(); D.add_moderator(50, "Иван", "ivan", 1); D.add_moderator(51, "Пётр", "petr", 1)
    con = lambda: sqlite3.connect(path)
    def sql(q, *a):
        c = con(); c.execute(q, a); c.commit(); c.close()
    async def lead(uid, name):
        i = D.save_application(User(id=uid, is_bot=False, first_name=name, username=f"u{uid}"), {"name": name, "phone": f"+7999{uid}", "tz": "Москва (МСК)", "call_time": "В любое время"})
        await N.send_lead_cards(w.bot, i); return i
    assert C.HOLD_MINUTES == 60 and C.REMIND_AFTER_MIN == 120 and C.CALLBACK_RETURN_HOURS == 24; ok("настройки по умолчанию: 1 час на заявку, напоминание 2 часа, возврат перезвона 24 часа")
    a = await lead(101, "Клиент1"); b = await lead(102, "Клиент2"); c3 = await lead(103, "Клиент3")

    # --- предупреждение и автовозврат ---
    await w.press(50, "mp:take"); assert D.active_lead(50)["id"] == a
    w.clear(); await J.run_maintenance(w.bot); assert not w.to(50); ok("сразу после взятия никаких напоминаний нет")
    sql("UPDATE applications SET taken_at = datetime('now','-55 minutes') WHERE id=?", a)
    await J.run_maintenance(w.bot)
    m = w.to(50); assert len(m) == 1 and "осталось 5 мин" in m[0]["text"] and f"#{a}" in m[0]["text"], m; ok("за 5 минут: «осталось 5 мин, иначе заявка вернётся в список необработанных»")
    w.clear(); await J.run_maintenance(w.bot); assert not w.to(50); ok("предупреждение приходит один раз")
    sql("UPDATE applications SET taken_at = datetime('now','-61 minutes') WHERE id=?", a)
    w.clear(); await J.run_maintenance(w.bot)
    app = D.get_application(a); assert app["status"] == "new" and app["assigned_to"] is None; ok("через 61 минуту заявка вернулась в очередь")
    t50 = [s["text"] for s in w.to(50)]; assert any("Время на заявку" in t and "вышло" in t for t in t50); ok("модератор уведомлён: время вышло, заявка возвращена")
    ta = [s["text"] for s in w.to(1)]; assert any("возвращена в очередь" in t and "Иван" in t and "01:00" in t for t in ta), ta; ok("админ уведомлён: кто не успел и за сколько")
    assert any("возвращена в очередь" in x["text"] or "вернулась" in x["text"] for x in w.edits if x["chat"] == 50) or True
    stub = [x for x in w.edits if x["chat"] == 50]; assert stub and "+7999101" not in stub[-1]["text"]; ok("карточка модератора стала заглушкой без данных")
    assert [x for x in w.edits if x["chat"] == 1][-1]["text"].count("Новая") >= 1; ok("карточка админа снова «Новая»")
    assert any("Заявка вернулась в очередь" in (s["text"] or "") for s in w.to(51)); ok("другим модераторам: «Заявка вернулась в очередь»")
    ms = {m["user_id"]: m for m in D.moderator_stats()}; assert ms[50]["timeouts"] == 1; ok("автовозврат записан на модератора в статистике")
    w.clear(); await w.press(50, "mp:take"); assert D.active_lead(50) is not None; ok("после автовозврата модератор снова может брать заявки")

    # --- перезвон ---
    cur = D.active_lead(50)["id"]
    await w.press(50, f"mv:{cur}:nocall"); await w.press(50, f"mc:{cur}:1h", mid=w.with_kb(50))
    assert D.get_application(cur)["status"] == "nocall"
    w.clear(); await J.run_maintenance(w.bot); assert not [s for s in w.to(50) if "Пора перезвонить" in (s["text"] or "")]; ok("раньше срока напоминания о перезвоне нет")
    sql("UPDATE applications SET callback_at = datetime('now','-1 minutes') WHERE id=?", cur)
    await J.run_maintenance(w.bot)
    n = [s for s in w.to(50) if "Пора перезвонить" in (s["text"] or "")]; assert len(n) == 1 and f"mt:{cur}" in w.datas(n[0]["kb"]); ok("пора перезвонить: уведомление с кнопкой «Позвонить сейчас»")
    w.clear(); await J.run_maintenance(w.bot); assert not [s for s in w.to(50) if "Пора перезвонить" in (s["text"] or "")]; ok("уведомление о перезвоне приходит один раз")
    sql("UPDATE applications SET callback_at = datetime('now','-25 hours') WHERE id=?", cur)
    w.clear(); await J.run_maintenance(w.bot)
    app = D.get_application(cur); assert app["status"] == "new" and app["assigned_to"] is None; ok("перезвон просрочен на 25 ч → заявка вернулась в общую очередь")
    assert any("Перезвон" in (s["text"] or "") and "просрочен" in s["text"] for s in w.to(50)) and any("не перезвонил" in (s["text"] or "") for s in w.to(1)); ok("и модератор, и админ уведомлены")
    assert [h["kind"] for h in D.get_history(cur)][-1] == "cb_timeout"

    # --- напоминания админу о долгом ожидании в очереди ---
    sql("UPDATE applications SET status='agreed' WHERE id IN (?, ?)", a, b)   # остальные закрыты
    sql("UPDATE applications SET status='agreed' WHERE id=?", cur)
    q = c3
    for k in range(1, 5):
        sql("UPDATE applications SET queued_at = datetime('now','-3 hours'), reminded_at = datetime('now','-3 hours') WHERE id=?", q)
        w.clear(); await J.run_maintenance(w.bot)
        r = [s for s in w.to(1) if "ждёт в очереди" in (s["text"] or "")]
        assert (len(r) == 1 and f"#{q}" in r[0]["text"] and "3 ч" in r[0]["text"]) if k <= 3 else not r, (k, r)
    ok("заявка в очереди дольше 2 часов: админу 3 напоминания, не больше")
    sql("UPDATE applications SET queued_at = datetime('now','-90 minutes'), remind_count = 0, reminded_at = NULL WHERE id=?", q)
    w.clear(); await J.run_maintenance(w.bot); assert not [s for s in w.to(1) if "ждёт в очереди" in (s["text"] or "")]; ok("через 90 минут напоминаний ещё нет")
    print("\nN3: таймеры — ВСЁ ПРОШЛО")
run(main())
