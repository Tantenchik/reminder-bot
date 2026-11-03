"""Хендлеры: команды, мастер повторяющихся напоминаний, естественный язык."""
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateparser.search import search_dates
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import db
from scheduler import next_recurring_fire

log = logging.getLogger(__name__)

router = Router()

TRIGGER_RE = re.compile(
    r"^\s*(?:@[\w_]+\s+)?(?:бот[,:]?\s*)?(?:напомни(?:ть)?|напоминание)\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
FILLER_RE = re.compile(
    r"^(?:что|чтобы|мне|надо|нужно|про|о\s+том\s*,?\s*что)\s+", re.IGNORECASE
)

DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
COMMON_TIMES = ["07:00", "08:00", "09:00", "10:00", "12:00", "13:00",
                "15:00", "18:00", "19:00", "20:00", "21:00", "22:00"]
COMMON_TZ = {
    "Москва": "Europe/Moscow",
    "Калининград": "Europe/Kaliningrad",
    "Самара": "Europe/Samara",
    "Екатеринбург": "Asia/Yekaterinburg",
    "Омск": "Asia/Omsk",
    "Новосибирск": "Asia/Novosibirsk",
    "Красноярск": "Asia/Krasnoyarsk",
    "Иркутск": "Asia/Irkutsk",
    "Хабаровск": "Asia/Khabarovsk",
    "Владивосток": "Asia/Vladivostok",
    "UTC": "UTC",
}

START_TEXT = (
    "👋 Привет! Я «Напоминалка» — запоминаю и напоминаю.\n\n"
    "Просто напиши мне естественным языком:\n"
    "• «напомни завтра в 18:00 забрать посылку»\n"
    "• «через 2 часа позвонить маме»\n"
    "• «в пятницу в 09:30 встреча с врачом»\n"
    "• «15.11 в 09:30 оплатить счёт»\n\n"
    "Команды:\n"
    "/recurring — повторяющееся напоминание (дни недели + время)\n"
    "/list — мои активные напоминания\n"
    "/timezone — сменить часовой пояс\n"
    "/cancel — отменить текущее действие"
)


class RecurringStates(StatesGroup):
    waiting_text = State()
    waiting_days = State()
    waiting_time = State()


class TimezoneStates(StatesGroup):
    waiting_tz = State()


# ---------- Утилиты ----------

def detect_tz() -> str:
    """Таймзона сервера через tzlocal (fallback — Москва)."""
    try:
        from tzlocal import get_localzone
        tz = get_localzone()
        name = getattr(tz, "key", None) or str(tz)
        ZoneInfo(name)  # проверка, что имя валидно
        return name
    except Exception:
        return "Europe/Moscow"


async def ensure_user(message: Message):
    """Регистрирует пользователя по первому сообщению, если его ещё нет."""
    user = await db.get_user(message.from_user.id)
    if user is None:
        await db.add_user(message.from_user.id, message.chat.id, detect_tz())
        user = await db.get_user(message.from_user.id)
    return user


NUMDATE_RE = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d.])")
TIME_AFTER_RE = re.compile(r"^\s*в\s+(\d{1,2})[:.](\d{2})\b", re.IGNORECASE)
BARE_TIME_RE = re.compile(r"^в\s+(\d{1,2})[:.](\d{2})\b", re.IGNORECASE)


def _dp_settings(tz_name: str) -> dict:
    return {
        "TIMEZONE": tz_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "DATE_ORDER": "DMY",
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(ZoneInfo(tz_name)),
    }


def _search_date(cleaned: str, tz_name: str):
    """Ищет словесную дату («завтра», «через 2 часа», «в пятницу») и её позицию."""
    try:
        results = search_dates(cleaned, languages=["ru"],
                               settings=_dp_settings(tz_name))
    except Exception:
        return None
    if not results:
        return None
    for match_str, dt in results:
        start = cleaned.find(match_str)
        if start < 0 or dt is None:
            continue
        before = cleaned[start - 1] if start > 0 else ""
        if before and (before.isdigit() or before == "."):
            continue  # обрезок числовой даты («15.11» -> «11 в 09:30»)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt, start, start + len(match_str)
    return None


def _clean_body(body: str) -> str:
    body = body.strip(" ,.–—:;")
    prev = None
    while prev != body:
        prev = body
        body = FILLER_RE.sub("", body).strip()
    return body


def split_reminder(text: str, tz_name: str):
    """Разделяет сообщение на (дату/время с таймзоной, текст задачи).

    Понимает «завтра в 18:00 забрать посылку» и «забрать посылку завтра в 18:00».
    """
    cleaned = TRIGGER_RE.sub("", text).strip(" ,.")
    if not cleaned:
        return None, None
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    # 1) Числовая дата: 15.11, 15.11.2026, 15/11 (+ необязательное «в 09:30»)
    m = NUMDATE_RE.search(cleaned)
    if m:
        hh = mm = None
        tm = TIME_AFTER_RE.match(cleaned[m.end():])
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
        try:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            if year < 100:
                year += 2000
            dt = datetime(year, month, day, hh or 0, mm or 0, tzinfo=tz)
        except ValueError:
            dt = None  # несуществующая дата (31.02) — пробуем дальше
        if dt is not None:
            end = m.end() + (tm.end() if tm else 0)
            body = _clean_body(cleaned[:m.start()] + " " + cleaned[end:])
            return dt, body

    # 2) Словесная дата: завтра, послезавтра, через 2 часа, в пятницу…
    found = _search_date(cleaned, tz_name)
    if found:
        dt, start, end = found
        body = _clean_body(cleaned[:start] + " " + cleaned[end:])
        return dt, body

    # 3) Только время: «в 18:00 забрать посылку»
    m = BARE_TIME_RE.match(cleaned)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        dt = datetime(now.year, now.month, now.day, hh, mm, tzinfo=tz)
        if dt <= now:
            dt += timedelta(days=1)
        return dt, _clean_body(cleaned[m.end():])

    return None, None


# ---------- Клавиатуры ----------

def days_kb(selected: set[int]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=("✅ " if d in selected else "") + DAYS_SHORT[d],
            callback_data=f"rd:{d}",
        )
        for d in range(7)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:4],
        buttons[4:],
        [InlineKeyboardButton(text="Далее ➡️", callback_data="rd:next")],
    ])


def times_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t, callback_data=f"rt:{t}")
         for t in COMMON_TIMES[i:i + 4]]
        for i in range(0, len(COMMON_TIMES), 4)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tz_kb() -> InlineKeyboardMarkup:
    items = list(COMMON_TZ.items())
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"tzs:{iana}")
         for name, iana in items[i:i + 3]]
        for i in range(0, len(items), 3)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_kb(reminders) -> InlineKeyboardMarkup:
    rows = []
    for n, r in enumerate(reminders, 1):
        short = r["text"] if len(r["text"]) <= 30 else r["text"][:29] + "…"
        rows.append([InlineKeyboardButton(text=f"🗑 {n}. {short}",
                                          callback_data=f"del:{r['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- /start, /cancel ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message)
    await message.answer(START_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять 🙂")
    else:
        await state.clear()
        await message.answer("Отменено ✖️")


# ---------- /timezone ----------

@router.message(Command("timezone"))
async def cmd_timezone(message: Message, state: FSMContext):
    user = await ensure_user(message)
    await state.set_state(TimezoneStates.waiting_tz)
    await message.answer(
        f"Сейчас у тебя часовой пояс: {user['timezone']}.\n"
        "Выбери город кнопкой или напиши название зоны "
        "(например Europe/Moscow или Asia/Novosibirsk):",
        reply_markup=tz_kb(),
    )


@router.callback_query(F.data.startswith("tzs:"))
async def cb_tz_pick(query: CallbackQuery, state: FSMContext):
    await _set_tz(query.message, query.from_user.id, query.data[4:], state)
    await query.answer()


@router.message(TimezoneStates.waiting_tz, F.text)
async def tz_text(message: Message, state: FSMContext):
    name = message.text.strip()
    try:
        ZoneInfo(name)
    except Exception:
        await message.answer(
            "Не знаю такую зону 🤔 Попробуй, например: Europe/Moscow, "
            "Asia/Yekaterinburg, UTC."
        )
        return
    await _set_tz(message, message.from_user.id, name, state)


async def _set_tz(message: Message, user_id: int, name: str, state: FSMContext):
    await db.set_timezone(user_id, name)
    local_now = datetime.now(ZoneInfo(name))
    await state.clear()
    await message.answer(
        f"✅ Установлен часовой пояс {name}. "
        f"Сейчас у тебя {local_now:%d.%m.%Y %H:%M}."
    )


# ---------- /recurring (мастер) ----------

@router.message(Command("recurring"))
async def cmd_recurring(message: Message, state: FSMContext):
    await ensure_user(message)
    await state.clear()
    await state.set_state(RecurringStates.waiting_text)
    await message.answer("Создаём повторяющееся напоминание.\n\n"
                         "Шаг 1 из 3. Напиши текст напоминания:")


@router.message(RecurringStates.waiting_text, F.text)
async def rec_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 2:
        await message.answer("Слишком коротко 🙂 Напиши текст напоминания:")
        return
    await state.update_data(text=text, days=[])
    await state.set_state(RecurringStates.waiting_days)
    await message.answer("Шаг 2 из 3. Выбери дни недели, когда напоминать:",
                         reply_markup=days_kb(set()))


@router.callback_query(F.data.startswith("rd:"), RecurringStates.waiting_days)
async def rec_days(query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    days = set(data.get("days", []))
    payload = query.data[3:]
    if payload == "next":
        if not days:
            await query.answer("Сначала выбери хотя бы один день ☝️",
                               show_alert=True)
            return
        await state.set_state(RecurringStates.waiting_time)
        await query.message.edit_text(
            "Шаг 3 из 3. Выбери время кнопкой или напиши его в формате ЧЧ:ММ "
            "(например 08:00):",
            reply_markup=times_kb(),
        )
        await query.answer()
        return
    days ^= {int(payload)}
    await state.update_data(days=sorted(days))
    await query.message.edit_reply_markup(reply_markup=days_kb(days))
    await query.answer()


@router.message(RecurringStates.waiting_days, F.text)
async def rec_days_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Выбери дни недели кнопками:",
                         reply_markup=days_kb(set(data.get("days", []))))


@router.callback_query(F.data.startswith("rt:"), RecurringStates.waiting_time)
async def rec_time_cb(query: CallbackQuery, state: FSMContext):
    await _finish_recurring(query.message, query.from_user.id, state,
                            query.data[3:])
    await query.answer()


@router.message(RecurringStates.waiting_time, F.text)
async def rec_time_text(message: Message, state: FSMContext):
    t = message.text.strip()
    if not TIME_RE.match(t):
        await message.answer("Напиши время в формате ЧЧ:ММ, например 08:00.",
                             reply_markup=times_kb())
        return
    await _finish_recurring(message, message.from_user.id, state, t)


async def _finish_recurring(message: Message, user_id: int, state: FSMContext,
                            time_str: str):
    data = await state.get_data()
    text = data["text"]
    days = sorted(data.get("days", []))
    hour, minute = map(int, time_str.split(":"))
    user = await db.get_user(user_id)
    fire = next_recurring_fire(set(days), hour, minute, user["timezone"])
    await db.add_reminder(
        user_id=user_id,
        chat_id=message.chat.id,
        text=text,
        kind="recurring",
        fire_at=fire,
        days_of_week=",".join(str(d) for d in days),
        time_of_day=f"{hour:02d}:{minute:02d}",
    )
    await state.clear()
    days_str = ", ".join(DAYS_SHORT[d] for d in days)
    await message.answer(f"✅ Готово! Буду напоминать «{text}» — "
                         f"{days_str} в {hour:02d}:{minute:02d}.")


# ---------- /list ----------

@router.message(Command("list"))
async def cmd_list(message: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(message)
    reminders = await db.list_pending(message.from_user.id)
    if not reminders:
        await message.answer(
            "У тебя нет активных напоминаний 🎉\n"
            "Напиши «напомни завтра в 18:00 …» или используй /recurring."
        )
        return
    lines = []
    for n, r in enumerate(reminders, 1):
        if r["kind"] == "recurring":
            days = [int(x) for x in r["days_of_week"].split(",")]
            when = ", ".join(DAYS_SHORT[d] for d in days) + f" в {r['time_of_day']}"
        else:
            when = datetime.fromtimestamp(
                r["fire_at"], ZoneInfo(user["timezone"])
            ).strftime("%d.%m.%Y %H:%M")
        lines.append(f"{n}. {r['text']}\n   ⏰ {when}")
    await message.answer("Твои активные напоминания:\n\n" + "\n".join(lines),
                         reply_markup=list_kb(reminders))


@router.callback_query(F.data.startswith("del:"))
async def cb_del(query: CallbackQuery):
    rid = int(query.data[4:])
    r = await db.get_reminder(rid)
    if r is None or r["user_id"] != query.from_user.id:
        await query.answer("Такого напоминания нет.", show_alert=True)
        return
    await db.delete_reminder(rid)
    await query.message.edit_text(f"🗑 Удалено: «{r['text']}»")
    await query.answer("Удалено")


# ---------- Кнопки на сообщении-напоминании ----------

@router.callback_query(F.data.startswith("done:"))
async def cb_done(query: CallbackQuery):
    rid = int(query.data[5:])
    r = await db.get_reminder(rid)
    if r is None or r["user_id"] != query.from_user.id:
        await query.answer()
        return
    await db.mark_done(rid)
    await query.message.edit_text(f"✅ Выполнено: «{r['text']}»\nМолодец!")
    await query.answer("Отмечено выполненным ✔")


@router.callback_query(F.data.startswith("snz10:"))
@router.callback_query(F.data.startswith("snz60:"))
async def cb_snooze(query: CallbackQuery):
    minutes = 10 if query.data.startswith("snz10:") else 60
    rid = int(query.data.split(":")[1])
    r = await db.get_reminder(rid)
    if r is None or r["user_id"] != query.from_user.id:
        await query.answer()
        return
    await db.snooze(rid, minutes)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.answer(f"⏰ Отложено на {minutes} минут")


# ---------- Естественный язык (ловим в самом конце) ----------

@router.message(F.text)
async def on_text(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return  # пользователь внутри мастера — отвечают его шаги
    user = await ensure_user(message)
    tz = user["timezone"]
    dt, body = split_reminder(message.text, tz)

    if dt is None:
        await message.answer(
            "Не понял дату и время 🤔\n"
            "Примеры: «напомни завтра в 18:00 забрать посылку», "
            "«через 2 часа позвонить».\n"
            "Или используй команду /recurring."
        )
        return
    if not body:
        await message.answer(
            "Напиши, что именно напомнить:\n"
            "«напомни завтра в 18:00 забрать посылку»."
        )
        return

    now = datetime.now(ZoneInfo(tz))
    if dt <= now:
        await message.answer(
            "Это время уже прошло 🙂 Уточни дату, например "
            "«завтра в 18:00 …» или «через 2 часа …»."
        )
        return

    await db.add_reminder(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        text=body,
        kind="once",
        fire_at=dt.timestamp(),
    )
    await message.answer(
        f"🔔 Напомню «{body}» — {dt:%d.%m.%Y %H:%M} ({tz}).\n"
        f"Все напоминания: /list"
    )
