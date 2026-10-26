import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("requeue", TYPING_MAX_SECONDS="0")
from h import ok, run

async def main():
    w = h.World(); B = w.B
    import db as D
    D.init_db(); D.add_moderator(50, "Иван", None, 1); D.add_moderator(51, "Пётр", None, 1)

    async def lead(uid):
        await w.msg(uid, "/start"); await w.press(uid, "qm:call"); await w.msg(uid, f"+7 900 000-00-{uid % 100:02d}")
        return D.latest_application(uid)["id"]

    # Иван «странно» обработал заявки: две в отказ, одну в мусор; одну Пётр — в согласие; одна ещё в очереди
    ids = [await lead(u) for u in (21, 22, 23, 24, 25)]
    for i, (mod, act) in enumerate([(50, "refused"), (50, "refused"), (50, "junk"), (51, "agreed")]):
        await w.press(mod, "mp:take")
        if act == "agreed":
            await w.press(mod, f"mv:{ids[i]}:agreed")
        else:
            await w.press(mod, f"mv:{ids[i]}:{act}"); await w.msg(mod, "странная причина")
    assert [D.get_application(i)["status"] for i in ids] == ["refused", "refused", "junk", "agreed", "new"]; ok("исходно: 2 отказа и мусор у Ивана, согласие у Петра, одна новая")

    # админ: архив → фильтр «Модератор: Иван» → вернуть в очередь
    await w.msg(1, "/start"); await w.press(1, "ap:arch")
    assert "ar:rq" in w.datas(w.last_to(1)["kb"]); ok("в «Все заявки» у админа кнопка «♻️ Вернуть в очередь по фильтру»")
    await w.press(1, "ar:m:50")
    w.clear(); await w.press(1, "ar:rq")
    t = w.last_to(1)["text"]; assert "Вернуть в очередь 3 заявок" in t and "Иван" in t; ok("подтверждение: 3 заявки Ивана, видно, по какому фильтру")
    w.clear(); await w.press(1, "ar:rqok")
    st = [D.get_application(i)["status"] for i in ids]
    assert st == ["new", "new", "new", "agreed", "new"], st; ok("заявки Ивана снова «Новые», согласие Петра не тронуто")
    a = D.get_application(ids[0]); assert a["assigned_to"] is None and any("странная причина" in n["text"] for n in a["notes"]); ok("заявка свободна, прежние заметки сохранены")
    assert any("вернул в очередь ваши заявки (3)" in (s["text"] or "") for s in w.to(50)); ok("Ивану сообщили, что заявки возвращены")
    assert any("повторной обработки: 3" in (s["text"] or "") for s in w.to(51)); ok("модераторам — уведомление о заявках в очереди")

    # повторная обработка: Пётр берёт — сначала самые старые
    await w.press(51, "mp:take"); assert D.get_application(ids[0])["assigned_to"] == 51; ok("при «Взять заявку» первой выдаётся самая старая из возвращённых")

    # фильтр по статусу; без фильтров — предупреждение
    await w.press(1, "ar:reset"); await w.press(1, "ar:s:agreed"); w.clear(); await w.press(1, "ar:rq")
    assert "Вернуть в очередь 1 заявок" in w.last_to(1)["text"]; ok("фильтр «Статус: Согласился» — 1 заявка")
    await w.press(1, "ar:reset"); w.clear(); await w.press(1, "ar:rq")
    assert "Фильтры не заданы" in w.last_to(1)["text"]; ok("без фильтров — предупреждение, что вернутся все")
    await w.press(1, "ar:s:new"); w.clear(); await w.press(1, "ar:rq")
    assert w.answers and "вернуть нечего" in w.answers[-1][0]; ok("фильтр «Новые»: вернуть нечего")
    await w.msg(50, "/start"); await w.press(50, "mp:arch"); assert "ar:rq" not in w.datas(w.last_to(50)["kb"]); ok("у модератора такой кнопки нет")
    print("\nПОВТОРНАЯ ОБРАБОТКА — ВСЁ ПРОШЛО")
run(main())
