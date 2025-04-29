from telegram import Update, Chat
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import requests
import logging
import re
import os
from datetime import datetime

# Настройка логгера
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TG_BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_INTEGRATION_SECRET"]
DATABASE_ID = os.environ["SALES_CRM_DATABASE_ID"]

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def find_notion_page(client_name: str) -> str | None:
    norm_client = _normalize(client_name)
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"

    # 1) подтягиваем все страницы (можно задать page_size=100, добавить пагинацию при необходимости)
    res = requests.post(url, headers=headers, json={})
    pages = res.json().get("results", [])

    for page in pages:
        # 2) собираем всё текстовое содержимое свойства "Имя клиента"
        rich = page["properties"]["Name"]["title"]
        notion_name = "".join(rt.get("plain_text", "") for rt in rich)
        norm_page = _normalize(notion_name)

        # 3) сравниваем: client_in_page или page_in_client
        if norm_client in norm_page or norm_page in norm_client:
            return page["id"]

    return None


def update_notion_page(page_id: str, summary: str):
    # 1. Сначала GET, чтобы достать текущее Rich Text из поля Lead status
    url_page = f"https://api.notion.com/v1/pages/{page_id}"
    page = requests.get(url_page, headers=headers).json()
    rich = page["properties"]["Lead status"]["rich_text"]

    # 2. Выделяем дату из саммари (dd/mm/yyyy или dd/mm/yy) или берём сегодня
    match = re.search(r"(\d{2}/\d{2}/\d{2,4})", summary)
    if match:
        raw = match.group(1)
        fmt = "%d/%m/%Y" if len(raw.split("/")[-1]) == 4 else "%d/%m/%y"
        call_date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
    else:
        call_date = datetime.utcnow().strftime("%Y-%m-%d")

    # 3. Формируем новый массив rich_text для Lead status:
    #    сначала наша короткая запись, затем весь старый текст
    date_mention = {
        "type": "mention",
        "mention": {
            "type": "date",
            "date": {
                "start": call_date,
                "end": None
            }
        }
    }

    # 3b) текст после даты
    text_mention = {
        "type": "text",
        "text": {
            "content":
            " 🤖 Провели звонок с лидом. Саммари по звонку перенес в карточку с клиентом.\n\n"
        }
    }

    new_rich = [date_mention, text_mention] + rich
    # 4. PATCH — обновляем только поле Lead status
    data_page = {"properties": {"Lead status": {"rich_text": new_rich}}}
    requests.patch(url_page, headers=headers, json=data_page)

    # 5. POST — создаём комментарий к странице с полным саммари
    url_comments = "https://api.notion.com/v1/comments"
    data_comment = {
        "parent": {
            "page_id": page_id
        },
        "rich_text": [{
            "type": "text",
            "text": {
                "content": summary
            }
        }]
    }
    requests.post(url_comments, headers=headers, json=data_comment)


def _normalize(name: str) -> str:
    """
    Убираем протокол, www., всё после первого '/', 
    приводим к нижнему регистру и обрезаем пробелы/слэши.
    """
    n = name.lower().strip()
    n = re.sub(r'https?://(www\.)?', '', n)
    n = n.split('/', 1)[0]
    return n.rstrip('/')


def parse_call_message(text):
    if re.match(r"^\s*[\W_]*\s*(\d{2}/\d{2}/\d{2,4})", text):
        logger.info("Найдено сообщение с датой — считаем это саммари звонка.")
        return text.strip()
    else:
        logger.info(
            "Дата в формате dd/mm/yyyy не найдена — не саммари звонка.")
        return None


def extract_client_name(chat_title: str) -> str | None:
    """
    Извлекает название клиента из заголовка чата, в котором фигурирует WeDo.
    Ищет шаблоны вида:
    - 'Клиент + WeDo'
    - 'WeDo + Клиент'
    - 'Клиент x WeDo'
    - 'WeDo x Клиент'
    - 'Клиент & WeDo'
    - 'WeDo & Клиент'
    """
    if not chat_title:
        return None

    # Регулярные выражения на основе допустимых разделителей
    pattern_client_first = r'^(.*?)\s*[\+x&]\s*WeDo\b'
    pattern_client_last = r'\bWeDo\s*[\+x&]\s*(.*?)$'

    match = re.search(pattern_client_first, chat_title, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    match = re.search(pattern_client_last, chat_title, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_type = message.chat.type
    if chat_type not in [Chat.GROUP, Chat.SUPERGROUP]:
        logger.info(f"Игнорируем сообщение из чата типа {chat_type}")
        return

    text = message.text
    logger.info("▶️ Incoming message (first 2 paragraphs):\n%s",
                "\n\n".join(text.split("\n\n")[:2]))
    if not text:
        return

    logger.info(f"Сообщение из группы: {message.chat.title} — {text[:50]}")

    client_name = extract_client_name(message.chat.title or "")

    if not client_name:
        logger.warning(
            f"Не удалось извлечь название клиента из чата: {message.chat.title}"
        )
        return

    logger.info("🎯 Extracted client_name from chat '%s': %s",
                message.chat.title, client_name)

    summary = parse_call_message(text)
    if client_name and summary:
        page_id = find_notion_page(client_name)
        if page_id:
            update_notion_page(page_id, summary)
            # await message.reply_text(f"✅ Обновлено в Notion для {client_name}")
            logger.info(f"✅ Обновлено в Notion для {client_name}")
        else:
            logger.info(f"⚠️ Клиент '{client_name}' не найден в Notion")
    else:
        logger.info("🔜 Сообщение не похоже на саммари — пропускаем.")


# async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     text = update.message.text
#     name, summary = parse_call_message(text)
#     # logger.info(f"Получено сообщение: {text}")
#     # logger.info(f"Распознано имя: {name}, резюме: {summary}")

#     if name:
#         page_id = find_notion_page(name)
#         if page_id:
#             update_notion_page(page_id, summary)
#             await update.message.reply_text(
#                 f"Обновлена карточка клиента: {name}")
#         else:
#             await update.message.reply_text(
#                 f"Клиент {name} не найден в Notion.")

if __name__ == '__main__':
    logger.info("Запуск Telegram-бота...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
