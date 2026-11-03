"""Работа с базой данных SQLite через aiosqlite.

Храним:
- users: user_id, chat_id, таймзона (IANA-имя, например Europe/Moscow)
- reminders: разовые (kind='once') и повторяющиеся (kind='recurring') напоминания

fire_at — Unix-timestamp (UTC) следующего срабатывания.
"""
import time
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    timezone   TEXT NOT NULL DEFAULT 'Europe/Moscow',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    chat_id      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('once', 'recurring')),
    fire_at      REAL NOT NULL,                -- Unix-timestamp UTC следующего срабатывания
    days_of_week TEXT,                         -- '0,2,4' (пн=0..вс=6) для recurring
    time_of_day  TEXT,                         -- 'HH:MM' для recurring
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'fired', 'done', 'deleted')),
    created_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, fire_at);
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id, status);
"""

_db: aiosqlite.Connection | None = None


async def init_db(path: str = "reminders.db") -> None:
    """Открывает соединение и создаёт таблицы, если их нет."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(path)
        _db.row_factory = aiosqlite.Row
        await _db.executescript(SCHEMA)
        await _db.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Пользователи ----------

async def get_user(user_id: int):
    cur = await _db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


async def add_user(user_id: int, chat_id: int, timezone_name: str) -> None:
    await _db.execute(
        "INSERT OR REPLACE INTO users (user_id, chat_id, timezone, created_at)"
        " VALUES (?, ?, ?, ?)",
        (user_id, chat_id, timezone_name, _now_iso()),
    )
    await _db.commit()


async def set_timezone(user_id: int, timezone_name: str) -> None:
    await _db.execute(
        "UPDATE users SET timezone = ? WHERE user_id = ?",
        (timezone_name, user_id),
    )
    await _db.commit()


# ---------- Напоминания ----------

async def add_reminder(user_id: int, chat_id: int, text: str, kind: str,
                       fire_at: float, days_of_week: str | None = None,
                       time_of_day: str | None = None) -> int:
    cur = await _db.execute(
        "INSERT INTO reminders"
        " (user_id, chat_id, text, kind, fire_at, days_of_week, time_of_day, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (user_id, chat_id, text, kind, fire_at, days_of_week, time_of_day, _now_iso()),
    )
    await _db.commit()
    return cur.lastrowid


async def get_reminder(reminder_id: int):
    cur = await _db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    return await cur.fetchone()


async def get_due_reminders(now_ts: float):
    """Все pending-напоминания, у которых подошло время (с таймзоной владельца)."""
    cur = await _db.execute(
        "SELECT r.*, u.timezone AS timezone FROM reminders r"
        " JOIN users u ON u.user_id = r.user_id"
        " WHERE r.status = 'pending' AND r.fire_at <= ?"
        " ORDER BY r.fire_at",
        (now_ts,),
    )
    return await cur.fetchall()


async def list_pending(user_id: int):
    """Активные напоминания пользователя (другие пользователи не видят чужие)."""
    cur = await _db.execute(
        "SELECT * FROM reminders WHERE user_id = ? AND status = 'pending'"
        " ORDER BY fire_at",
        (user_id,),
    )
    return await cur.fetchall()


async def mark_fired(reminder_id: int) -> None:
    """Разовое напоминание отправлено — ждёт реакции (Готово/Отложить)."""
    await _db.execute(
        "UPDATE reminders SET status = 'fired' WHERE id = ?", (reminder_id,)
    )
    await _db.commit()


async def mark_done(reminder_id: int) -> None:
    await _db.execute(
        "UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,)
    )
    await _db.commit()


async def snooze(reminder_id: int, minutes: int) -> None:
    """Отложить: новое время = сейчас + minutes, возвращаем в pending."""
    await _db.execute(
        "UPDATE reminders SET fire_at = ?, status = 'pending' WHERE id = ?",
        (time.time() + minutes * 60, reminder_id),
    )
    await _db.commit()


async def update_fire_at(reminder_id: int, fire_at: float) -> None:
    """Вычислить следующее срабатывание для повторяющегося напоминания."""
    await _db.execute(
        "UPDATE reminders SET fire_at = ? WHERE id = ?", (fire_at, reminder_id)
    )
    await _db.commit()


async def delete_reminder(reminder_id: int) -> None:
    await _db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    await _db.commit()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
