import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("transfer", TYPING_MAX_SECONDS="0")
from h import ok, run
import sqlite3

G = -100700

async def main():
    w = h.World(); B = w.B
    import db as D, relay as R
    R.is_work_time = lambda now=None: True
    D.init_db()
    con = lambda: sqlite3.connect(path)

    async def lead(uid):
        await w.msg(uid, "/start"); await w.press(uid, "qm:call"); await w.msg(uid, f"+7 900 000-00-{uid % 100:02d}")
        return D.latest_application(uid)["id"]

    # ---------- админ берёт заявки сам ----------
    await w.msg(1, "/start"); kb = w.datas(w.last_to(1)["kb"])
    assert "ap:self" in kb and "mp:take" not in kb; ok("в панели админа кнопка «Брать заявки самому», взять пока нельзя")
    a1 = await lead(21)
    w.clear(); await w.press(1, "ap:self")
    assert D.is_moderator(1); kb = w.datas(w.last_to(1)["kb"])
    assert "mp:take" in kb and "tb:open" in kb and "mp:cb" in kb and "nm:open" in kb; ok("включил — в панели «Взять заявку», «Моя таблица», «В ожидании», имя для клиентов")
    assert "👷 Беру заявки сам: ВКЛ" in w.buttons(w.last_to(1)["kb"]); ok("кнопка показывает, что режим включён")
    w.clear(); await w.press(1, "mp:take")
    app = D.get_application(a1); assert app["assigned_to"] == 1 and app["status"] == "work"; ok("админ взял заявку в работу")
    assert any("Заявка #" in (s["text"] or "") and "mv:" in str(s["kb"]) for s in w.to(1)); ok("админ получил карточку модератора с кнопками")
    # выключение при заявке в работе — с подтверждением
    w.clear(); await w.press(1, "ap:self")
    assert D.is_moderator(1) and "вернутся в очередь" in w.last_to(1)["text"]; ok("выключить при заявке в работе: бот предупреждает, что заявка вернётся в очередь")

    # ---------- передача заявки ----------
    D.add_moderator(50, "Иван", None, 1); D.add_moderator(51, "Пётр", None, 1)
    a2 = await lead(22)
    await w.press(50, "mp:take"); assert D.get_application(a2)["assigned_to"] == 50
    card50 = [s for s in w.to(50) if f"Заявка #{a2}" in (s["text"] or "") and s["kb"]][-1]
    assert f"tr:{a2}" in w.datas(card50["kb"]); ok("в карточке модератора кнопка «🔀 Передать другому менеджеру»")
    w.clear(); await w.press(52, f"tr:{a2}"); assert not w.to(52); ok("посторонний передать не может")
    w.clear(); await w.press(50, f"tr:{a2}", mid=card50["id"])
    opts = w.datas(w.last_to(50)["kb"]); assert opts == [f"tr:{a2}:1", f"tr:{a2}:51", "tr:x"], opts; ok("выбор получателя: все модераторы, кроме себя (включая админа)")
    w.clear(); await w.press(50, f"tr:{a2}:51")
    e = w.edits[-1]; assert "Пётр" in e["text"] and "что уже выяснили" in e["text"] and f"trg:{a2}:51" in w.datas(e["kb"]); ok("после выбора: просьба написать, что уже выяснили, или «Передать без комментария»")
    w.clear(); await w.msg(50, "Служил в ВДВ, хочет в штурмовое, нужен подробный разговор про выплаты")
    app = D.get_application(a2)
    assert app["assigned_to"] == 51 and app["assigned_name"] == "Пётр" and app["status"] == "reached"; ok("заявка у Петра, статус «В работе» (без часового таймера)")
    assert "передана" in w.last_to(50)["text"] and "mp:take" in w.datas(w.last_to(50)["kb"]); ok("передавшему: «Заявка передана» и кнопка «Взять следующую»")
    got = [s["text"] for s in w.to(51)]
    assert any("Вам передана заявка" in t and "штурмовое" in t for t in got); ok("получатель: «Вам передана заявка» и что уже выяснили")
    c51 = [s for s in w.to(51) if f"Заявка #{a2}" in (s["text"] or "")][-1]
    assert "+7900000002" in c51["text"].replace(" ", "") or "+79000000022" in c51["text"]; assert "mv:" in str(c51["kb"]) or "mt:" in str(c51["kb"]); ok("получатель сразу видит полную карточку с кнопками")
    stub = [x for x in w.edits if x["chat"] == 50]; assert stub and "больше не закреплена" in stub[-1]["text"]; ok("у передавшего карточка стала заглушкой без данных клиента")
    assert any("Заявка #%d" % a2 in (s["text"] or "") and "Иван → Пётр" in s["text"] for s in w.to(1)); ok("админу — уведомление о передаче")
    notes = [n["text"] for n in D.get_application(a2)["notes"]]; assert any("Передал → Пётр: Служил в ВДВ" in n for n in notes); ok("в заметках заявки: кто кому передал и комментарий")
    hist = con().execute("SELECT kind, by_id, holder_id FROM status_history WHERE app_id=? ORDER BY id DESC LIMIT 1", (a2,)).fetchone()
    assert hist == ("transfer", 50, 51); ok("в истории заявки: передача")
    await w.press(50, f"mv:{a2}:agreed"); assert D.get_application(a2)["status"] == "reached"; ok("старые кнопки у передавшего больше не работают")
    a3 = await lead(23); await w.press(51, "mp:take")
    assert D.get_application(a3)["assigned_to"] == 51; ok("получатель может брать новые заявки из очереди (переданная не блокирует)")
    await w.press(51, f"mv:{a3}:reached")

    # без комментария; «Не дозвонились» сохраняет время перезвона
    await w.press(51, f"mt:{a2}")  # Пётр берёт переданную заявку в работу
    await w.press(51, f"mv:{a2}:nocall"); await w.press(51, f"mc:{a2}:3h")
    cb_at = D.get_application(a2)["callback_at"]; assert D.get_application(a2)["status"] == "nocall"
    w.clear(); await w.press(51, f"tr:{a2}"); await w.press(51, f"tr:{a2}:50"); await w.press(51, f"trg:{a2}:50")
    app = D.get_application(a2); assert app["assigned_to"] == 50 and app["status"] == "nocall" and app["callback_at"] == cb_at; ok("«Передать без комментария»; «Не дозвонились» и время перезвона сохраняются")

    # админ передаёт чужую заявку из своей карточки
    from ui import lead_keyboard
    assert f"tr:{a3}" in w.datas(lead_keyboard(D.get_application(a3), "admin")); ok("в карточке админа у взятой заявки есть «🔀 Передать»")
    adm_mid = [m for c, m, v in D.lead_message_refs(a3) if c == 1][0]
    w.clear(); await w.press(1, f"tr:{a3}", mid=adm_mid); await w.press(1, f"tr:{a3}:50"); await w.press(1, f"trg:{a3}:50")
    assert D.get_application(a3)["assigned_to"] == 50; ok("админ передал заявку Петра Ивану")
    assert any("администратор передал" in (s["text"] or "") for s in w.to(51)); ok("прежнему менеджеру — сообщение, что заявку передал админ")

    # ---------- передача в теме группы ----------
    await w.gmsg(1, G, None, "/relay_here")
    a4 = await lead(24); await w.press(1, "mv:%d:reached" % a1); await w.press(50, f"mv:{a3}:reached")
    await w.press(50, "mp:take"); assert D.get_application(a4)["assigned_to"] == 50
    card = [s for s in w.sent if s["chat"] == G and f"Заявка #{a4}" in (s["text"] or "") and s["kb"]][-1]; th = card["thread"]
    assert f"rt:{a4}:tr" in w.datas(card["kb"]); ok("в теме под карточкой кнопка «🔀 Передать другому менеджеру»")
    w.clear(); await w.gpress(50, G, th, f"rt:{a4}:tr", card["id"])
    assert w.datas(w.markups[-1]["kb"]) == [f"rt:{a4}:to:1", f"rt:{a4}:to:51", f"rt:{a4}:back"]; ok("в теме — выбор получателя")
    await w.gpress(50, G, th, f"rt:{a4}:to:51", card["id"])
    assert "Пётр" in w.last_to(G)["text"] and w.last_to(G)["thread"] == th; ok("в теме — просьба написать комментарий")
    w.clear(); await w.gmsg(50, G, th, "Нужен разговор про контракт на год")
    assert not w.to(24); ok("комментарий к передаче клиенту не уходит")
    assert D.get_application(a4)["assigned_to"] == 51; ok("заявка передана Петру из темы")
    assert any("Заявка передана" in (s["text"] or "") and "Пётр" in s["text"] for s in w.to(G) if s["thread"] == th); ok("в теме запись о передаче")
    assert not D.relay_topic(a4)["closed"]; ok("тема остаётся открытой — вся переписка сохраняется")
    w.clear(); await w.gmsg(50, G, th, "это пишет Иван"); assert not w.to(24); ok("прежний менеджер клиенту больше не пишет")
    await w.gmsg(51, G, th, "Здравствуйте, я Пётр"); assert w.last_to(24)["text"].startswith("<b>Менеджер Пётр:</b>"); ok("новый менеджер пишет клиенту в той же теме")

    # ---------- передавать некому ----------
    await w.press(1, "ap:selfoff")
    assert not D.is_moderator(1) and D.get_application(a1)["status"] == "new"; ok("админ перестал брать заявки: его заявки вернулись в очередь")
    await w.msg(1, "/start"); assert "ap:self" in w.datas(w.last_to(1)["kb"]) and "mp:take" not in w.datas(w.last_to(1)["kb"]); ok("в панели снова «Брать заявки самому»")
    D.remove_moderator(51)
    w.clear(); await w.press(50, f"tr:{a2}")
    assert w.answers and "некому" in w.answers[-1][0]; ok("если других модераторов нет — «Передать некому»")
    print("\nПЕРЕДАЧА ЗАЯВОК И АДМИН-МЕНЕДЖЕР — ВСЁ ПРОШЛО")
run(main())
