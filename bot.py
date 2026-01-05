# bot.py — QuietBot / PicWords bot (aiogram v3) — ПРОД-ВЕРСИЯ (русский всегда)
# Фичи:
# - выбор стиля: женский/мужской/универсальный
# - режим: без мата / 18+ (мат разрешён только при подтверждении)
# - выбор типа подписи: В точку / Смешно / Красиво / Мудро / Дерзко
# - “думаю…” сообщение
# - пачка вариантов (топ + запас) и кнопка “Другая”
# - дневной лимит + антиспам
# - health web-server для Render (/ и /health)
# - защита от конфликтов polling (лок-файл lock) — чтобы не было TelegramConflictError

import os
import sys
import json
import base64
import random
import time
import fcntl
from pathlib import Path
from typing import Dict, Any, Tuple, List

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiohttp import web
from openai import OpenAI

from quotes import QUOTES

# ====== LOCK (анти-конфликт polling) ======
LOCK_FILE = "/tmp/quote_bot.lock"
_lock_fd = open(LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Another instance is already running. Exiting.")
    sys.exit(0)

# ===== настройки лимитов =====
DAILY_LIMIT = 20       # 20 генераций в день
COOLDOWN_SEC = 3.0     # не чаще 1 генерации в 3 секунды

# Надёжно грузим .env рядом с bot.py (локально). На Render берётся из Environment.
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Нет BOT_TOKEN (добавь в .env или Render Environment)")
if not OPENAI_API_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY (добавь в .env или Render Environment)")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# user_state[user_id] = {
#   "gender": "female|male|universal",
#   "length": "short|medium",
#   "mode": "clean|adult",
#   "adult_ok": bool,
#   "kind": "best|funny|beautiful|wise|bold",
#   "analysis": dict|None,
#   "last_batch": list[str],   # очередь готовых подписей
#   "used_quotes": set(),
#   "quota_day": "YYYY-MM-DD",
#   "quota_used": int,
#   "last_req_ts": float,
# }
user_state: Dict[int, Dict[str, Any]] = {}


# ===== util =====
def today_str() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def st(uid: int) -> Dict[str, Any]:
    if uid not in user_state:
        user_state[uid] = {
            "gender": "universal",
            "length": "medium",
            "mode": "clean",
            "adult_ok": False,
            "kind": "best",
            "analysis": None,
            "last_batch": [],
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

    now = time.time()
    dt = now - float(s.get("last_req_ts", 0.0))
    if dt < COOLDOWN_SEC:
        wait = max(1, int(COOLDOWN_SEC - dt + 0.999))
        return False, f"⏳ Подожди {wait} сек и попробуй ещё раз."

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


def kind_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 В точку", callback_data="kind:best")
    kb.button(text="😂 Смешно", callback_data="kind:funny")
    kb.button(text="✨ Красиво", callback_data="kind:beautiful")
    kb.button(text="🧠 Мудро", callback_data="kind:wise")
    kb.button(text="😈 Дерзко", callback_data="kind:bold")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def actions_kb(uid: int):
    left = quota_left(uid)
    kb = InlineKeyboardBuilder()

    kb.button(text=f"🔄 Другая (осталось {left})", callback_data="gen:next")

    kb.button(text="😂", callback_data="kind:funny")
    kb.button(text="✨", callback_data="kind:beautiful")
    kb.button(text="🧠", callback_data="kind:wise")
    kb.button(text="😈", callback_data="kind:bold")
    kb.button(text="🎯", callback_data="kind:best")

    kb.button(text="✍️ Коротко", callback_data="len:short")
    kb.button(text="🧾 Подлиннее", callback_data="len:medium")

    kb.button(text="🎭 Стиль", callback_data="nav:gender")
    kb.button(text="🧼/😈 Режим", callback_data="nav:mode")

    kb.adjust(1, 5, 2, 2)
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
    """
    Достаём вайб максимально полезно для подписи.
    Возвращаем JSON.
    """
    prompt = (
        "Проанализируй фото для подбора подписи в соцсети. Верни строго JSON без лишнего текста.\n"
        "{"
        "\"mood\":\"...\","
        "\"persona\":\"...\","
        "\"scene\":\"...\","
        "\"style\":\"...\","
        "\"colors\":\"...\","
        "\"vibe_tags\":[\"...\",\"...\",\"...\"],"
        "\"safe\":\"yes|no\""
        "}\n"
        "mood: 1-3 слова (например: спокойствие/ирония/романтика/драйв/задумчивость)\n"
        "persona: какое впечатление производит человек (например: уверенный интроверт/мягкий романтик/ироничный)\n"
        "scene: что за место/ситуация\n"
        "style: эстетика/одежда/настроение кадра\n"
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
        max_output_tokens=260,
    )
    t = r.output_text.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return {
            "mood": "спокойствие",
            "persona": "естественный вайб",
            "scene": "фото",
            "style": "минимализм",
            "colors": "нейтрально",
            "vibe_tags": ["aesthetic", "calm"],
            "safe": "yes",
        }


def generate_batch(analysis: Dict[str, Any], gender: str, length: str, mode: str, kind: str) -> List[str]:
    """
    Генерирует пачку вариантов и возвращает список строк (уже отфильтрованных).
    Мы будем показывать по одной, а “Другая” — следующую из очереди.
    """
    gender_style = {
        "female": "Женский стиль: эстетично, мягко, уверенно.",
        "male": "Мужской стиль: сдержанно, уверенно, можно чуть дерзко.",
        "universal": "Универсально: подходит всем, красиво и естественно."
    }[gender]

    len_style = "Очень коротко (2–6 слов)." if length == "short" else "Средняя длина (1–2 строки)."

    kind_style = {
        "best": "Максимально точно в вайб фото, звучит естественно, современно.",
        "funny": "Смешно и умно, лёгкая ирония, без кринжа.",
        "beautiful": "Очень красиво и эстетично, как идеальная подпись к фото.",
        "wise": "Мудро и глубоко, но без банальных мотивашек и пафоса.",
        "bold": "Дерзко и уверенно, но без токсичности и грубости.",
    }.get(kind, "Максимально точно в вайб фото, естественно.")

    if mode == "adult":
        tone = (
            "Разрешён мат (18+), но: без травли, без унижения групп людей, без угроз, "
            "без призывов к насилию, без сексуального контента."
        )
    else:
        tone = "Строго без мата и без грубых оскорблений."

    # Запрещаем кринж-клише
    banned = (
        "Запрещённые клише (не использовать): мечты, успех, будь собой, никогда не сдавайся, "
        "живи моментом, всё возможно, счастье в мелочах, внутренняя сила."
    )

    prompt = (
        "Ты — топовый автор подписей к фото на русском языке.\n"
        "Сделай так, будто ты на одном вайбе с человеком на фото.\n\n"
        f"{gender_style}\n"
        f"Тип: {kind_style}\n"
        f"Длина: {len_style}\n"
        f"Ограничения: {tone}\n"
        f"{banned}\n\n"
        "Задача: Сгенерируй 10 вариантов подписей (все разные), строго на русском.\n"
        "Правила:\n"
        "- без эмодзи\n"
        "- без кавычек\n"
        "- без хэштегов\n"
        "- не оценивать внешность\n"
        "- избегать пафоса и банальностей\n\n"
        "Контекст фото:\n"
        f"mood: {analysis.get('mood')}\n"
        f"persona: {analysis.get('persona')}\n"
        f"scene: {analysis.get('scene')}\n"
        f"style: {analysis.get('style')}\n"
        f"colors: {analysis.get('colors')}\n"
        f"tags: {', '.join(analysis.get('vibe_tags', []))}\n\n"
        "Верни строго JSON формата:\n"
        "{ \"captions\": [\"...\", \"...\", \"...\"] }\n"
    )

    r = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        max_output_tokens=280 if length == "short" else 420,
    )
    txt = r.output_text.strip()
    try:
        data = json.loads(txt)
        captions = data.get("captions", [])
        # чистим и фильтруем пустое/повторы
        clean = []
        seen = set()
        for c in captions:
            if not isinstance(c, str):
                continue
            c = c.strip().strip('"').strip()
            if not c:
                continue
            key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(c)
        return clean[:10] if clean else []
    except Exception:
        return []


def pop_or_generate(uid: int) -> str:
    """
    Берём следующую подпись из очереди пользователя.
    Если очереди нет — генерим новую пачку.
    """
    s = st(uid)
    if s.get("last_batch"):
        return s["last_batch"].pop(0)

    analysis = s.get("analysis")
    if not analysis:
        return pick_fallback(uid)

    try:
        batch = generate_batch(analysis, s["gender"], s["length"], s["mode"], s["kind"])
        if not batch:
            return pick_fallback(uid)
        s["last_batch"] = batch[1:]  # оставляем запас
        return batch[0]
    except Exception:
        return pick_fallback(uid)


# ===== handlers =====
@dp.message(CommandStart())
async def start(message: Message):
    s = st(message.from_user.id)
    s["analysis"] = None
    s["last_batch"] = []
    await message.answer(
        "Привет! Я делаю подписи под фото (на русском).\n\n"
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
        await c.message.answer("Шаг 3: какой тип подписи хочешь?", reply_markup=kind_kb())
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
        await c.message.answer("Шаг 3: какой тип подписи хочешь?", reply_markup=kind_kb())
    else:
        st(uid)["mode"] = "clean"
        st(uid)["adult_ok"] = False
        await c.answer("Без мата")
        await c.message.answer("Шаг 3: какой тип подписи хочешь?", reply_markup=kind_kb())


@dp.callback_query(F.data.startswith("kind:"))
async def on_kind(c: CallbackQuery):
    uid = c.from_user.id
    st(uid)["kind"] = c.data.split(":", 1)[1]
    st(uid)["last_batch"] = []  # сбрасываем очередь, чтобы новый стиль реально применился
    await c.answer("Ок")
    await c.message.answer("Шаг 4: отправь фото 📸")


@dp.callback_query(F.data.startswith("len:"))
async def on_len(c: CallbackQuery):
    uid = c.from_user.id
    st(uid)["length"] = c.data.split(":", 1)[1]
    st(uid)["last_batch"] = []
    await c.answer("Ок")

    # если уже было фото — пересоберём подпись под новый формат
    if st(uid).get("analysis"):
        ok, msg = can_request(uid)
        if not ok:
            await c.message.answer(msg)
            return

        wait_msg = await c.message.answer("⏳ Подбираю подпись под фото...")
        try:
            mark_request(uid)
            cap = pop_or_generate(uid)
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
            analysis = {
                "mood": "спокойствие",
                "persona": "естественный вайб",
                "scene": "фото",
                "style": "минимализм",
                "colors": "нейтрально",
                "vibe_tags": ["aesthetic", "calm"],
                "safe": "yes",
            }

        s["analysis"] = analysis
        s["last_batch"] = []

        if analysis.get("safe") == "no":
            try:
                await wait_msg.delete()
            except Exception:
                pass
            await m.answer("Не могу сделать подпись для такого изображения. Пришли другое фото 🙂")
            return

        try:
            mark_request(uid)
            cap = pop_or_generate(uid)
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
        cap = pop_or_generate(uid)
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


# ===== Web server for Render =====
async def start_web_server():
    app = web.Application()

    async def health(request):
        return web.Response(text="OK")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    print(f"✅ Web server started on 0.0.0.0:{port}")


async def main():
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
