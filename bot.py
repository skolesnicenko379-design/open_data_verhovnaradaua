import asyncio
import json
import os
import random
import logging

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
# GitHub Actions runner щоразу має нову, незареєстровану IP-адресу, тому
# персональний токен з /api/token (який прив'язаний до зареєстрованої IP)
# тут не підходить. За документацією порталу JSON-списки та картки
# документів доступні БЕЗ реєстрації, якщо передавати User-Agent рядком
# "OpenData" — саме це й використовуємо.
VRU_USER_AGENT = "OpenData"

# /laws/main/r/docs.json повертає лише "dokid" (внутрішній числовий id), а
# НЕ "nreg" (системний номер документа), який потрібен для /laws/card і
# /laws/show. Натомість /laws/main/r[/pageN].json одразу віддає повні
# реквізити кожного документа (dokid, nreg, nazva, ...), тож окремий запит
# картки на кожен документ більше не потрібен.
LIST_URL_TEMPLATE = "https://data.rada.gov.ua/laws/main/r{page_suffix}.json"
MAX_LIST_PAGES = 3  # скільки сторінок списку переглядати за один прогін


def _extract_doc_fields(item: dict) -> dict:
    # Деякі відповіді порталу гніздять поля документа під ключем "doc",
    # інші віддають їх на верхньому рівні — підтримуємо обидва варіанти.
    return item.get("doc", item) if isinstance(item, dict) else {}


async def check_new_laws(state: dict):
    logger.info("Запуск перевірки оновлень ВРУ...")
    headers = {"User-Agent": VRU_USER_AGENT, "Accept": "application/json"}
    processed = set(state.get("processed_documents", []))
    users = state.get("users", [])

    try:
        new_docs = []  # [(nreg, nazva), ...] у порядку надходження

        for page in range(1, MAX_LIST_PAGES + 1):
            page_suffix = "" if page == 1 else f"/page{page}"
            list_url = LIST_URL_TEMPLATE.format(page_suffix=page_suffix)
            list_resp = await asyncio.to_thread(requests.get, list_url, headers=headers, timeout=15)

            if list_resp.status_code != 200:
                logger.error(f"Список (стор. {page}): {list_resp.status_code} — {list_resp.text[:300]!r}")
                break

            try:
                documents = list_resp.json()
            except ValueError:
                logger.error(f"Список (стор. {page}) не є JSON: {list_resp.text[:300]!r}")
                break

            if not documents:
                break  # далі сторінок немає

            page_had_new = False
            for item in documents:
                doc_info = _extract_doc_fields(item)
                nreg = doc_info.get("nreg")
                if not nreg or nreg in processed:
                    continue
                page_had_new = True
                nazva = doc_info.get("nazva", "Без назви")
                new_docs.append((nreg, nazva))

            # якщо на цій сторінці все вже оброблено раніше — подальші
            # (ще старіші) сторінки точно нічого нового не дадуть
            if not page_had_new:
                break

            if page < MAX_LIST_PAGES:
                await asyncio.sleep(random.uniform(5.0, 7.0))

        if not new_docs:
            logger.info("Нових документів немає.")
            return

        logger.info(f"Знайдено нових документів: {len(new_docs)}")

        for nreg, nazva in new_docs[:MAX_DOCS_PER_RUN]:
            doc_url = f"https://data.rada.gov.ua/laws/show/{nreg}"

            msg_text = (
                f"🏛 <b>Оновлення законодавства</b>\n\n"
                f"<b>Назва:</b> {html.quote(nazva)}\n"
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

        # документи поза MAX_DOCS_PER_RUN лишаємо необробленими навмисно —
        # вони підтягнуться в наступному прогоні (через 30 хв)

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
