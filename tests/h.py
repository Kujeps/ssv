"""Каркас для сквозных тестов: имитация Telegram без сети."""
import asyncio
import os
import sqlite3
import sys
import tempfile

SP = tempfile.mkdtemp(prefix="svo_bot_test_")  # база и файлы тестов — во временной папке
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {1: "Админ", 50: "Иван", 51: "Пётр", 52: "Анна"}


def setup(name, admins="1", **env):
    path = f"{SP}/{name}.db"
    if os.path.exists(path):
        os.remove(path)
    os.environ.update(BOT_TOKEN="123:TEST", ADMIN_USER_IDS=admins, DB_PATH=path, TZ_OFFSET_HOURS="3",
                      TYPING_MAX_SECONDS="0")
    for k, v in env.items():
        os.environ[k] = str(v)
    sys.path.insert(0, PROJECT)
    return path


class World:
    """Бот + имитация Telegram + помощники для «действий пользователей»."""

    def __init__(self):
        import bot as B
        from aiogram import Bot
        from aiogram.client.session.base import BaseSession
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.types import Update
        self.B, self.Update = B, Update
        world = self

        class Fake(BaseSession):
            def __init__(self):
                super().__init__()
                self.n = 1000

            async def close(self):
                pass

            async def stream_content(self, *a, **k):
                yield b""

            async def make_request(self, bot, method, timeout=None):
                name = type(method).__name__
                if name == "SendMessage":
                    if method.chat_id in world.forbidden:
                        raise TelegramForbiddenError(method=method, message="Forbidden: bot can't initiate")
                    self.n += 1
                    world.last[method.chat_id] = self.n
                    world.sent.append(dict(chat=method.chat_id, text=method.text, kb=method.reply_markup,
                                           reply=getattr(method, "reply_parameters", None), id=self.n,
                                           thread=getattr(method, "message_thread_id", None)))
                    return method.__returning__.model_validate({
                        "message_id": self.n, "date": 0, "text": method.text,
                        "chat": {"id": method.chat_id, "type": "private"}}, context={"bot": bot})
                if name == "EditMessageText":
                    world.edits.append(dict(chat=method.chat_id, id=method.message_id, text=method.text, kb=method.reply_markup))
                    return True
                if name == "EditMessageReplyMarkup":
                    world.markups.append(dict(chat=method.chat_id, id=method.message_id, kb=method.reply_markup))
                    return True
                if name == "DeleteMessage":
                    world.deleted.append((method.chat_id, method.message_id))
                    return True
                if name == "AnswerCallbackQuery":
                    world.answers.append((method.text, method.show_alert))
                    return True
                if name == "SendDocument":
                    self.n += 1
                    world.docs.append((method.chat_id, method.document.filename, method.document.data, method.caption))
                    return method.__returning__.model_validate({
                        "message_id": self.n, "date": 0, "chat": {"id": method.chat_id, "type": "private"},
                        "document": {"file_id": "f", "file_unique_id": "u"}}, context={"bot": bot})
                if name == "GetMe":
                    return method.__returning__.model_validate(
                        {"id": 8676218033, "is_bot": True, "first_name": "ZV", "username": "pobeda_skoro_bot"})
                if name == "SendChatAction":
                    world.actions.append((method.chat_id, str(method.action)))
                    return True
                if name == "CreateForumTopic":
                    if world.flood > 0:
                        from aiogram.exceptions import TelegramRetryAfter
                        world.flood -= 1
                        raise TelegramRetryAfter(method=method, message="Flood control", retry_after=30)
                    self.n += 1
                    world.calls.append((name, method))
                    return method.__returning__.model_validate(
                        {"message_thread_id": self.n, "name": method.name, "icon_color": 0})
                if name == "CopyMessage":
                    self.n += 1
                    world.calls.append((name, method))
                    return method.__returning__.model_validate({"message_id": self.n})
                if name in ("SetMessageReaction", "CloseForumTopic", "ReopenForumTopic", "PinChatMessage",
                            "EditMessageCaption"):
                    world.calls.append((name, method))
                    return True
                if name in ("SetMyCommands", "DeleteMyCommands"):
                    world.commands.append((name, getattr(method, "scope", None)))
                    return True
                return True

        self.sent, self.edits, self.markups, self.deleted = [], [], [], []
        self.answers, self.docs, self.commands, self.actions = [], [], [], []
        self.calls = []
        self.flood = 0
        self.last, self.forbidden = {}, set()
        self.n = 0
        self.session = Fake()
        self.bot = Bot("123:TEST", session=self.session, default=B.bot_props)

    # --- действия «пользователей» ---
    def _upd(self, **kw):
        self.n += 1
        return self.Update.model_validate({"update_id": self.n, **kw})

    @staticmethod
    def user(uid, name=None):
        return {"id": uid, "is_bot": False, "first_name": name or NAMES.get(uid) or f"U{uid}", "username": f"u{uid}"}

    async def feed(self, **kw):
        await self.B.dp.feed_update(self.bot, self._upd(**kw))

    async def msg(self, uid, text, name=None):
        self.n += 1
        await self.feed(message={"message_id": 9000 + self.n, "date": 0, "chat": {"id": uid, "type": "private"},
                                 "from": self.user(uid, name), "text": text})

    async def shared(self, uid, users):
        self.n += 1
        await self.feed(message={"message_id": 9000 + self.n, "date": 0, "chat": {"id": uid, "type": "private"},
                                 "from": self.user(uid), "users_shared": {"request_id": 1, "users": users}})

    def with_kb(self, chat):
        ids = [s["id"] for s in self.sent if s["chat"] == chat and s["kb"] is not None]
        return ids[-1] if ids else 1

    async def press(self, uid, data, mid=None, name=None):
        self.answers.clear()
        self.n += 1
        await self.feed(callback_query={
            "id": str(self.n), "from": self.user(uid, name), "chat_instance": "x", "data": data,
            "message": {"message_id": mid or self.with_kb(uid), "date": 0,
                        "chat": {"id": uid, "type": "private"}, "text": "x"}})

    # --- группа с темами и вложения ---
    async def gmsg(self, uid, chat, thread, text=None, edit=False, **fields):
        self.n += 1
        m = {"message_id": 9000 + self.n if "message_id" not in fields else fields.pop("message_id"), "date": 0,
             "chat": {"id": chat, "type": "supergroup", "is_forum": True, "title": "g"},
             "from": self.user(uid), "message_thread_id": thread, "is_topic_message": True, **fields}
        if text is not None:
            m["text"] = text
        await self.feed(**({"edited_message": m} if edit else {"message": m}))
        return m["message_id"]

    async def gpress(self, uid, chat, thread, data, mid):
        self.answers.clear()
        self.n += 1
        await self.feed(callback_query={
            "id": str(self.n), "from": self.user(uid), "chat_instance": "x", "data": data,
            "message": {"message_id": mid, "date": 0, "message_thread_id": thread, "is_topic_message": True,
                        "chat": {"id": chat, "type": "supergroup", "is_forum": True, "title": "g"}, "text": "x"}})

    async def media(self, uid, kind="photo", caption=None):
        """Клиент присылает вложение в личку."""
        self.n += 1
        body = {"photo": {"photo": [{"file_id": "ph", "file_unique_id": "u", "width": 9, "height": 9}]},
                "voice": {"voice": {"file_id": "vo", "file_unique_id": "u", "duration": 1}},
                "sticker": {"sticker": {"file_id": "st", "file_unique_id": "u", "type": "regular", "width": 9,
                                        "height": 9, "is_animated": False, "is_video": False}}}[kind]
        m = {"message_id": 9000 + self.n, "date": 0, "chat": {"id": uid, "type": "private"},
             "from": self.user(uid), **body}
        if caption:
            m["caption"] = caption
        await self.feed(message=m)
        return m["message_id"]

    # --- наблюдение ---
    def to(self, chat):
        return [s for s in self.sent if s["chat"] == chat]

    def last_to(self, chat):
        return self.to(chat)[-1]

    def buttons(self, markup):
        return [b.text for r in markup.inline_keyboard for b in r] if markup else []

    def datas(self, markup):
        return [b.callback_data for r in markup.inline_keyboard for b in r if b.callback_data] if markup else []

    def clear(self):
        self.sent.clear(); self.edits.clear(); self.markups.clear(); self.deleted.clear(); self.answers.clear()


def ok(text):
    print("  PASS", text)


def run(coro):
    asyncio.run(coro)
