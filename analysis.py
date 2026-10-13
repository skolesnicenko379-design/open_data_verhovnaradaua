"""Відбір змістовних законопроєктів і короткий аналітичний текст."""
from __future__ import annotations

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "openai/gpt-4o-mini"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Це відсів вузьких процедурних законопроєктів, а не фільтр за політичною позицією.
TECHNICAL_TITLE_PATTERNS = [
    r"про внесення змін до порядку денного",
    r"про внесення змін до календарного плану",
    r"про затвердження календарного плану",
    r"про затвердження персонального складу",
    r"про утворення тимчасової слідчої комісії",
    r"про утворення тимчасової спеціальної комісії",
    r"про дострокове припинення повноважень народного депутата",
    r"про призначення .* на посаду",
    r"про звільнення .* з посади",
    r"про направлення депутатського запиту",
    r"про кошторис верховної ради україни",
]
TECHNICAL_PATTERNS = [re.compile(p, re.I) for p in TECHNICAL_TITLE_PATTERNS]

POLICY_TERMS = (
    "подат", "бюджет", "державн", "місцев", "громад", "соціаль", "пенсі",
    "заробіт", "праці", "осві", "медич", "охорон", "безпек", "оборон",
    "військ", "ветеран", "бізнес", "підприєм", "економ", "енерг", "земель",
    "суд", "правосуд", "кримін", "адміністративн", "правопоруш", "поліці",
    "прокуратур", "корупц", "державн служ", "вибор", "парті", "медіа",
    "інформац", "цифров", "персональн дан", "інвалід", "довкіл", "еколог",
    "транспорт", "житлов", "комуналь", "митн", "інвестиці", "ліценз",
    "регулюван", "відповідальн", "пільг", "допомог", "державн підтрим",
)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\x60{3}(?:json)?\s*", "", text)
    text = re.sub(r"\s*\x60{3}$", "", text)
    return text.strip()


def is_obviously_technical(title: str) -> bool:
    return any(pattern.search(title or "") for pattern in TECHNICAL_PATTERNS)


def _fallback_analysis(bill: dict) -> dict:
    """Працює без LLM-ключа: коротко описує предмет, не вигадуючи юридичних наслідків."""
    title = bill.get("title", "").strip()
    subject = re.sub(r"^(Проєкт|Проект) Закону\s+", "", title, flags=re.I).strip()
    subject = subject[:360].rstrip(" .")
    source = re.sub(r"\s+", " ", bill.get("source_text", "")).strip()
    source_sentence = ""
    if source:
        match = re.search(r"(.{60,360}?[.!?])(?:\s|$)", source)
        if match:
            source_sentence = match.group(1).strip()
    if source_sentence:
        analysis = (
            f"Предмет ініціативи — {subject[:180]}. У супровідних матеріалах зазначено: "
            f"«{source_sentence[:260]}». Для оцінки практичного ефекту потрібно зіставити запропоновані норми "
            "з чинною редакцією відповідного закону."
        )
    else:
        analysis = (
            f"Предмет ініціативи — {subject[:220]}. За самою назвою неможливо достовірно визначити, "
            "які саме права, обов’язки або бюджетні видатки зміняться; для цього потрібен текст законопроєкту "
            "та пояснювальна записка."
        )
    return {
        "publish": True,
        "headline": subject[:150] or title[:150],
        "summary": f"Документ стосується питання «{subject[:300]}».",
        "analysis": analysis,
        "reason": "Автоматичний резервний режим без OPENROUTER_API_KEY.",
    }


def analyze_bill(bill: dict) -> dict:
    title = bill.get("title", "")
    if is_obviously_technical(title):
        return {"publish": False, "reason": "Процедурна або технічна ініціатива."}

    if not OPENROUTER_API_KEY:
        # Без LLM не відкидаємо законопроєкт лише через нечітку назву.
        # Вузько процедурні документи вже відсічені вище.
        return _fallback_analysis(bill)

    prompt_data = {
        "title": title,
        "number": bill.get("number"),
        "registration_date": bill.get("date"),
        "official_card": bill.get("card_url"),
        "initiators": bill.get("authors", []),
        "initiator_type": bill.get("sponsor", ""),
        "source_material_type": bill.get("source_text_type", ""),
        "source_text": (bill.get("source_text") or "")[:14000],
    }
    system_prompt = """
Ти редактор українського парламентського моніторингу. Відбирай тільки змістовні законопроєкти, які можуть змінити державну політику, права/обов'язки громадян, податки, бюджет, правила для бізнесу, повноваження органів влади, безпеку, соціальні гарантії або інші суспільно важливі сфери.
Не публікуй вузько процедурні, кадрові та суто організаційні ініціативи. Не відкидай законопроєкт лише через широку чи технічну назву, якщо джерельний текст свідчить про значні наслідки.
Не вигадуй фактів, норм, сум, наслідків або мотивів. Якщо надано лише назву, прямо познач обмеження. Відрізняй заявлену мету авторів від власної оцінки ймовірного ефекту.
Поверни ЛИШЕ валідний JSON з ключами:
publish (boolean), headline (короткий український заголовок без номера), summary (1 речення про суть), analysis (один абзац до 80 слів із механізмом зміни та можливим ефектом, без загальних фраз), reason (коротка причина рішення).
Якщо тексту недостатньо для змістовного аналізу, publish=false.
"""
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/skolesnicenko379-design/open_data_verhovnaradaua",
                "X-Title": "Моніторинг законопроєктів ВРУ",
            },
            json={
                "model": OPENROUTER_MODEL,
                "temperature": 0.15,
                "max_tokens": 650,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_data, ensure_ascii=False)},
                ],
            },
            timeout=45,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        result = json.loads(_strip_code_fence(raw))
        if not isinstance(result.get("publish"), bool):
            raise ValueError("LLM JSON does not contain boolean publish")
        if result["publish"] and (not result.get("headline") or not result.get("analysis")):
            raise ValueError("LLM response is missing headline or analysis")
        result["headline"] = str(result.get("headline", "")).strip()[:180]
        result["summary"] = str(result.get("summary", "")).strip()[:500]
        result["analysis"] = str(result.get("analysis", "")).strip()[:1000]
        return result
    except Exception as exc:
        logger.exception("LLM-аналітика не спрацювала для №%s: %s", bill.get("number"), exc)
        # Якщо модель недоступна, не замінюємо аналіз вигаданими висновками.
        return {"publish": False, "reason": "Не вдалося отримати перевірюваний аналітичний текст."}
