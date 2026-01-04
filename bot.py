import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart

# ================= ENV =================
load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ================= AIROGRAM =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Бот работает 🔥 Отправь фото или текст.")

@dp.message()
async def echo(m: Message):
    await m.answer("Я жив. Бот работает.")

# ================= WEB SERVER =================
async def start_web_server():
    app = web.Application()

    async def health(request):
        return web.Response(text="OK")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Web server running on 0.0.0.0:{port}")

# ================= MAIN =================
async def main():
    await start_web_server()        # запускаем Render порт
    await dp.start_polling(bot)     # запускаем Telegram

if __name__ == "__main__":
    asyncio.run(main())
