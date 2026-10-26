import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("claim", TYPING_MAX_SECONDS="0")
from h import ok, run

G = -100900

async def main():
    w = h.World(); B = w.B
    import db as D, relay as R
    R.is_work_time = lambda now=None: True
    D.init_db(); D.add_moderator(50, "Иван", None, 1); D.add_moderator(51, "Пётр", None, 1)
    calls = lambda name: [m for n, m in w.calls if n == name]
    await w.gmsg(1, G, None, "/relay_here")

    async def lead(uid):
        await w.msg(uid, "/start"); await w.press(uid, "qm:call"); await w.msg(uid, f"+7 900 000-00-{uid % 100:02d}")
        return D.latest_application(uid)["id"]

    def thread_of(app_id):
        return D.relay_topic(app_id)["thread_id"]

    # Иван взял и отказал — тема закрыта; заявку вернули в очередь — тема так и закрыта
    a = await lead(21); await w.press(50, "mp:take"); th = thread_of(a)
    await w.press(50, f"mv:{a}:refused"); await w.msg(50, "не берёт трубку")
    assert D.relay_topic(a)["closed"]; ok("исходно: заявка обработана, тема закрыта")
    D.admin_requeue(a, 1, "Админ"); await R.sync_topic(w.bot, a)  # как при «♻️ Вернуть в очередь»
    assert D.get_application(a)["status"] == "new" and D.relay_topic(a)["closed"]
    await w.media(21, "photo", "паспорт")  # клиент прислал, пока заявка в очереди

    # Пётр просто пишет в старую тему
    w.clear(); w.calls.clear(); await w.gmsg(51, G, th, "Здравствуйте! Вернёмся к вашей заявке?")
    app = D.get_application(a)
    assert app["assigned_to"] == 51 and app["status"] == "reached"; ok("модератор написал в тему заявки из очереди → заявка его, «В работе» (без таймера)")
    assert w.last_to(21)["text"] == "<b>Менеджер Пётр:</b>\nЗдравствуйте! Вернёмся к вашей заявке?"; ok("и сообщение сразу ушло клиенту")
    assert calls("ReopenForumTopic") and not D.relay_topic(a)["closed"]; ok("закрытая тема открылась")
    assert any("Заявку взял" in (s["text"] or "") and "Пётр" in s["text"] for s in w.to(G)); ok("в теме запись, кто взял")
    assert any(c.caption and "паспорт" in c.caption for c in calls("CopyMessage") if c.chat_id == G); ok("вложение клиента, присланное пока заявка была в очереди, подтянулось в тему")
    assert any("взял заявку #%d, написав в теме" % a in (s["text"] or "") for s in w.to(1)); ok("админу — уведомление")
    # клиент отвечает — в тему
    w.clear(); await w.msg(21, "Да, давайте"); assert any("Да, давайте" in (s["text"] or "") for s in w.to(G)); ok("ответ клиента идёт в тему")
    # кнопки статусов теперь работают у Петра
    card_id = D.relay_topic(a)["card_msg_id"]
    await w.gpress(51, G, th, f"rt:{a}:agreed", card_id); assert D.get_application(a)["status"] == "agreed"; ok("кнопки статусов в теме работают у нового менеджера")

    # повторная обработка закрытой заявки: Иван пишет в тему «Согласился»-заявки
    w.clear(); await w.gmsg(50, G, th, "Добрый день, уточню пару моментов")
    app = D.get_application(a); assert app["assigned_to"] == 50 and app["status"] == "reached"; ok("написал в тему закрытой заявки → повторная обработка, заявка его")
    assert any("Повторная обработка" in (s["text"] or "") for s in w.to(G)); ok("в теме пометка «Повторная обработка»")

    # чужую заявку в работе модератор забрать не может
    w.clear(); await w.gmsg(51, G, th, "Пётр снова тут")
    assert D.get_application(a)["assigned_to"] == 50 and not [s for s in w.to(21) if "Пётр снова" in (s["text"] or "")]; ok("заявку в работе у другого модератор не забирает, клиенту не уходит")
    assert "Передать" in w.last_to(G)["text"]; ok("пояснение: попросите передать заявку")

    # админ (не модератор) пишет — забирает себе, Ивану сообщение
    assert not D.is_moderator(1)
    w.clear(); await w.gmsg(1, G, th, "Здравствуйте, это руководитель")
    app = D.get_application(a); assert app["assigned_to"] == 1 and app["status"] == "reached"; ok("админ написал в теме → забрал заявку себе")
    assert D.is_moderator(1); ok("админ автоматически включён в «Беру заявки сам»")
    assert any("забрал себе" in (s["text"] or "") for s in w.to(50)) and any("забрал себе" in (s["text"] or "") for s in w.to(G)); ok("прежнему менеджеру и в теме — «забрал себе»")
    assert w.last_to(21)["text"].startswith("<b>Менеджер"); ok("сообщение админа ушло клиенту")

    # заметка с # заявку не забирает
    w.clear(); await w.gmsg(50, G, th, "# посмотрю позже")
    assert D.get_application(a)["assigned_to"] == 1; ok("заметка (#) заявку не забирает")
    print("\nЗАЯВКА ПО СООБЩЕНИЮ В ТЕМЕ — ВСЁ ПРОШЛО")
run(main())
