"""Telegram-бот для моніторингу змістовних законопроєктів Верховної Ради."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from html import escape

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from analysis import analyze_bill
from vru_api import get_author_affiliation, get_bill_details, list_recent_bills

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
STATE_FILE = "state.json"
MAX_PROCESSED_DOCS = 3000
MAX_DOCS_PER_RUN = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задано TELEGRAM_TOKEN у Secrets репозиторію.")

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def load_state() -> dict:
    default = {
        "users": [],
        "processed_documents": [],
        "last_update_id": 0,
        "author_affiliations": {},
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            loaded = json.load(file)
        if not isinstance(loaded, dict):
            return default
        for key, value in default.items():
            loaded.setdefault(key, value)
        return loaded
    except (json.JSONDecodeError, OSError) as exc:
        logger.exception("Не вдалося прочитати %s: %s", STATE_FILE, exc)
        return default


def save_state(state: dict) -> None:
    state["processed_documents"] = list(dict.fromkeys(state.get("processed_documents", [])))[-MAX_PROCESSED_DOCS:]
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


async def process_pending_commands(state: dict) -> None:
    offset = int(state.get("last_update_id", 0)) + 1
    try:
        updates = await bot.get_updates(
            offset=offset,
            timeout=0,
            allowed_updates=["message"],
        )
    except Exception:
        logger.exception("Не вдалося отримати команди Telegram")
        return

    users = {int(user_id) for user_id in state.get("users", [])}
    for update in updates:
        state["last_update_id"] = update.update_id
        message = update.message
        if not message or not message.text:
            continue
        command = message.text.split()[0].split("@")[0].lower()
        chat_id = message.chat.id
        if command == "/start":
            if chat_id not in users:
                users.add(chat_id)
                await bot.send_message(
                    chat_id,
                    "Ви підписалися на моніторинг змістовних законопроєктів Верховної Ради.\n\n"
                    "Бот надсилатиме короткий опис ініціатив, авторів, їхніх фракцій і стислий аналіз.\n"
                    "Щоб відписатися, надішліть /stop.",
                )
            else:
                await bot.send_message(chat_id, "Ви вже підписані на сповіщення. Надішліть /stop, щоб відписатися.")
        elif command == "/stop":
            users.discard(chat_id)
            await bot.send_message(chat_id, "Підписку вимкнено. Щоб відновити її, надішліть /start.")
        elif command == "/help":
            await bot.send_message(chat_id, "/start — підписатися на сповіщення\n/stop — відписатися")
    state["users"] = sorted(users)


async def _affiliations_for_bill(bill: dict, state: dict) -> list[str]:
    cache = state.setdefault("author_affiliations", {})
    profiles = bill.get("author_profiles", [])
    if not profiles:
        return bill.get("authors", []) or ["Не зазначено"]

    result = []
    for profile in profiles:
        name = profile["name"]
        url = profile["url"]
        if url not in cache:
            try:
                cache[url] = await asyncio.to_thread(get_author_affiliation, url)
            except Exception:
                logger.exception("Помилка профілю депутата: %s", url)
                cache[url] = "фракцію/партію не вдалося підтвердити"
        affiliation = cache[url]
        result.append(f"{name} — {affiliation}")
    return result or ["Не зазначено"]


def _post_text(bill: dict, analysis: dict, author_lines: list[str]) -> str:
    title = escape(analysis.get("headline") or bill.get("title") or "Законопроєкт")
    number = escape(str(bill.get("number") or "не зазначено"))
    date = escape(bill.get("date") or "не зазначено")
    summary = escape((analysis.get("summary") or "").strip())
    body = escape((analysis.get("analysis") or "").strip())
    authors = "\n".join(f"• {escape(line)}" for line in author_lines)
    source = escape(bill.get("card_url", ""), quote=True)

    parts = [
        f"<b>{title}</b>",
        f"<b>Дата реєстрації:</b> {date} | <b>№</b> {number}",
        "<b>Автори та фракції:</b>",
        authors,
    ]
    if summary:
        parts.append(f"<b>Суть:</b> {summary}")
    if body:
        parts.append(f"<b>Аналіз:</b> {body}")
    parts.append(f'🔎 <a href="{source}">Офіційна картка законопроєкту</a>')
    result = "\n\n".join(part for part in parts if part)
    # Telegram message cap is 4096 characters; keep a margin for HTML parsing.
    if len(result) > 3900:
        result = result[:3700] + f'…\n\n🔎 <a href="{source}">Офіційна картка законопроєкту</a>'
    return result


async def _send_post(text: str, state: dict) -> bool:
    sent = False
    if CHANNEL_ID:
        try:
            await bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
            sent = True
            logger.info("Опубліковано допис у каналі %s", CHANNEL_ID)
        except Exception:
            logger.exception("Не вдалося опублікувати допис у каналі")
    for chat_id in state.get("users", []):
        try:
            await bot.send_message(int(chat_id), text, disable_web_page_preview=True)
            sent = True
        except Exception:
            logger.warning("Не вдалося надіслати допис користувачу %s", chat_id, exc_info=True)
    if not sent:
        logger.warning("Допис не доставлено: перевір CHANNEL_ID і права бота в каналі.")
    return sent


async def check_new_bills(state: dict) -> None:
    logger.info("Перевірка нових законопроєктів ВРУ")
    try:
        listed = await asyncio.to_thread(list_recent_bills)
    except Exception:
        logger.exception("Не вдалося отримати перелік законопроєктів")
        return

    processed = set(str(value) for value in state.get("processed_documents", []))
    candidates = [item for item in listed if str(item.get("number")) not in processed]
    logger.info("Карток у переліку: %d; нових для обробки: %d", len(listed), len(candidates))

    handled = 0
    for item in candidates:
        if handled >= MAX_DOCS_PER_RUN:
            break
        number = str(item.get("number", "")).strip()
        if not number:
            continue
        try:
            bill = await asyncio.to_thread(get_bill_details, item)
        except Exception:
            logger.exception("Не вдалося прочитати картку №%s", number)
            continue
        if not bill:
            # Це не законопроєкт (наприклад, проєкт постанови), тож повторно перевіряти не потрібно.
            processed.add(number)
            handled += 1
            continue

        decision = await asyncio.to_thread(analyze_bill, bill)
        if not decision.get("publish"):
            logger.info("Пропущено №%s: %s", number, decision.get("reason", "не пройшов фільтр"))
            processed.add(number)
            handled += 1
            continue

        authors = await _affiliations_for_bill(bill, state)
        post = _post_text(bill, decision, authors)
        if await _send_post(post, state):
            processed.add(number)
            handled += 1
        else:
            # Не позначаємо як опублікований: бот повторить спробу під час наступного запуску.
            logger.error("№%s залишено необробленим через помилку доставки", number)

    state["processed_documents"] = list(processed)
    logger.info("Завершено. Опрацьовано нових карток: %d", handled)


async def main() -> None:
    state = load_state()
    try:
        await process_pending_commands(state)
        await check_new_bills(state)
    finally:
        save_state(state)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
