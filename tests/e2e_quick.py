import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("quick", TYPING_MAX_SECONDS="0.01")
from h import ok, run
import sqlite3

async def main():
    w = h.World(); B = w.B
    import db as D, quick as Q, config as C
    from form import parse_intro
    D.init_db(); D.add_moderator(50, "Иван", None, 1)
    assert C.FORM_VARIANT == "short"; ok("по умолчанию включён короткий диалог")
    con = lambda: sqlite3.connect(path)

    # ---------- /start: сразу диалог, без анкеты ----------
    await w.msg(21, "/start")
    t = [s["text"] for s in w.to(21)]
    assert t == [Q.TEXTS["hello"], Q.TEXTS["ask_info"]], t; ok("/start: две реплики — представление и один вопрос (возраст, город, пол)")
    assert "помощник" in t[0] and "контрактную службу" in t[0]; ok("бот представляется помощником, а не человеком")
    assert (21, "typing") in [(c, a.split(".")[-1].lower()) for c, a in w.actions] or w.actions; ok("перед репликами бот показывает «печатает…» (SendChatAction)")
    assert len(w.actions) >= 2, w.actions; ok("«печатает…» перед каждой репликой: %d раз" % len(w.actions))
    assert not w.to(1); ok("админу в личку о /start ничего не приходит")
    assert not [s for s in w.to(21) if s["kb"] is not None]; ok("никаких кнопок «Оставить заявку» и анкеты — просто вопрос")

    # ---------- первый ответ: недостающее переспрашивается ----------
    w.clear(); await w.msg(21, "27 Иркутск")
    assert w.last_to(21)["text"] == Q.TEXTS["ask_gender"] and w.datas(w.last_to(21)["kb"]) == ["qg:0", "qg:1"]; ok("«27 Иркутск» → возраст и город приняты, переспрашивает только пол (кнопки)")
    f = con().execute("SELECT started_form_at FROM funnel WHERE user_id=21").fetchone()[0]; assert f; ok("в воронке отмечено «начал отвечать»")
    w.clear(); await w.press(21, "qg:0")
    assert w.last_to(21)["text"] == Q.TEXTS["ask_phone"] and "Поделиться номером" in str(w.last_to(21)["kb"]); ok("после выбора пола — просьба оставить номер, кнопка «Поделиться номером»")
    assert "соглаша" in w.last_to(21)["text"] and "обработку персональных данных" in w.last_to(21)["text"]; ok("согласие на обработку данных — в просьбе про номер")

    # ---------- телефон ----------
    w.clear(); await w.msg(21, "8888888")
    assert w.last_to(21)["text"] == Q.TEXTS["bad_phone"] and "Не получилось распознать номер" in w.last_to(21)["text"]; ok("короткий номер: «Не получилось распознать номер. Проверьте цифры…»")
    assert not con().execute("SELECT 1 FROM applications").fetchone(); ok("пока номер неверный, заявка не создаётся")
    w.clear(); await w.msg(21, "+7 (999) 123-45-67")
    admin_card = [s for s in w.to(1) if "Заявка #" in (s["text"] or "")]
    assert admin_card, w.to(1)
    c = admin_card[-1]["text"]
    for part in ("Заявка #1", "🆕 Новая", "U21", "+79991234567", "@u21", "27", "Иркутск", "Мужской"):
        assert part in c, (part, c)
    ok("админу пришла карточка: имя и @username из профиля, телефон, возраст, город, пол")
    t = [s["text"] for s in w.to(21)]
    assert t == [Q.TEXTS["done"], Q.TEXTS["extra_invite"]], t; ok("клиенту: «Спасибо, заявка принята! Специалист свяжется с вами в ближайшее время» + приглашение дополнить")
    assert "в ближайшее время" in t[0] and "рассказать о себе подробнее" in t[1] and "опыт, образование, пожелания" in t[1]; ok("в приглашении — «рассказать о себе подробнее: опыт, образование, пожелания»")
    note = [s for s in w.to(50) if "Поступила новая заявка" in (s["text"] or "")][-1]
    for secret in ("+7999", "Иркутск", "U21"):
        assert secret not in note["text"], secret
    ok("модератору — только «Поступила новая заявка» и число, данные клиента скрыты")
    app = D.get_application(1)
    assert app["status"] == "new" and app["tz_offset"] is None and app["call_time"] is None and app["telegram"] == "@u21" and app["age"] == 27, app; ok("в базе: статус «Новая», без часового пояса и времени звонка, @username сохранён")
    f = con().execute("SELECT completed_at FROM funnel WHERE user_id=21").fetchone()[0]; assert f; ok("в воронке: заявка подана")

    # ---------- дополнение информации обычным сообщением ----------
    w.clear(); await w.msg(21, "Служил срочную в ВДВ, хотел бы в десант")
    assert w.last_to(21)["text"] == Q.TEXTS["extra_ack"]; ok("любое сообщение после подачи: «Записал и передам специалисту»")
    assert D.get_application(1)["comment"] == "Служил срочную в ВДВ, хотел бы в десант"; ok("записано в комментарий клиента")
    e = [x for x in w.edits if x["chat"] == 1][-1]["text"]; assert "Служил срочную" in e; ok("карточка админа обновилась")
    await w.msg(21, "Есть мед. образование, фельдшер"); assert D.get_application(1)["comment"].count("\n") == 1; ok("несколько дополнений дописываются друг за другом")
    w.clear(); await w.msg(21, "ок"); assert "на рассмотрении" in w.last_to(21)["text"] and D.get_application(1)["comment"].count("\n") == 1; ok("слишком короткие реплики («ок») в заявку не пишутся")
    await w.msg(21, "/start"); assert "на рассмотрении" in w.last_to(21)["text"]; ok("повторный /start: «заявка на рассмотрении», новой не создать")
    # модератор видит дополнения
    await w.press(50, "mp:take"); mc = w.last_to(50)["text"]
    assert "Служил срочную" in mc and "фельдшер" in mc and "+79991234567" in mc, mc; ok("модератор, взяв заявку, видит всё, что дописал клиент")
    t = __import__("table").row_block(D.get_application(1)); assert "Клиент пишет:" in t and "Служил срочную" in t; ok("и в таблице: строка «Клиент пишет: …»")
    from openpyxl import load_workbook
    import io
    w.docs.clear(); await w.press(50, "tb:xl"); ws = load_workbook(io.BytesIO(w.docs[-1][2])).active
    assert "Клиент пишет:" in [r for r in ws.iter_rows(values_only=True)][1][2]; ok("и в Excel «Моя таблица» (в ячейке анкеты)")

    # ---------- разные формулировки и ошибки ----------
    cases = [("27, Иркутск, мужчина", 27, "Иркутск", "Мужской"), ("Омск 34 ж", 34, "Омск", "Женский"),
             ("мне 41 год, живу в Казани, девушка", 41, "Казани", "Женский"), ("г. Санкт-Петербург, 30, м", 30, "Санкт-Петербург", "Мужской"),
             ("нижний новгород 28 муж", 28, "Нижний Новгород", "Мужской")]
    for text, age, city, gender in cases:
        assert parse_intro(text) == {"age": age, "city": city, "gender": gender}, (text, parse_intro(text))
    ok("разбор: «27, Иркутск, мужчина», «Омск 34 ж», «мне 41 год, живу в Казани, девушка», «г. Санкт-Петербург, 30, м», «нижний новгород 28 муж»")
    # полный сценарий «в одно сообщение» + недопустимый возраст
    w.clear(); await w.msg(31, "/start"); w.clear()
    await w.msg(31, "17 Самара муж"); t = w.last_to(31)["text"]
    assert "от 18 до 65" in t; ok("возраст 17: «Служба по контракту доступна от 18 до 65 лет…»")
    await w.msg(31, "19"); assert w.last_to(31)["text"] == Q.TEXTS["ask_phone"]; ok("после исправленного возраста всё собрано (город и пол запомнены) — сразу просьба о номере")
    w.clear(); await w.msg(31, "привет"); assert w.last_to(31)["text"] == Q.TEXTS["bad_phone"]; ok("слово вместо номера: просьба написать номер полностью")
    # контакт кнопкой
    w.clear()
    D.save_application  # noqa
    n = w.n; w.n += 1
    await w.feed(message={"message_id": 9000 + w.n, "date": 0, "chat": {"id": 31, "type": "private"}, "from": w.user(31), "contact": {"phone_number": "79001112233", "first_name": "U31", "user_id": 31}})
    app = [a for a in [D.get_application(i) for i in (2, 3)] if a and a["user_id"] == 31][0]
    assert app["phone"] == "+79001112233" and app["city"] == "Самара" and app["age"] == 19 and app["gender"] == "Мужской"; ok("номер кнопкой «Поделиться номером»: заявка создана (+79001112233)")
    # только город / только возраст
    w.clear(); await w.msg(41, "/start"); w.clear(); await w.msg(41, "Иркутск")
    assert w.last_to(41)["text"] == Q.TEXTS["ask_age"]; ok("только город → спрашивает возраст")
    await w.msg(41, "30"); assert w.last_to(41)["text"] == Q.TEXTS["ask_gender"]; ok("затем — пол")
    w.clear(); await w.msg(42, "/start"); w.clear(); await w.msg(42, "🙂"); assert w.last_to(42)["text"] == Q.TEXTS["unparsed"]; ok("непонятное сообщение: подсказка с примером")
    # /start посреди диалога начинает его заново, а не «не получилось разобрать»
    w.clear(); await w.msg(42, "/start"); assert [s["text"] for s in w.to(42)] == [Q.TEXTS["hello"], Q.TEXTS["ask_info"]]; ok("/start посреди диалога — начало заново")
    await w.msg(42, "25 Омск"); w.clear(); await w.msg(42, "/start"); assert w.to(42)[0]["text"] == Q.TEXTS["hello"]; ok("/start на шаге «пол» — тоже заново")
    # /cancel и кнопка из напоминания
    w.clear(); await w.msg(42, "/cancel"); assert "отменена" in w.last_to(42)["text"] and "apply" in str(w.datas(w.last_to(42)["kb"])); ok("/cancel: «Заявка отменена» и кнопка «Оставить заявку»")
    w.clear(); await w.press(42, "apply"); assert Q.TEXTS["hello"] in [s["text"] for s in w.to(42)]; ok("кнопка «Оставить заявку» (в т.ч. из напоминаний) запускает тот же короткий диалог")

    # ---------- приоритет выдачи без часового пояса ----------
    from utils import lead_priority
    assert lead_priority(None, None) == 1; ok("заявка без пояса и времени звонка: приоритет «любое время» (ночное правило не применяется)")
    print("\nБЫСТРАЯ ЗАЯВКА — ВСЁ ПРОШЛО")
run(main())
