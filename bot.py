import asyncio
import sqlite3
import requests
import random
import logging
import os
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, types, html
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# === НАЛАШТУВАННЯ ===
# Токени підтягуються зі змінних середовища (GitHub Secrets, хостинг тощо)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID") 

DB_NAME = "vru_monitor.db"
CHECK_INTERVAL_MINUTES = 30 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not TELEGRAM_TOKEN:
    raise ValueError("Помилка: TELEGRAM_TOKEN не знайдено у змінних середовища!")

vru_token = None
vru_token_date = None

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# === БАЗА ДАНИХ (SQLite) ===
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documents (
                nreg TEXT PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

def add_user(chat_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        try:
            conn.execute('INSERT INTO users (chat_id) VALUES (?)', (chat_id,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_user(chat_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))
        conn.commit()

def get_all_users():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT chat_id FROM users')
        return [row[0] for row in cursor.fetchall()]

def is_document_processed(nreg: str) -> bool:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM documents WHERE nreg = ?', (nreg,))
        return cursor.fetchone() is not None

def mark_document_processed(nreg: str):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('INSERT INTO documents (nreg) VALUES (?)', (nreg,))
        conn.commit()


# === ЛОГІКА РОБОТИ З API ВРУ ===
def get_valid_vru_token():
    global vru_token, vru_token_date
    now = datetime.now()
    
    if not vru_token or not vru_token_date or vru_token_date.date() != now.date():
        try:
            response = requests.get("https://data.rada.gov.ua/api/token", timeout=10)
            if response.status_code == 200:
                vru_token = response.text.strip()
                vru_token_date = now
                logger.info(f"Отримано новий токен ВРУ: {vru_token}")
            else:
                logger.error(f"Помилка отримання токена: {response.status_code}")
        except Exception as e:
            logger.error(f"Помилка підключення до API ВРУ: {e}")
            
    return vru_token

async def check_new_laws():
    logger.info("Запуск перевірки оновлень ВРУ...")
    token = get_valid_vru_token()
    if not token:
        logger.error("Немає токена, скасування перевірки.")
        return

    headers = {
        "User-Agent": token,
        "Accept": "application/json"
    }

    try:
        list_url = "https://data.rada.gov.ua/laws/main/r/docs.json"
        list_resp = await asyncio.to_thread(requests.get, list_url, headers=headers, timeout=15)
        
        if list_resp.status_code != 200:
            logger.error(f"Не вдалося отримати список: {list_resp.status_code}")
            return

        documents = list_resp.json()
        new_nregs = []

        for item in documents:
            nreg = item.get('nreg') if isinstance(item, dict) else str(item)
            if nreg and not is_document_processed(nreg):
                new_nregs.append(nreg)

        if not new_nregs:
            logger.info("Нових документів немає.")
            return

        logger.info(f"Знайдено нових документів: {len(new_nregs)}")
        users = get_all_users()
        
        for nreg in new_nregs[:20]: 
            delay = random.uniform(5.0, 7.0)
            await asyncio.sleep(delay)

            card_url = f"https://data.rada.gov.ua/laws/card/{nreg}.json"
            card_resp = await asyncio.to_thread(requests.get, card_url, headers=headers, timeout=15)
            
            if card_resp.status_code == 200:
                card_data = card_resp.json()
                doc_info = card_data.get('doc', {})
                
                title = doc_info.get('nazva', 'Без назви')
                doc_url = f"https://data.rada.gov.ua/laws/show/{nreg}"
                
                msg_text = (
                    f"🏛 <b>Оновлення законодавства</b>\n\n"
                    f"<b>Назва:</b> {html.quote(title)}\n"
                    f"<b>Номер:</b> {html.quote(nreg)}\n\n"
                    f"📄 <a href='{doc_url}'>Переглянути документ</a>"
                )

                # Публікація в канал
                if CHANNEL_ID:
                    try:
                        await bot.send_message(CHANNEL_ID, msg_text)
                    except Exception as e:
                        logger.error(f"Помилка відправки в канал: {e}")

                # Розсилка підписникам бота
                for chat_id in users:
                    try:
                        await bot.send_message(chat_id, msg_text)
                    except Exception as e:
                        logger.error(f"Помилка відправки користувачу {chat_id}: {e}")
                
            mark_document_processed(nreg)

    except Exception as e:
        logger.error(f"Помилка під час перевірки оновлень: {e}")


# === ХЕНДЛЕРИ ТЕЛЕГРАМ БОТА ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    is_new = add_user(message.chat.id)
    if is_new:
        await message.answer(
            "👋 Привіт! Ви підписалися на моніторинг оновлень законодавства України.\n\n"
            "Щойно з'явиться новий документ у базі Верховної Ради, ви отримаєте сповіщення.\n"
            "Щоб відписатися, надішліть /stop"
        )
    else:
        await message.answer("Ви вже підписані на розсилку! Очікуйте на нові сповіщення. 🏛")

@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    remove_user(message.chat.id)
    await message.answer("❌ Ви відписалися від розсилки. Щоб відновити її, надішліть /start")


# === ЗАПУСК ===
async def main():
    init_db()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(check_new_laws, 'interval', minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()
    
    logger.info("Бот запущений...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Роботу бота завершено.")
