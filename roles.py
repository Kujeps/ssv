"""Роли пользователей: админ, модератор, клиент (все остальные)."""
from aiogram.filters import BaseFilter

from config import ADMIN_USER_IDS
from db import is_moderator


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_staff(user_id: int) -> bool:
    return is_admin(user_id) or is_moderator(user_id)


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and is_admin(user.id))


class IsModerator(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and is_moderator(user.id))


class IsStaff(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and is_staff(user.id))
