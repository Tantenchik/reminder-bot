"""Фоновая проверка и отправка напоминаний (раз в 30 секунд)."""
import asyncio
import logging
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # секунды между проверками


def next_recurring_fire(days: set[int], hour: int, minute: int,
                        tz_name: str) -> float:
    """Ближайшее время срабатывания (Unix-timestamp UTC) для заданных дней недели."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    for shift in range(0, 8):  # сегодня + 7 дней вперёд — гарантированно найдём
        day = now_local.date() + timedelta(days=shift)
        if day.weekday() not in days:
            continue
        fire = datetime.combine(day, dt_time(hour, minute), tzinfo=tz)
        if fire > now_local:
            return fire.timestamp()
    raise RuntimeError("Не удалось найти ближайшее время для повторяющегося напоминания")


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Готово ✔", callback_data=f"done:{reminder_id}")],
        [
            InlineKeyboardButton(text="Отложить на 10 минут",
                                 callback_data=f"snz10:{reminder_id}"),
            InlineKeyboardButton(text="Отложить на 1 час",
                                 callback_data=f"snz60:{reminder_id}"),
        ],
    ])


async def send_due(bot: Bot) -> None:
    now = time.time()
    for r in await db.get_due_reminders(now):
        text = f"🔔 Напоминание!\n\n{r['text']}"
        try:
            await bot.send_message(r["chat_id"], text,
                                   reply_markup=reminder_keyboard(r["id"]))
        except TelegramAPIError as exc:
            # Чат заблокировал бота и т.п. — не спамим повторными попытками.
            log.warning("Не удалось отправить напоминание %s: %s", r["id"], exc)
            await db.mark_done(r["id"])
            continue
        except Exception:
            log.exception("Ошибка при отправке напоминания %s", r["id"])
            continue

        if r["kind"] == "recurring":
            days = {int(x) for x in r["days_of_week"].split(",")}
            hour, minute = map(int, r["time_of_day"].split(":"))
            nxt = next_recurring_fire(days, hour, minute, r["timezone"])
            await db.update_fire_at(r["id"], nxt)
            log.info("Отправлено повторяющееся #%s, следующее срабатывание %s",
                     r["id"], nxt)
        else:
            await db.mark_fired(r["id"])
            log.info("Отправлено разовое #%s", r["id"])


async def scheduler_loop(bot: Bot) -> None:
    log.info("Планировщик запущен (проверка каждые %s сек)", CHECK_INTERVAL)
    while True:
        try:
            await send_due(bot)
        except Exception:
            log.exception("Ошибка в цикле планировщика")
        await asyncio.sleep(CHECK_INTERVAL)
