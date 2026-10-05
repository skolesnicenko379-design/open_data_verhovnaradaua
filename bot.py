import asyncio
import json
import os
import random
import logging
from datetime import datetime

import requests
from aiogram import Bot, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# === НАЛАШТУВАННЯ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

STATE_FILE = "state.json"
MAX_PROCESSED_DOCS = 3000  # щоб файл стану не ріс нескінченно
MAX_DOCS_PER_RUN = 20      # скільки нових законів публікувати за один прогін

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not TELEGRAM_TOKEN:
    raise ValueError("Помилка: TELEGRAM_TOKEN не знайдено у змінних середовища!")

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# === СТАН (JSON-файл, що комітиться назад у репозиторій) ===
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Не вдалося прочитати {STATE_FILE}: {e}. Починаю з чистого стану.")
    return {
        "users": [],
        "processed_documents": [],
        "last_update_id": 0,
    }


def save_state(state: dict):
    # обрізаємо список оброблених документів, щоб файл не ріс вічно
    if len(state["processed_documents"]) > MAX_PROCESSED_DOCS:
        state["processed_documents"] = state["processed_documents"][-MAX_PROCESSED_DOCS:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# === ОБРОБКА КОМАНД /start і /stop ЧЕРЕЗ get_updates ===
async def process_pending_commands(state: dict):
    offset = state.get("last_update_id", 0) + 1 if state.get("last_update_id") else None
    try:
        updates = await bot.get_updates(offset=offset, timeout=0, allowed_updates=["message"])
    except Exception as e:
        logger.error(f"Помилка отримання оновлень Telegram: {e}")
        return

    users = set(state.get("users", []))

    for update in updates:
        state["last_update_id"] = update.update_id
        msg = update.message
        if not msg or not msg.text:
            continue

        chat_id = msg.chat.id
        text = msg.text.strip()

        if text == "/start":
            if chat_id not in users:
                users.add(chat_id)
                await bot.send_message(
                    chat_id,
                    "👋 Привіт! Ви підписалися на моніторинг оновлень законодавства України.\n\n"
                    "Щойно з'явиться новий документ у базі Верховної Ради, ви отримаєте сповіщення.\n"
                    "Щоб відписатися, надішліть /stop"
                )
            else:
                await bot.send_message(chat_id, "Ви вже підписані на розсилку! Очікуйте на нові сповіщення. 🏛")
        elif text == "/stop":
            users.discard(chat_id)
            await bot.send_message(chat_id, "❌ Ви відписалися від розсилки. Щоб відновити її, надішліть /start")

    state["users"] = list(users)


# === ЛОГІКА РОБОТИ З API ВРУ ===
def get_vru_token() -> str | None:
    try:
        response = requests.get("https://data.rada.gov.ua/api/token", timeout=10)
        if response.status_code == 200:
            return response.text.strip()
        logger.error(f"Помилка отримання токена: {response.status_code}")
    except Exception as e:
        logger.error(f"Помилка підключення до API ВРУ: {e}")
    return None


async def check_new_laws(state: dict):
    logger.info("Запуск перевірки оновлень ВРУ...")
    token = get_vru_token()
    if not token:
        logger.error("Немає токена, скасування перевірки.")
        return

    headers = {"User-Agent": token, "Accept": "application/json"}
    processed = set(state.get("processed_documents", []))

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
            if nreg and nreg not in processed:
                new_nregs.append(nreg)

        if not new_nregs:
            logger.info("Нових документів немає.")
            return

        logger.info(f"Знайдено нових документів: {len(new_nregs)}")
        users = state.get("users", [])

        for nreg in new_nregs[:MAX_DOCS_PER_RUN]:
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

                if CHANNEL_ID:
                    try:
                        await bot.send_message(CHANNEL_ID, msg_text)
                    except Exception as e:
                        logger.error(f"Помилка відправки в канал: {e}")

                for chat_id in users:
                    try:
                        await bot.send_message(chat_id, msg_text)
                    except Exception as e:
                        logger.error(f"Помилка відправки користувачу {chat_id}: {e}")

            processed.add(nreg)

    except Exception as e:
        logger.error(f"Помилка під час перевірки оновлень: {e}")
    finally:
        state["processed_documents"] = list(processed)


# === ОДИН ПРОХІД: читаємо стан -> обробляємо команди -> перевіряємо закони -> зберігаємо стан ===
async def main():
    state = load_state()
    try:
        await process_pending_commands(state)
        await check_new_laws(state)
    finally:
        save_state(state)
        await bot.session.close()
    logger.info("Прогін завершено.")


if __name__ == "__main__":
    asyncio.run(main())
