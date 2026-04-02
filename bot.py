import asyncio
import logging
import os
from datetime import datetime, timedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

DB_PATH = "db.sqlite"


# =========================
# DATABASE
# =========================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            client_id TEXT,
            client_secret TEXT,
            access_token TEXT,
            token_expires_at TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            telegram_id INTEGER,
            merchant_id INTEGER
        )
        """)

        await db.commit()


# =========================
# API CLIENT
# =========================

class APIClient:
    BASE_URL = "https://api.lemanapro.ru"

    @staticmethod
    async def get_token(client_id, client_secret):
        url = "https://developers.lemanapro.ru/b2b-authorization/"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "client_id": client_id,
                "client_secret": client_secret
            }) as resp:

                if resp.status != 200:
                    return None

                data = await resp.json()

                return {
                    "access_token": data.get("access_token"),
                    "expires_in": data.get("expires_in", 3600)
                }


# =========================
# TOKEN AUTO REFRESH
# =========================

async def refresh_tokens_loop():
    while True:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, client_id, client_secret, token_expires_at FROM merchants") as cursor:
                rows = await cursor.fetchall()

                for row in rows:
                    merchant_id, client_id, client_secret, expires = row

                    if not expires:
                        continue

                    expires = datetime.fromisoformat(expires)

                    if datetime.utcnow() > expires - timedelta(minutes=5):
                        token_data = await APIClient.get_token(client_id, client_secret)

                        if token_data:
                            new_exp = datetime.utcnow() + timedelta(seconds=token_data["expires_in"])

                            await db.execute("""
                                UPDATE merchants
                                SET access_token=?, token_expires_at=?
                                WHERE id=?
                            """, (
                                token_data["access_token"],
                                new_exp.isoformat(),
                                merchant_id
                            ))

            await db.commit()

        await asyncio.sleep(60)


# =========================
# DRIVER ACCESS CHECK
# =========================

async def driver_access_check_loop():
    while True:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, phone FROM drivers") as cursor:
                drivers = await cursor.fetchall()

                # тут можно дергать внешний API проверки
                # пока просто лог
                for d in drivers:
                    logging.info(f"Проверка водителя {d}")

        await asyncio.sleep(300)


# =========================
# UI
# =========================

def role_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Продавец", "Водитель")
    return kb
def merchant_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Добавить водителя", "Удалить водителя")
    kb.add("Список водителей")
    return kb


def driver_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Список заказов", "Поиск заказа")
    return kb


# =========================
# HANDLERS
# =========================

@dp.message_handler(commands=["start"])
async def start(msg: types.Message):
    await msg.answer("Выберите роль:", reply_markup=role_keyboard())


# -------- MERCHANT --------

@dp.message_handler(lambda m: m.text == "Продавец")
async def merchant_start(msg: types.Message):
    await msg.answer("Введите client_id:")
    dp.register_message_handler(get_client_secret, state=None)


async def get_client_secret(msg: types.Message):
    client_id = msg.text
    await msg.answer("Введите client_secret:")

    async def save_merchant(secret_msg: types.Message):
        client_secret = secret_msg.text

        token_data = await APIClient.get_token(client_id, client_secret)

        if not token_data:
            await secret_msg.answer("Ошибка авторизации")
            return

        expires = datetime.utcnow() + timedelta(seconds=token_data["expires_in"])

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
            INSERT INTO merchants (telegram_id, client_id, client_secret, access_token, token_expires_at)
            VALUES (?, ?, ?, ?, ?)
            """, (
                secret_msg.from_user.id,
                client_id,
                client_secret,
                token_data["access_token"],
                expires.isoformat()
            ))

            await db.commit()

        await secret_msg.answer("Успешно!", reply_markup=merchant_keyboard())

    dp.register_message_handler(save_merchant)


# -------- DRIVER --------

@dp.message_handler(lambda m: m.text == "Водитель")
async def driver_start(msg: types.Message):
    phone = msg.contact.phone_number if msg.contact else None

    if not phone:
        kb = ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(KeyboardButton("Отправить номер", request_contact=True))
        await msg.answer("Нужно подтвердить номер", reply_markup=kb)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM drivers WHERE phone=?", (phone,)) as cursor:
            driver = await cursor.fetchone()

            if not driver:
                await msg.answer("Нет доступа")
                return

    await msg.answer("Доступ разрешен", reply_markup=driver_keyboard())


# -------- ADD DRIVER --------

@dp.message_handler(lambda m: m.text == "Добавить водителя")
async def add_driver(msg: types.Message):
    await msg.answer("Введите номер телефона:")

    async def save_driver(phone_msg: types.Message):
        phone = phone_msg.text

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id FROM merchants WHERE telegram_id=?", (msg.from_user.id,)) as cur:
                merchant = await cur.fetchone()

            if not merchant:
                await phone_msg.answer("Ошибка")
                return

            await db.execute("""
            INSERT INTO drivers (phone, merchant_id)
            VALUES (?, ?)
            """, (phone, merchant[0]))

            await db.commit()

        await phone_msg.answer("Водитель добавлен")

    dp.register_message_handler(save_driver)


# =========================
# MAIN
# =========================

async def on_startup(dp):
    await init_db()
    asyncio.create_task(refresh_tokens_loop())
    asyncio.create_task(driver_access_check_loop())


if name == "main":
    executor.start_polling(dp, on_startup=on_startup)
