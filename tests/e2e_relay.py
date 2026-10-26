import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("relay", TYPING_MAX_SECONDS="0")
from h import ok, run
import sqlite3

G = -100500   # группа с темами

async def main():
    w = h.World(); B = w.B
    import db as D, relay as R
    D.init_db(); D.add_moderator(50, "Иван", None, 1); D.add_moderator(51, "Пётр", None, 1)
    con = lambda: sqlite3.connect(path)
    calls = lambda name: [m for n, m in w.calls if n == name]
    R.is_work_time = lambda now=None: True   # результат не зависит от времени запуска теста

    # ---------- подключение группы ----------
    assert D.relay_chat_id() is None; ok("по умолчанию группа переписки не подключена")
    await w.gmsg(99, G, None, "/relay_here"); assert D.relay_chat_id() is None; ok("/relay_here от не-админа игнорируется")
    await w.msg(1, "/relay_here"); assert "в группе" in w.last_to(1)["text"] and D.relay_chat_id() is None; ok("/relay_here в личке: подсказка, как подключить")
    await w.feed(message={"message_id": 5, "date": 0, "chat": {"id": G, "type": "supergroup", "is_forum": False, "title": "g"}, "from": w.user(1), "text": "/relay_here"})
    assert D.relay_chat_id() is None and "не включены темы" in w.last_to(G)["text"]; ok("группа без включённых тем отклоняется с пояснением")
    await w.gmsg(1, G, None, "/relay_here"); assert D.relay_chat_id() == G; ok("/relay_here админа в группе с темами — группа подключена")
    await w.msg(1, "/start"); assert "переписка через группу: <b>подключена</b>" in w.last_to(1)["text"]; ok("в панели админа видно, что группа подключена")

    # ---------- подключение не создаёт темы для старых заявок разом ----------
    # (заявка #0: взята до подключения группы)
    w.calls.clear()
    assert not calls("CreateForumTopic"); ok("подключение группы не создаёт тем (нет всплеска запросов к Telegram)")

    # ---------- заявка и вложение клиента до взятия ----------
    await w.msg(21, "/start"); await w.press(21, "qm:call"); await w.msg(21, "+7 999 123-45-67")
    await w.media(21, "photo", "мой паспорт")
    row = con().execute("SELECT kind, text, group_msg_id FROM relay_log WHERE app_id=1").fetchone()
    assert row[0] == "photo" and row[1] == "мой паспорт" and row[2] is None; ok("вложение клиента из очереди записано в журнал (ничего не теряется)")
    assert not calls("CreateForumTopic"); ok("пока заявку не взяли, темы нет")

    # ---------- менеджер берёт заявку: появляется тема ----------
    w.clear(); w.flood = 1; await w.press(50, "mp:take")   # Telegram просит подождать
    assert D.get_application(1)["status"] == "work" and D.relay_topic(1) is None; ok("лимит Telegram на создание тем: бот не падает и не зависает, заявка взята")
    assert not any("Не получилось создать" in (s["text"] or "") for s in w.to(1)); ok("админам из-за лимита тревоги нет")
    assert D.taken_without_topic() == [1]; ok("заявка без темы поставлена в очередь на повторную попытку")
    assert R._flood_until > 0; R._flood_until = 0   # «время ожидания прошло»
    await R.retry_topics(w.bot); assert D.relay_topic(1) is not None; ok("фоновая проверка создала тему, когда лимит снялся")
    t = calls("CreateForumTopic"); assert len(t) == 1 and t[0].name.startswith("#1 U21"), t; ok("взял заявку → создана тема «#1 U21»")
    card = [s for s in w.sent if s["chat"] == G and s["thread"] and "Заявка #1" in (s["text"] or "")][0]
    assert "+79991234567" in card["text"] and "Менеджер Иван" in card["text"] and "Имя для клиентов" in card["text"]; ok("первое сообщение темы — карточка клиента и подсказка про подпись и кнопку смены имени")
    assert w.datas(card["kb"]) == ["rt:1:agreed", "rt:1:refused", "rt:1:reached", "rt:1:nocall", "rt:1:junk", "rt:1:release", "rt:1:tr"]; ok("под карточкой кнопки статусов и «Передать»")
    assert calls("PinChatMessage"); ok("карточка закреплена в теме")
    cp = calls("CopyMessage"); assert len(cp) == 1 and cp[0].chat_id == G and "мой паспорт" in cp[0].caption and "👤" in cp[0].caption; ok("вложение, присланное до взятия, перенесено в тему с пометкой клиента")
    assert any("до того, как заявку взяли" in (s["text"] or "") for s in w.sent if s["chat"] == G); ok("с пояснением, что это было до взятия")
    thread = card["thread"]
    topic = D.relay_topic(1); assert topic["thread_id"] == thread and topic["chat_id"] == G and not topic["closed"]; ok("тема записана в базу")
    assert D.relay_log_pending(1) == []; ok("журнал: всё из очереди доставлено в тему")

    # ---------- менеджер -> клиент ----------
    w.clear(); m1 = await w.gmsg(50, G, thread, "Здравствуйте! Удобно сейчас говорить?")
    out = w.last_to(21); assert out["text"] == "<b>Менеджер Иван:</b>\nЗдравствуйте! Удобно сейчас говорить?"; ok("текст менеджера уходит клиенту: «Менеджер Иван: …»")
    assert calls("SetMessageReaction"); ok("менеджер видит реакцию — доставлено")
    await w.msg(50, "/start"); assert "nm:open" in w.datas(w.last_to(50)["kb"]); ok("в панели менеджера есть кнопка «Имя для клиентов»")
    await w.press(50, "nm:open"); t = w.last_to(50)["text"]
    assert "Менеджер Иван" in t and "Так выглядит ваше сообщение у клиента" in t and "Здравствуйте! Это по поводу вашей заявки" in t; ok("кнопка показывает текущее имя и предпросмотр: как сообщение выглядит у клиента")
    await w.msg(50, "x" * 40); assert "от 1 до 30" in w.last_to(50)["text"]; ok("слишком длинное имя отклоняется")
    await w.msg(50, "Менеджер Дмитрий"); t = w.last_to(50)["text"]
    assert "Имя сохранено" in t and "<b>Менеджер Дмитрий:</b>\nЗдравствуйте!" in t and "nm:open" in w.datas(w.last_to(50)["kb"]); ok("новое имя сохранено (лишнее «Менеджер» убрано), сразу виден предпросмотр")
    assert D.get_setting("mgr_name:50") == "Дмитрий"; ok("в базе: «Дмитрий»")
    await w.msg(50, "/name"); assert "Менеджер Дмитрий" in w.last_to(50)["text"]; ok("/name без имени тоже показывает предпросмотр")
    await w.press(1, "nm:open"); await w.msg(1, "Алексей"); assert D.get_setting("mgr_name:1") == "Алексей"; ok("админ (если сам отвечает клиентам) тоже задаёт имя кнопкой")
    await w.press(21, "nm:open"); assert not [s for s in w.to(21) if "Так выглядит" in (s["text"] or "")]; ok("клиент кнопкой имени воспользоваться не может")
    w.clear(); await w.gmsg(50, G, thread, "Дмитрий на связи")
    assert w.last_to(21)["text"].startswith("<b>Менеджер Дмитрий:</b>"); ok("теперь подпись «Менеджер Дмитрий»")
    w.calls.clear(); await w.gmsg(50, G, thread, photo=[{"file_id": "p", "file_unique_id": "u", "width": 1, "height": 1}], caption="Вот список документов")
    cp = calls("CopyMessage"); assert cp[0].chat_id == 21 and cp[0].caption == "<b>Менеджер Дмитрий:</b>\nВот список документов"; ok("фото менеджера → клиенту с подписью «Менеджер Дмитрий: …»")
    w.calls.clear(); w.clear(); await w.gmsg(50, G, thread, sticker={"file_id": "s", "file_unique_id": "u", "type": "regular", "width": 1, "height": 1, "is_animated": False, "is_video": False})
    assert any("Менеджер Дмитрий:" in (s["text"] or "") and "стикер" in s["text"] for s in w.to(21)) and calls("CopyMessage"); ok("стикер: сначала метка «Менеджер Дмитрий: (стикер)», затем сам стикер")
    out_rows = con().execute("SELECT direction, kind, by_name, client_msg_id FROM relay_log WHERE direction='out'").fetchall()
    assert len(out_rows) == 4 and all(r[3] for r in out_rows) and out_rows[0][2] == "Иван" and out_rows[1][2] == "Дмитрий"; ok("все исходящие записаны в журнал вместе с номерами сообщений")

    # заметка для себя
    w.clear(); await w.gmsg(50, G, thread, "# перезвонить после обеда, просил в 15:00")
    assert not w.to(21); ok("сообщение с # клиенту не уходит")
    assert any("перезвонить после обеда" in n["text"] for n in D.get_application(1)["notes"]); ok("…а сохраняется заметкой в заявке")

    # редактирование
    w.clear(); await w.gmsg(50, G, thread, "Здравствуйте! Удобно сейчас?", edit=True, message_id=m1)
    e = [x for x in w.edits if x["chat"] == 21]; assert e and "Удобно сейчас?" in e[-1]["text"] and e[-1]["text"].startswith("<b>Менеджер Иван:</b>"); ok("правка сообщения в теме правит его и у клиента")

    # ---------- клиент -> тема ----------
    w.clear(); await w.msg(21, "Да, удобно, слушаю")
    got = [s for s in w.to(G) if s["thread"] == thread]; assert got and got[-1]["text"] == "👤 <b>U21:</b>\nДа, удобно, слушаю"; ok("ответ клиента появляется в теме: «👤 U21: …»")
    assert not [s for s in w.to(21) if "Спасибо" in (s["text"] or "")]; ok("клиент не получает лишних ответов бота")
    w.calls.clear(); await w.media(21, "voice")
    cp = calls("CopyMessage"); assert cp and cp[0].chat_id == G and cp[0].message_thread_id == thread; ok("голосовое клиента → в тему")
    await w.media(21, "sticker"); assert len(calls("CopyMessage")) == 2; ok("стикер клиента → в тему")
    n_in = con().execute("SELECT COUNT(*) FROM relay_log WHERE direction='in' AND group_msg_id IS NOT NULL").fetchone()[0]
    assert n_in >= 4; ok("все входящие записаны в журнал и доставлены в тему (%d)" % n_in)

    # ---------- автоответ ночью ----------
    R.is_work_time = lambda now=None: False
    w.clear(); await w.msg(21, "А во сколько вы работаете?")
    assert any("нерабочее время" in (s["text"] or "") and "9:00" in s["text"] for s in w.to(21)); ok("ночью клиент получает автоответ: «ответим утром, после 9:00 по Москве»")
    assert [s for s in w.to(G) if "работаете" in (s["text"] or "")]; ok("а сообщение всё равно попало в тему")
    w.clear(); await w.msg(21, "Алло?")
    assert not [s for s in w.to(21) if "нерабочее" in (s["text"] or "")]; ok("автоответ не повторяется на каждое сообщение")
    R.is_work_time = lambda now=None: True

    # ---------- права ----------
    w.clear(); await w.gmsg(99, G, thread, "я посторонний"); assert not w.to(21); ok("посторонний в теме: клиенту ничего не уходит")
    w.clear(); await w.gmsg(51, G, thread, "пишет другой менеджер")
    assert not [s for s in w.to(21) if "другой" in (s["text"] or "")] and "в работе у другого менеджера" in w.last_to(G)["text"]; ok("менеджер чужой заявки: клиенту не уходит, в теме пояснение")
    w.clear(); await w.gpress(51, G, thread, "rt:1:agreed", card_mid := card["id"]); assert D.get_application(1)["status"] == "work"; ok("кнопки статусов чужой заявки не срабатывают")

    # ---------- кнопки статусов в теме ----------
    w.clear(); await w.gpress(50, G, thread, "rt:1:reached", card["id"])
    assert D.get_application(1)["status"] == "reached" and not D.relay_topic(1)["closed"]; ok("«В работе» из темы: статус сменился, тема остаётся открытой")
    await w.gpress(50, G, thread, "rt:1:nocall", card["id"])
    assert w.datas(w.markups[-1]["kb"]) == ["rt:1:c:1h", "rt:1:c:3h", "rt:1:c:tom", "rt:1:back"]; ok("«Не дозвонился»: варианты времени перезвона")
    await w.gpress(50, G, thread, "rt:1:back", card["id"])
    await w.gpress(50, G, thread, "rt:1:nocall", card["id"]); await w.gpress(50, G, thread, "rt:1:c:3h", card["id"])
    a = D.get_application(1); assert a["status"] == "nocall" and a["attempts"] == 1 and a["callback_at"]; ok("перезвон через 3 часа выбран кнопкой — заявка в «Не дозвонились»")
    assert not D.relay_topic(1)["closed"]; ok("тема остаётся открытой: клиент может ответить")
    w.calls.clear(); w.clear(); await w.gpress(50, G, thread, "rt:1:refused", card["id"])
    assert "Причина отказа" in w.last_to(G)["text"] and D.get_application(1)["status"] == "nocall"; ok("«Отказался»: бот просит причину, статус пока прежний")
    await w.gmsg(50, G, thread, "нашёл другую работу")
    a = D.get_application(1); assert a["status"] == "refused"; ok("причина написана в теме → статус «Отказался»")
    assert not [s for s in w.to(21) if "другую работу" in (s["text"] or "")]; ok("причина клиенту не уходит")
    assert calls("CloseForumTopic") and D.relay_topic(1)["closed"] == 1; ok("итоговый статус → тема закрыта")
    assert any("тема закрыта" in (s["text"] or "") for s in w.to(G)); ok("в теме запись о закрытии")
    # клиент пишет после закрытия
    w.calls.clear(); w.clear(); await w.msg(21, "Подумал, готов идти")
    assert calls("ReopenForumTopic") and D.relay_topic(1)["closed"] == 0; ok("клиент написал после закрытия → тема открыта снова")
    assert any(s["text"] == R.REOPEN_ACK for s in w.to(21)) and any("после закрытия" in (s["text"] or "") for s in w.to(G)); ok("клиенту: «передал менеджеру», в теме пометка")

    # ---------- возврат в очередь ----------
    await w.msg(31, "/start"); await w.press(31, "qm:wa"); await w.msg(31, "+380501234567")
    await w.press(50, "mp:take"); t2 = [s for s in w.sent if s["chat"] == G and "Заявка #2" in (s["text"] or "")][0]; th2 = t2["thread"]
    w.calls.clear(); await w.gpress(50, G, th2, "rt:2:release", t2["id"]); await w.gmsg(50, G, th2, "клиент не отвечает")
    assert D.get_application(2)["status"] == "new" and calls("CloseForumTopic"); ok("«Вернуть в очередь» из темы: заявка снова новая, тема закрыта")
    assert any("Заявка вернулась в очередь" in (s["text"] or "") for s in w.to(50)); ok("модераторам уведомление о заявке в очереди")
    await w.press(51, "mp:take"); assert D.get_application(2)["assigned_to"] == 51 and D.relay_topic(2)["closed"] == 0; ok("другой менеджер взял — та же тема открыта снова")
    assert any("Заявку взял" in (s["text"] or "") and "Пётр" in s["text"] for s in w.to(G)); ok("в теме запись, кто взял")
    assert con().execute("SELECT COUNT(*) FROM relay_topics").fetchone()[0] == 2; ok("на заявку одна тема, а не по теме на каждое взятие")

    # ---------- заблокировавший бота ----------
    w.forbidden.add(31); w.clear(); await w.gmsg(51, G, th2, "Добрый день")
    assert "Не доставлено" in w.last_to(G)["text"] and "заблокировал" in w.last_to(G)["text"]; ok("клиент заблокировал бота → в теме «Не доставлено», нужен звонок")
    assert con().execute("SELECT blocked_at FROM funnel WHERE user_id=31").fetchone()[0]; ok("клиент отмечен заблокировавшим")
    w.forbidden.clear()

    # ---------- постепенное создание тем для старых заявок ----------
    for uid in (71, 72, 73, 74):   # четыре заявки «в работе», взятые «до подключения»: темы нет
        await w.msg(uid, "/start"); await w.press(uid, "qm:call"); await w.msg(uid, f"+7 900 000-00-{uid}")
    c = con(); ids = [r[0] for r in c.execute("SELECT id FROM applications WHERE user_id IN (71,72,73,74) ORDER BY id")]
    c.execute(f"UPDATE applications SET status='reached', assigned_to=50, assigned_name='Иван' WHERE id IN ({','.join(map(str, ids))})"); c.commit(); c.close()
    assert len(D.open_without_topic()) >= 4; ok("есть открытые заявки без тем")
    await w.msg(50, "/relay_sync"); assert D.get_setting("relay_backfill") != "1"; ok("/relay_sync от не-админа игнорируется")
    await w.msg(1, "/relay_sync"); assert "Создаю темы" in w.last_to(1)["text"] and "по 3 каждые 30 секунд" in w.last_to(1)["text"]; ok("/relay_sync: сообщает, сколько тем и как быстро")
    before = len(calls("CreateForumTopic"))
    await R.retry_topics(w.bot); made = len(calls("CreateForumTopic")) - before
    assert made == 3; ok("за одну проверку создаётся не больше 3 тем (лимит Telegram)")
    await R.retry_topics(w.bot); await R.retry_topics(w.bot)
    assert D.open_without_topic() == [] ; ok("через несколько проверок темы есть у всех открытых заявок")
    assert D.get_setting("relay_backfill") == "0" and any("Темы для всех открытых заявок созданы" in (s["text"] or "") for s in w.to(1)); ok("по окончании админ получает «готово», режим выключается")
    await w.msg(1, "/relay_sync"); assert "уже в темах" in w.last_to(1)["text"]; ok("повторный /relay_sync: создавать нечего")

    # ---------- имя по умолчанию берётся из профиля Telegram ----------
    D.add_moderator(77, "", None, 1)
    assert D.manager_display_name(77, "Сергей Петров") == "Сергей"; ok("имя не задано: подпись — имя из профиля Telegram («Менеджер Сергей»), а не «менеджер менеджер»")
    assert D.manager_display_name(77, "Серёжа") == "Серёжа"; ok("сменил имя в профиле — подпись сменилась")
    D.set_setting("mgr_name:77", "Дмитрий"); assert D.manager_display_name(77, "Серёжа") == "Дмитрий"; ok("заданное в боте имя важнее профиля")

    # ---------- группа отключена ----------
    await w.msg(1, "/relay_off"); await w.gmsg(1, G, None, "/relay_off")
    assert D.relay_chat_id() is None; ok("/relay_off отключает переписку")
    await w.msg(41, "/start"); await w.press(41, "qm:call"); await w.msg(41, "+7 900 111-22-33")
    w.clear(); await w.msg(41, "Хочу служить в десанте, есть опыт")
    assert not w.to(G); ok("без группы бот работает по-старому: в группу ничего не пишется")
    assert "десанте" in (D.get_application(D.latest_application(41)["id"])["comment"] or ""); ok("сообщение клиента ушло в комментарий заявки, как раньше")
    print("\nПЕРЕПИСКА ЧЕРЕЗ ГРУППУ — ВСЁ ПРОШЛО")
run(main())
