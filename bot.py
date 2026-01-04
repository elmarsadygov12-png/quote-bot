import os
import json
import base64
import time
import random
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from dotenv import load_dotenv
from aiohttp import web

import sys
import fcntl

LOCK_FILE = "/tmp/quote_bot.lock"

lock_fd = open(LOCK_FILE, "w")
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Another instance is already running. Exiting.")
    sys.exit(0)

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI

import storage


# =======================
# CONFIG
# =======================
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "20"))     # batches/day
COOLDOWN_SEC = float(os.getenv("COOLDOWN_SEC", "3"))  # sec between batches

# локально грузим .env (на Render лучше задавать env vars в Dashboard)
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Нет BOT_TOKEN (env var)")
if not OPENAI_API_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY (env var)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)


TONES = {
    "instagram": "Инстаграмный вайб: красиво, естественно, современно.",
    "romantic": "Романтика: мягко, тепло, нежно.",
    "bold": "Дерзко: уверенно, с характером, без грубости.",
    "minimal": "Минимализм: коротко, чисто, точно.",
    "poetic": "Поэтично: образно, атмосферно, но понятно.",
    "ironic": "Иронично: лёгкая самоирония, умно, без токсичности.",
    "motiv": "Мотивирующе: вдохновляюще, уверенно, без клише.",
    "cinema": "Киношно: как реплика из фильма/сцены, атмосферно.",
}

LANGS = {
    "ru": "Русский",
    "en": "English",
    "uk": "Українська",
    "kk": "Қазақша",
}


# =======================
# HELPERS
# =======================
def today_str() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def quota_left(user_id: int) -> int:
    q = storage.get_quota(user_id, today_str())
    return max(0, DAILY_LIMIT - int(q["used"]))


def can_request(user_id: int) -> Tuple[bool, str]:
    day = today_str()
    q = storage.get_quota(user_id, day)
    now_ts = time.time()

    dt = now_ts - float(q["last_ts"])
    if dt < COOLDOWN_SEC:
        wait = max(1, int(COOLDOWN_SEC - dt + 0.999))
        return False, f"⏳ Подожди {wait} сек и попробуй ещё раз."

    if int(q["used"]) >= DAILY_LIMIT:
        return False, f"Лимит {DAILY_LIMIT} генераций на сегодня исчерпан 😅\nПриходи завтра — лимит обновится."
    return True, ""


def mark_request(user_id: int):
    day = today_str()
    q = storage.get_quota(user_id, day)
    used = int(q["used"]) + 1
    total_used = int(q["total_used"]) + 1
    storage.update_quota(user_id, day, used=used, last_ts=time.time(), total_used=total_used)


def strip_caption(s: str) -> str:
    return s.strip().replace('"', "").replace("“", "").replace("”", "").strip()


def pick_fallback() -> str:
    pool = [
        "Красота — это тишина, которую замечают.",
        "Свет в кадре — значит, свет внутри.",
        "Просто момент, который хочется оставить.",
        "Там, где спокойно, там и красиво.",
        "Немного эстетики — и день лучше.",
    ]
    return random.choice(pool)


async def photo_to_data_url(message: Message) -> str:
    ph = message.photo[-1]
    f = await bot.get_file(ph.file_id)
    fb = await bot.download_file(f.file_path)
    raw = fb.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


# =======================
# KEYBOARDS (UX пункт 6)
# =======================
def kb_home(user_id: int):
    left = quota_left(user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📸 Сгенерировать (осталось {left})", callback_data="home:how")
    kb.button(text="⚙️ Настройки", callback_data="home:settings")
    kb.button(text="📌 Примеры", callback_data="home:examples")
    kb.button(text="📊 Статистика", callback_data="home:stats")
    kb.button(text="⭐️ Избранное", callback_data="home:favs")
    kb.adjust(1)
    return kb.as_markup()


def kb_settings(user_id: int):
    u = storage.get_or_create_user(user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎭 Стиль: {u['gender']}", callback_data="set:gender")
    kb.button(text=f"📏 Длина: {u['length']}", callback_data="set:length")
    kb.button(text=f"🧼/😈 Режим: {u['mode']}", callback_data="set:mode")
    kb.button(text=f"🗣 Язык: {LANGS.get(u['lang'], u['lang'])}", callback_data="set:lang")
    kb.button(text=f"💫 Тон: {u['tone']}", callback_data="set:tone")
    kb.button(text=f"🔥 Супер-режим: {'ON' if u['super_mode'] else 'OFF'}", callback_data="set:super")
    kb.button(text="⬅️ Назад", callback_data="nav:home")
    kb.adjust(1)
    return kb.as_markup()


def kb_gender():
    kb = InlineKeyboardBuilder()
    kb.button(text="👩 Женский", callback_data="gender:female")
    kb.button(text="👨 Мужской", callback_data="gender:male")
    kb.button(text="✨ Универсальный", callback_data="gender:universal")
    kb.button(text="⬅️ Назад", callback_data="home:settings")
    kb.adjust(1)
    return kb.as_markup()


def kb_length():
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Коротко", callback_data="length:short")
    kb.button(text="🧾 Средне", callback_data="length:medium")
    kb.button(text="⬅️ Назад", callback_data="home:settings")
    kb.adjust(1)
    return kb.as_markup()


def kb_mode():
    kb = InlineKeyboardBuilder()
    kb.button(text="🧼 Без мата", callback_data="mode:clean")
    kb.button(text="😈 Можно мат (18+)", callback_data="mode:adult")
    kb.button(text="⬅️ Назад", callback_data="home:settings")
    kb.adjust(1)
    return kb.as_markup()


def kb_adult_confirm():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Мне 18+ (включить)", callback_data="adult:yes")
    kb.button(text="❌ Нет (без мата)", callback_data="adult:no")
    kb.adjust(1)
    return kb.as_markup()


def kb_lang():
    kb = InlineKeyboardBuilder()
    for code, name in LANGS.items():
        kb.button(text=name, callback_data=f"lang:{code}")
    kb.button(text="⬅️ Назад", callback_data="home:settings")
    kb.adjust(1)
    return kb.as_markup()


def kb_tone():
    kb = InlineKeyboardBuilder()
    for k in ["instagram", "romantic", "bold", "minimal", "poetic", "ironic", "motiv", "cinema"]:
        kb.button(text=k, callback_data=f"tone:{k}")
    kb.button(text="⬅️ Назад", callback_data="home:settings")
    kb.adjust(1)
    return kb.as_markup()


def kb_variants(batch_id: str):
    # batch_id нужен чтобы понимать, к какому набору вариантов относится выбор
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣", callback_data=f"pick:{batch_id}:0")
    kb.button(text="2️⃣", callback_data=f"pick:{batch_id}:1")
    kb.button(text="3️⃣", callback_data=f"pick:{batch_id}:2")
    kb.button(text="🔁 Ещё 3", callback_data="gen:more")
    kb.button(text="⚙️ Настройки", callback_data="home:settings")
    kb.button(text="⬅️ Домой", callback_data="nav:home")
    kb.adjust(3, 2, 1)
    return kb.as_markup()


def kb_after_pick():
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐️ В избранное", callback_data="fav:add")
    kb.button(text="✍️ Сделай короче", callback_data="rewrite:shorter")
    kb.button(text="🧾 Сделай длиннее", callback_data="rewrite:longer")
    kb.button(text="🔁 Ещё 3 по этому фото", callback_data="gen:more")
    kb.button(text="⬅️ Домой", callback_data="nav:home")
    kb.adjust(1)
    return kb.as_markup()


# =======================
# OpenAI (пункт 3 качество)
# =======================
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
        data = json.loads(t)
        if not isinstance(data, dict):
            raise ValueError("not dict")
        return data
    except Exception:
        return {"mood": "универсально", "scene": "фото", "colors": "нейтрально", "vibe_tags": ["aesthetic"], "safe": "yes"}


def generate_candidates(analysis: Dict[str, Any], prefs: Dict[str, Any], n: int = 10) -> List[str]:
    gender_style = {
        "female": "Женский стиль: эстетично, мягко, уверенно.",
        "male": "Мужской стиль: сдержанно, уверенно, можно чуть дерзко.",
        "universal": "Универсально: подходит всем, красиво и естественно."
    }.get(prefs["gender"], "Универсально: подходит всем, красиво и естественно.")

    length = prefs["length"]
    len_style = "Очень коротко (до 8 слов)." if length == "short" else "Средняя длина (1–2 строки)."

    mode = prefs["mode"]
    if mode == "adult":
        tone_limits = (
            "Разрешён мат (18+), но без травли, без унижения групп, без угроз, "
            "без призывов к насилию, без сексуального контента."
        )
    else:
        tone_limits = "Строго без мата и без грубых оскорблений."

    tone = prefs["tone"]
    tone_style = TONES.get(tone, TONES["instagram"])

    lang = prefs["lang"]
    lang_name = LANGS.get(lang, lang)

    prompt = (
        f"Сгенерируй {n} РАЗНЫХ подписей для публикации под фото.\n"
        f"Язык: {lang_name}\n"
        f"{gender_style}\n"
        f"Тон: {tone_style}\n"
        f"Длина: {len_style}\n"
        f"Ограничения: {tone_limits}\n\n"
        "Формат ответа: строго JSON\n"
        "{\"captions\":[\"...\",\"...\",...]}\n\n"
        "Правила для каждой подписи:\n"
        "- без эмодзи\n"
        "- без кавычек\n"
        "- без хэштегов\n"
        "- без нумерации\n"
        "- без повторов между собой\n\n"
        f"Контекст:\n"
        f"mood: {analysis.get('mood')}\n"
        f"scene: {analysis.get('scene')}\n"
        f"colors: {analysis.get('colors')}\n"
        f"tags: {', '.join(analysis.get('vibe_tags', []))}\n"
    )

    r = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        max_output_tokens=700,
    )
    text = r.output_text.strip()
    try:
        data = json.loads(text)
        caps = data.get("captions", [])
        caps = [strip_caption(c) for c in caps if isinstance(c, str)]
        caps = [c for c in caps if c]
        return caps[:n] if caps else [pick_fallback()]
    except Exception:
        return [pick_fallback()]


def rerank_to_best3(candidates: List[str], analysis: Dict[str, Any], prefs: Dict[str, Any]) -> List[str]:
    # “Супер-режим”: выбираем лучшие 3 из 10 (вторая стадия)
    prompt = (
        "Ты редактор подписей. Выбери лучшие 3 из списка.\n"
        "Критерии: естественно, цепляет, без клише, соответствует вайбу.\n"
        "Запрещено: эмодзи, хэштеги, кавычки.\n"
        "Формат ответа строго JSON: {\"best\":[\"...\",\"...\",\"...\"]}\n\n"
        f"Контекст mood={analysis.get('mood')} scene={analysis.get('scene')} tags={analysis.get('vibe_tags', [])}\n\n"
        "Список:\n" + "\n".join([f"- {c}" for c in candidates[:10]])
    )
    r = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        max_output_tokens=250,
    )
    t = r.output_text.strip()
    try:
        data = json.loads(t)
        best = data.get("best", [])
        best = [strip_caption(x) for x in best if isinstance(x, str)]
        best = [b for b in best if b]
        if len(best) >= 3:
            return best[:3]
    except Exception:
        pass
    # fallback: просто первые 3
    return (candidates + [pick_fallback(), pick_fallback(), pick_fallback()])[:3]


def rewrite_caption(caption: str, how: str, prefs: Dict[str, Any]) -> str:
    lang = prefs["lang"]
    lang_name = LANGS.get(lang, lang)

    if how == "shorter":
        instr = "Сделай подпись короче, сохрани смысл и вайб. До 8 слов."
    else:
        instr = "Сделай подпись длиннее (1–2 строки), сохрани смысл и вайб."

    prompt = (
        f"Язык: {lang_name}\n"
        f"{instr}\n"
        "Правила:\n"
        "- без эмодзи\n"
        "- без кавычек\n"
        "- без хэштегов\n"
        "Верни только итоговую подпись.\n\n"
        f"Исходная подпись:\n{caption}"
    )
    r = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        max_output_tokens=120,
    )
    return strip_caption(r.output_text)


# =======================
# RUNTIME CACHE (только для UI-пакетов)
# =======================
# Чтобы помнить текущие варианты до выбора
# cache[user_id] = {"batch_id": str, "variants": [..], "last_caption": str}
cache: Dict[int, Dict[str, Any]] = {}


def make_batch_id() -> str:
    return f"{int(time.time()*1000)}"


# =======================
# WEB SERVER (/health) — must have для Render
# =======================
async def start_web_server():
    app = web.Application()

    async def health(_request):
        return web.Response(text="OK")

    async def root(_request):
        return web.Response(text="OK")

    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    log.info(f"✅ Web server started on 0.0.0.0:{port}")
    return runner


# =======================
# COMMANDS / HANDLERS
# =======================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    user_id = m.from_user.id
    storage.get_or_create_user(user_id)
    await m.answer(
        "Я делаю подписи под фото.\n\n"
        "Нажми «Сгенерировать» и пришли фото 📸",
        reply_markup=kb_home(user_id),
    )


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "Команды:\n"
        "/start — меню\n"
        "/help — помощь\n\n"
        "Как пользоваться:\n"
        "1) Настройки (тон/язык/длина/режим)\n"
        "2) Пришли фото\n"
        "3) Выбери 1/2/3 вариант\n"
        "4) Сохрани в избранное ⭐️"
    )


@dp.callback_query(F.data == "nav:home")
async def nav_home(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Главное меню:", reply_markup=kb_home(c.from_user.id))


@dp.callback_query(F.data == "home:how")
async def home_how(c: CallbackQuery):
    await c.answer()
    left = quota_left(c.from_user.id)
    await c.message.answer(
        f"Отправь фото 📸\n\nСегодня осталось генераций: {left}",
        reply_markup=kb_home(c.from_user.id),
    )


@dp.callback_query(F.data == "home:settings")
async def home_settings(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Настройки:", reply_markup=kb_settings(c.from_user.id))


@dp.callback_query(F.data == "home:examples")
async def home_examples(c: CallbackQuery):
    await c.answer()
    # примеры “пункт 6”
    await c.message.answer(
        "Примеры (без привязки к фото):\n"
        "- Тише, чем слова, но громче смысла.\n"
        "- Оставлю это здесь — на память.\n"
        "- В этом кадре всё на своём месте.\n"
        "- Ничего лишнего. Только момент.\n\n"
        "Хочешь — выбери тон в настройках и пришли фото 📸",
        reply_markup=kb_home(c.from_user.id),
    )


@dp.callback_query(F.data == "home:stats")
async def home_stats(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id
    q = storage.get_quota(user_id, today_str())
    favs = storage.count_favorites(user_id)
    await c.message.answer(
        f"📊 Статистика:\n"
        f"- Сегодня использовано: {q['used']}/{DAILY_LIMIT}\n"
        f"- Осталось сегодня: {quota_left(user_id)}\n"
        f"- Всего генераций: {q['total_used']}\n"
        f"- В избранном: {favs}",
        reply_markup=kb_home(user_id),
    )


@dp.callback_query(F.data == "home:favs")
async def home_favs(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id
    favs = storage.list_favorites(user_id, limit=10)
    if not favs:
        await c.message.answer("⭐️ Избранное пустое.", reply_markup=kb_home(user_id))
        return
    text = "⭐️ Последние сохранённые подписи:\n\n" + "\n\n".join([f"- {cap}" for _, cap in favs])
    await c.message.answer(text, reply_markup=kb_home(user_id))


# settings navigation
@dp.callback_query(F.data == "set:gender")
async def set_gender(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери стиль:", reply_markup=kb_gender())


@dp.callback_query(F.data.startswith("gender:"))
async def on_gender(c: CallbackQuery):
    await c.answer("Ок")
    gender = c.data.split(":", 1)[1]
    storage.update_user(c.from_user.id, gender=gender)
    await c.message.answer("Обновил стиль ✅", reply_markup=kb_settings(c.from_user.id))


@dp.callback_query(F.data == "set:length")
async def set_length(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери длину:", reply_markup=kb_length())


@dp.callback_query(F.data.startswith("length:"))
async def on_length(c: CallbackQuery):
    await c.answer("Ок")
    length = c.data.split(":", 1)[1]
    storage.update_user(c.from_user.id, length=length)
    await c.message.answer("Обновил длину ✅", reply_markup=kb_settings(c.from_user.id))


@dp.callback_query(F.data == "set:mode")
async def set_mode(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери режим:", reply_markup=kb_mode())


@dp.callback_query(F.data.startswith("mode:"))
async def on_mode(c: CallbackQuery):
    mode = c.data.split(":", 1)[1]
    if mode == "clean":
        storage.update_user(c.from_user.id, mode="clean", adult_ok=0)
        await c.answer("Ок")
        await c.message.answer("Режим: без мата ✅", reply_markup=kb_settings(c.from_user.id))
    else:
        await c.answer()
        await c.message.answer("Подтверди 18+:", reply_markup=kb_adult_confirm())


@dp.callback_query(F.data.startswith("adult:"))
async def on_adult(c: CallbackQuery):
    ans = c.data.split(":", 1)[1]
    if ans == "yes":
        storage.update_user(c.from_user.id, mode="adult", adult_ok=1)
        await c.answer("18+ включено")
        await c.message.answer("Режим 18+ включён ✅", reply_markup=kb_settings(c.from_user.id))
    else:
        storage.update_user(c.from_user.id, mode="clean", adult_ok=0)
        await c.answer("Без мата")
        await c.message.answer("Режим: без мата ✅", reply_markup=kb_settings(c.from_user.id))


@dp.callback_query(F.data == "set:lang")
async def set_lang(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери язык:", reply_markup=kb_lang())


@dp.callback_query(F.data.startswith("lang:"))
async def on_lang(c: CallbackQuery):
    await c.answer("Ок")
    lang = c.data.split(":", 1)[1]
    storage.update_user(c.from_user.id, lang=lang)
    await c.message.answer("Язык обновлён ✅", reply_markup=kb_settings(c.from_user.id))


@dp.callback_query(F.data == "set:tone")
async def set_tone(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери тон:", reply_markup=kb_tone())


@dp.callback_query(F.data.startswith("tone:"))
async def on_tone(c: CallbackQuery):
    await c.answer("Ок")
    tone = c.data.split(":", 1)[1]
    storage.update_user(c.from_user.id, tone=tone)
    await c.message.answer("Тон обновлён ✅", reply_markup=kb_settings(c.from_user.id))


@dp.callback_query(F.data == "set:super")
async def set_super(c: CallbackQuery):
    await c.answer()
    u = storage.get_or_create_user(c.from_user.id)
    new_val = 0 if int(u["super_mode"]) == 1 else 1
    storage.update_user(c.from_user.id, super_mode=new_val)
    await c.message.answer("Супер-режим переключён ✅", reply_markup=kb_settings(c.from_user.id))


# photo => generate 3 variants
@dp.message(F.photo)
async def on_photo(m: Message):
    user_id = m.from_user.id
    ok, msg = can_request(user_id)
    if not ok:
        await m.answer(msg)
        return

    wait_msg = await m.answer("⏳ Анализирую фото и делаю варианты...")

    try:
        mark_request(user_id)

        data_url = await photo_to_data_url(m)
        analysis = analyze_image(data_url)
        if analysis.get("safe") == "no":
            await wait_msg.delete()
            await m.answer("Не могу сделать подпись для такого изображения. Пришли другое фото 🙂")
            return

        storage.save_analysis(user_id, analysis)

        prefs = storage.get_or_create_user(user_id)
        candidates = generate_candidates(analysis, prefs, n=10)

        if int(prefs["super_mode"]) == 1 and len(candidates) >= 3:
            best3 = rerank_to_best3(candidates, analysis, prefs)
        else:
            best3 = candidates[:3] if len(candidates) >= 3 else (candidates + [pick_fallback(), pick_fallback()])[:3]

        batch_id = make_batch_id()
        cache[user_id] = {"batch_id": batch_id, "variants": best3, "last_caption": ""}

        await wait_msg.delete()
        text = "Выбери лучший вариант:\n\n" + "\n\n".join([f"{i+1}) {v}" for i, v in enumerate(best3)])
        await m.answer(text, reply_markup=kb_variants(batch_id))

    except Exception as e:
        log.exception("photo handler error: %s", e)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await m.answer(pick_fallback(), reply_markup=kb_home(user_id))


@dp.callback_query(F.data == "gen:more")
async def gen_more(c: CallbackQuery):
    user_id = c.from_user.id
    await c.answer()

    analysis = storage.load_analysis(user_id)
    if not analysis:
        await c.message.answer("Сначала отправь фото 📸", reply_markup=kb_home(user_id))
        return

    ok, msg = can_request(user_id)
    if not ok:
        await c.message.answer(msg)
        return

    wait_msg = await c.message.answer("⏳ Делаю ещё 3 варианта...")

    try:
        mark_request(user_id)
        prefs = storage.get_or_create_user(user_id)
        candidates = generate_candidates(analysis, prefs, n=10)

        if int(prefs["super_mode"]) == 1 and len(candidates) >= 3:
            best3 = rerank_to_best3(candidates, analysis, prefs)
        else:
            best3 = candidates[:3] if len(candidates) >= 3 else (candidates + [pick_fallback(), pick_fallback()])[:3]

        batch_id = make_batch_id()
        cache[user_id] = {"batch_id": batch_id, "variants": best3, "last_caption": ""}

        await wait_msg.delete()
        text = "Новые варианты:\n\n" + "\n\n".join([f"{i+1}) {v}" for i, v in enumerate(best3)])
        await c.message.answer(text, reply_markup=kb_variants(batch_id))

    except Exception as e:
        log.exception("gen_more error: %s", e)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await c.message.answer(pick_fallback(), reply_markup=kb_home(user_id))


@dp.callback_query(F.data.startswith("pick:"))
async def pick_variant(c: CallbackQuery):
    user_id = c.from_user.id
    await c.answer()

    parts = c.data.split(":")
    if len(parts) != 3:
        return
    batch_id, idx_s = parts[1], parts[2]
    idx = int(idx_s)

    st = cache.get(user_id)
    if not st or st.get("batch_id") != batch_id:
        await c.message.answer("Эти варианты устарели. Нажми «Ещё 3» или пришли фото заново.")
        return

    variants = st.get("variants", [])
    if idx < 0 or idx >= len(variants):
        return

    chosen = variants[idx]
    st["last_caption"] = chosen

    await c.message.answer(f"✅ Выбрано:\n{chosen}", reply_markup=kb_after_pick())


@dp.callback_query(F.data == "fav:add")
async def fav_add(c: CallbackQuery):
    user_id = c.from_user.id
    await c.answer()
    st = cache.get(user_id)
    caption = (st or {}).get("last_caption", "")
    if not caption:
        await c.message.answer("Сначала выбери вариант 1/2/3 🙂")
        return
    storage.add_favorite(user_id, caption)
    await c.message.answer("⭐️ Сохранил в избранное!", reply_markup=kb_home(user_id))


@dp.callback_query(F.data.startswith("rewrite:"))
async def rewrite(c: CallbackQuery):
    user_id = c.from_user.id
    await c.answer()

    st = cache.get(user_id)
    caption = (st or {}).get("last_caption", "")
    if not caption:
        await c.message.answer("Сначала выбери вариант 1/2/3 🙂")
        return

    ok, msg = can_request(user_id)
    if not ok:
        await c.message.answer(msg)
        return

    how = c.data.split(":", 1)[1]
    wait_msg = await c.message.answer("⏳ Переписываю...")

    try:
        mark_request(user_id)
        prefs = storage.get_or_create_user(user_id)
        new_cap = rewrite_caption(caption, how, prefs)
        st["last_caption"] = new_cap

        await wait_msg.delete()
        await c.message.answer(new_cap, reply_markup=kb_after_pick())
    except Exception as e:
        log.exception("rewrite error: %s", e)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await c.message.answer("Не получилось переписать, попробуй ещё раз.", reply_markup=kb_after_pick())


@dp.message()
async def other(m: Message):
    await m.answer("Отправь фото 📸 или нажми /start", reply_markup=kb_home(m.from_user.id))


async def main():
    storage.init_db()
    await start_web_server()
    await dp.start_polling(bot)



if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
