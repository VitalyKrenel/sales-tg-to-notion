
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import requests
import re
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def find_notion_page(client_name):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    data = {
        "filter": {
            "property": "Имя клиента",
            "rich_text": {
                "contains": client_name
            }
        }
    }
    res = requests.post(url, headers=headers, json=data)
    results = res.json().get("results", [])
    return results[0]["id"] if results else None

def update_notion_page(page_id, summary):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {
        "properties": {
            "Lead status": {"select": {"name": "Встреча проведена"}},
            "Последний звонок": {"rich_text": [{"text": {"content": summary}}]}
        }
    }
    requests.patch(url, headers=headers, json=data)

def parse_call_message(text):
    match = re.match(r"Звонок с ([\w\s]+): (.+)", text)
    if match:
        name, summary = match.groups()
        return name.strip(), summary.strip()
    return None, None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    name, summary = parse_call_message(text)
    if name:
        page_id = find_notion_page(name)
        if page_id:
            update_notion_page(page_id, summary)
            await update.message.reply_text(f"Обновлена карточка клиента: {name}")
        else:
            await update.message.reply_text(f"Клиент {name} не найден в Notion.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
