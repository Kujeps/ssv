import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h
path = h.setup("n5", ADMIN_CHAT_IDS="-1004480737949", MANAGER_IDS="50")   # старые переменные окружения не мешают
from h import ok, run
import sqlite3

# База «как на сервере сейчас» (версия с группой менеджеров и статусами)
c = sqlite3.connect(path)
c.execute("""CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, user_id INTEGER, username TEXT, full_name TEXT, name TEXT, phone TEXT, telegram TEXT, max_contact TEXT, whatsapp TEXT,
 gender TEXT, medical TEXT, age INTEGER, city TEXT, unit TEXT, served TEXT, call_time TEXT, comment TEXT, source TEXT, status TEXT DEFAULT 'new', status_by TEXT, status_by_id INTEGER, status_at TEXT, reminded_at TEXT, remind_count INTEGER DEFAULT 0, legacy INTEGER DEFAULT 0)""")
rows = [("A-новая-с-карточкой", "new", 0), ("B-в-работе", "work", 0), ("C-недозвон", "nocall", 0), ("D-согласился", "agreed", 0), ("E-старая-без-карточки", "new", 1)]
for i, (n, st, lg) in enumerate(rows, 1):
    c.execute("INSERT INTO applications (user_id, name, phone, status, status_by, legacy) VALUES (?,?,?,?,?,?)", (300 + i, n, f"+7999{i}", st, "Менеджер" if st != "new" else None, lg))
c.execute("CREATE TABLE lead_messages (app_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY (chat_id, message_id))")
c.execute("INSERT INTO lead_messages VALUES (1, -1004480737949, 777)")     # карточка в старой группе
c.execute("CREATE TABLE status_history (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT, by_id INTEGER, by_name TEXT)")
c.execute("INSERT INTO status_history (app_id, status, by_id, by_name) VALUES (2, 'work', 5, 'Менеджер')")
c.execute("CREATE TABLE funnel (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, started_at TEXT, started_form_at TEXT, completed_at TEXT, blocked_at TEXT, source TEXT)")
c.commit(); c.close()

async def main():
    w = h.World(); B = w.B
    import db as D, notify as N
    D.init_db(); D.init_db()
    st = dict(sqlite3.connect(path).execute("SELECT name, status FROM applications").fetchall())
    assert st == {"A-новая-с-карточкой": "new", "B-в-работе": "new", "C-недозвон": "new", "D-согласился": "agreed", "E-старая-без-карточки": "new"}, st
    ok("все необработанные заявки (в т.ч. «в работе», «недозвон» и старые без карточки) — в очереди; закрытая осталась")
    assert D.queue_count() == 4; ok("в очереди 4 заявки")
    kinds = [r[0] for r in sqlite3.connect(path).execute("SELECT kind FROM status_history WHERE kind IS NOT NULL")]
    assert kinds.count("migrate") == 2; ok("в истории отмечен перенос (2 заявки), повторный запуск ничего не дублирует")
    old = sqlite3.connect(path).execute("SELECT view FROM lead_messages").fetchone()[0]; assert old == "admin"
    await N.refresh_cards(w.bot, 1)
    assert not [e for e in w.edits if e["chat"] == -1004480737949]; ok("старые карточки из группы бот больше не трогает")
    assert D.active_application(305)["status"] == "new"; ok("клиент старой заявки теперь тоже защищён от дубля (его заявка в очереди)")
    # проводка бота
    used = set(B.dp.resolve_used_update_types()); assert {"message", "callback_query"} <= used; ok("бот слушает сообщения и нажатия кнопок: " + ", ".join(sorted(used)))
    await B.setup_commands(w.bot); names = [c[0] for c in w.commands]; assert names.count("SetMyCommands") >= 2; ok("меню команд для всех и для админа установлено")
    # /start админа после миграции
    await w.msg(1, "/start"); t = w.last_to(1); assert "В очереди: <b>4</b>" in t["text"]; ok("панель админа показывает «В очереди: 4»")
    print("\nN5: миграция — ВСЁ ПРОШЛО")
run(main())
