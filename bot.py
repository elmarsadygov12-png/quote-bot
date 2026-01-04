import os
import json
import base64
import random
import time
from pathlib import Path
from typing import Dict, Any, Tuple

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI
from quotes import QUOTES

# ===== настройки лимитов =====
DAILY_LIMIT = 20          # 20 генераций в день
COOLDOWN_SEC = 3.0        # не чаще 1 генерации в 3 секунды

# Надёжно грузим .env рядом с bot.py
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Нет BOT_TOKEN в .env")
if not OPENAI_API_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# user_state[user_id] = {
#   "gender": "female|male|universal",
#   "length": "short|medium",
#   "mode": "clean|adult",
#   "adult_ok": bool,
#   "analysis": dict|None,
#   "used_quotes": set(),
#   "quota_day": "YYYY-MM-DD",
#   "quota_used": int,
#   "last_req_ts": float,
# }
user_state: Dict[int, Dict[str, Any]] = {}

def today_str() -> str:
    # локальное время машины/сервера; для простоты ок
    return time.strftime("%Y-%m-%d", time.localtime())

def st(uid: int) -> Dict[str, Any]:
    if uid not in user_state:
        user_state[uid] = {
            "gender": "universal",
            "length": "medium",
            "mode": "clean",
            "adult_ok": False,
            "analysis": None,
            "used_quotes": set(),
            "quota_day": today_str(),
            "quota_used": 0,
            "last_req_ts": 0.0,
        }
    # сброс дневного лимита на новый день
    if user_state[uid]["quota_day"] != today_str():
        user_state[uid]["quota_day"] = today_str()
        user_state[uid]["quota_used"] = 0
    return user_state[uid]

def quota_left(uid: int) -> int:
    s = st(uid)
    return max(0, DAILY_LIMIT - int(s.get("quota_used", 0)))

def can_request(uid: int) -> Tuple[bool, str]:
    s = st(uid)

    # антиспам
    now = time.time()
    dt = now - float(s.get("last_req_ts", 0.0))
    if dt < COOLDOWN_SEC:
        wait = max(1, int(COOLDOWN_SEC - dt + 0.999))
        return False, f"⏳ Подожди {wait} сек и попробуй ещё раз."

    # дневной лимит
    if s.get("quota_used", 0) >= DAILY_LIMIT:
        return False, "Лимит 20 генераций на сегодня исчерпан 😅\nПриходи завтра — лимит обновится."

    return True, ""

def mark_request(uid: int) -> None:
    s = st(uid)
    s["last_req_ts"] = time.time()
    s["quota_used"] = int(s.get("quota_used", 0)) + 1


# ===== keyboards =====
def gender_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👩 Женский", callback_data="gender:female")
    kb.button(text="👨 Мужской", callback_data="gender:male")
    kb.button(text="✨ Универсальный", callback_data="gender:universal")
    kb.adjust(1)
    return kb.as_markup()

def mode_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🧼 Без мата", callback_data="mode:clean")
    kb.button(text="😈 Можно мат (18+)", callback_data="mode:adult")
    kb.adjust(1)
    return kb.as_markup()

def adult_confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Мне 18+ (включить)", callback_data="adult:yes")
    kb.button(text="❌ Нет (без мата)", callback_data="adult:no")
    kb.adjust(1)
    return kb.as_markup()

def actions_kb(uid: int):
    left = quota_left(uid)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔄 Другая (осталось {left})", callback_data="gen:next")
    kb.button(text="✍️ Коротко", callback_data="len:short")
    kb.button(text="🧾 Подлиннее", callback_data="len:medium")
    kb.button(text="🎭 Стиль", callback_data="nav:gender")
    kb.button(text="🧼/😈 Режим", callback_data="nav:mode")
    kb.adjust(1, 2, 2)
    return kb.as_markup()


# ===== fallback quotes =====
def pick_fallback(uid: int) -> str:
    pool = QUOTES.get("универсальные", [])
    used = st(uid)["used_quotes"]
    avail = [q for q in pool if q not in used]
    if not avail:
        used.clear()
        avail = pool[:]
    q = random.choice(avail) if avail else "Красота — это настроение."
    used.add(q)
    return q


# ===== image -> data url =====
async def photo_to_data_url(message: Message) -> str:
    ph = message.photo[-1]
    f = await bot.get_file(ph.file_id)
    fb = await bot.download_file(f.file_path)
    raw = fb.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


# ===== OpenAI calls =====
def analyze_image(image_data_url: str) -> Dict[str, Any]:
    prompt = (
        "Проанализируй фото. Верни строго JSON без лишнего текста.\n"
        "{"
        "\"mood\":\"...\","
        "\"scene\":\"...\","
        "\"colors\":\"...\","
        "\"vibe_tags\":[\"...\",\"...\"],"
        "\"safe\":\"yes|no\""
        "}\n"
        "mood: романтика/уверенность/свобода/грусть/уют/драйв и т.п.\n"
        "safe='no' если изображение явно неприемлемое."
    )
    r = client.responses.create(
        model="gpt-4o-mini",
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": image_data_url},
            ],
        }],
        max_output_tokens=220,
    )
    t = r.output_text.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return {"mood": "универсально", "scene": "фото", "colors": "нейтрально", "vibe_tags": ["aesthetic"], "safe": "yes"}

def generate_caption(analysis: Dict[str, Any], gender: str, length: str, mode: str) -> str:
    gender_style = {
        "female": "Женский стиль: эстетично, мягко, уверенно.",
        "male": "Мужской стиль: сдержанно, уверенно, можно чуть дерзко.",
        "universal": "Универсально: подходит всем, красиво и естественно."
    }[gender]

    len_style = "Очень коротко (до 8 слов)." if length == "short" else "Средняя длина (1–2 строки)."

    if mode == "adult":
        tone = (
            "Разрешён реальный мат (18+), но без травли, без унижения групп людей, "
            "без угроз, без призывов к насилию, без сексуального контента."
        )
    else:
        tone = "Строго без мата и без грубых оскорблений."

    prompt = (
        "Сгенерируй одну подпись для публикации под фото на русском.\n"
        f"{gender_style}\n"
        f"Длина: {len_style}\n"
        f"Ограничения: {tone}\n"
        "Правила:\n"
        "- только одна подпись\n"
        "- без эмодзи\n"
        "- без кавычек\n"
        "- без хэштегов\n\n"
        f"Контекст:\n"
        f"mood: {analysis.get('mood')}\n"
        f"scene: {analysis.get('scene')}\n"
        f"colors: {analysis.get('colors')}\n"
        f"tags: {', '.join(analysis.get('vibe_tags', []))}\n"
    )

    r = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        max_output_tokens=90 if length == "short" else 160,
    )
    return r.output_text.strip().replace('"', "").strip()


# ===== handlers =====
@dp.message(CommandStart())
async def start(message: Message):
    s = st(message.from_user.id)
    s["analysis"] = None
    await message.answer(
        "Привет! Я делаю подписи под фото.\n\n"
        "Шаг 1: выбери стиль:",
        reply_markup=gender_kb()
    )

@dp.callback_query(F.data.startswith("gender:"))
async def on_gender(c: CallbackQuery):
    uid = c.from_user.id
    st(uid)["gender"] = c.data.split(":", 1)[1]
    await c.answer("Ок")
    await c.message.answer("Шаг 2: выбери режим:", reply_markup=mode_kb())

@dp.callback_query(F.data.startswith("mode:"))
async def on_mode(c: CallbackQuery):
    uid = c.from_user.id
    mode = c.data.split(":", 1)[1]
    if mode == "clean":
        st(uid)["mode"] = "clean"
        st(uid)["adult_ok"] = False
        await c.answer("Ок")
        await c.message.answer("Шаг 3: отправь фото 📸")
    else:
        await c.answer()
        await c.message.answer("18+ подтверждаешь?", reply_markup=adult_confirm_kb())

@dp.callback_query(F.data.startswith("adult:"))
async def on_adult_confirm(c: CallbackQuery):
    uid = c.from_user.id
    ans = c.data.split(":", 1)[1]
    if ans == "yes":
        st(uid)["mode"] = "adult"
        st(uid)["adult_ok"] = True
        await c.answer("18+ включено")
        await c.message.answer("Ок. Отправь фото 📸")
    else:
        st(uid)["mode"] = "clean"
        st(uid)["adult_ok"] = False
        await c.answer("Без мата")
        await c.message.answer("Ок. Отправь фото 📸")

@dp.callback_query(F.data.startswith("len:"))
async def on_len(c: CallbackQuery):
    uid = c.from_user.id
    st(uid)["length"] = c.data.split(":", 1)[1]
    await c.answer("Ок")

    if st(uid).get("analysis"):
        ok, msg = can_request(uid)
        if not ok:
            await c.message.answer(msg)
            return

        wait_msg = await c.message.answer("⏳ Подбираю подпись под фото...")
        try:
            mark_request(uid)
            cap = generate_caption(st(uid)["analysis"], st(uid)["gender"], st(uid)["length"], st(uid)["mode"])
        except Exception:
            cap = pick_fallback(uid)

        try:
            await wait_msg.delete()
        except Exception:
            pass

        await c.message.answer(cap, reply_markup=actions_kb(uid))

@dp.callback_query(F.data == "nav:gender")
async def nav_gender(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери стиль:", reply_markup=gender_kb())

@dp.callback_query(F.data == "nav:mode")
async def nav_mode(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери режим:", reply_markup=mode_kb())

@dp.message(F.photo)
async def on_photo(m: Message):
    uid = m.from_user.id
    s = st(uid)

    ok, msg = can_request(uid)
    if not ok:
        await m.answer(msg)
        return

    wait_msg = await m.answer("⏳ Подбираю подпись под фото...")

    try:
        data_url = await photo_to_data_url(m)

        try:
            analysis = analyze_image(data_url)
        except Exception:
            analysis = {"mood": "универсально", "scene": "фото", "colors": "нейтрально", "vibe_tags": ["aesthetic"], "safe": "yes"}

        s["analysis"] = analysis

        if analysis.get("safe") == "no":
            try:
                await wait_msg.delete()
            except Exception:
                pass
            await m.answer("Не могу сделать подпись для такого изображения. Пришли другое фото 🙂")
            return

        try:
            mark_request(uid)
            cap = generate_caption(analysis, s["gender"], s["length"], s["mode"])
        except Exception:
            cap = pick_fallback(uid)

        try:
            await wait_msg.delete()
        except Exception:
            pass

        await m.answer(cap, reply_markup=actions_kb(uid))

    except Exception:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await m.answer(pick_fallback(uid), reply_markup=actions_kb(uid))

@dp.callback_query(F.data == "gen:next")
async def gen_next(c: CallbackQuery):
    uid = c.from_user.id
    s = st(uid)
    await c.answer()

    if not s.get("analysis"):
        await c.message.answer("Сначала отправь фото 📸")
        return

    ok, msg = can_request(uid)
    if not ok:
        await c.message.answer(msg)
        return

    wait_msg = await c.message.answer("⏳ Подбираю подпись под фото...")

    try:
        mark_request(uid)
        cap = generate_caption(s["analysis"], s["gender"], s["length"], s["mode"])
    except Exception:
        cap = pick_fallback(uid)

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await c.message.answer(cap, reply_markup=actions_kb(uid))

@dp.message()
async def other(m: Message):
    await m.answer("Отправь фото 📸 или нажми /start")


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
