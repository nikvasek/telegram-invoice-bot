
import os
import logging
import pdfplumber
import openai
from telegram import Update, Document, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from supabase import create_client
from datetime import datetime

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "invoices")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
openai.api_key = OPENAI_API_KEY
logging.basicConfig(level=logging.INFO)

def pdf_to_text(file_path):
    with pdfplumber.open(file_path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)

def extract_invoice_data_with_llm(text: str) -> dict:
    prompt = f"""
Ты — помощник-бухгалтер. Извлеки из текста инвойса структурированные данные для таблицы:
invoice_number, invoice_type (invoice/credit_note), date_issued (YYYY-MM-DD), period_start (YYYY-MM-DD), period_end (YYYY-MM-DD),
seller_name, seller_vat, provider_name, provider_vat, description, amount_net, amount_vat, amount_total, original_invoice, vat_percent.
Верни результат в формате JSON с этими ключами. Если данных нет — ставь null.
Текст инвойса:
{text}
"""
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    import json, re
    content = response.choices[0].message.content
    try:
        data = json.loads(content)
    except Exception:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            raise ValueError("LLM did not return valid JSON")
    return data

def upload_pdf_to_supabase(file_path: str, file_name: str) -> str:
    import uuid
    unique_name = f"{uuid.uuid4()}_{file_name}"
    with open(file_path, "rb") as f:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            unique_name,
            f,
            {
                "content-type": "application/pdf"
            }
        )
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{unique_name}", file_name

def clean_invoice_data(data: dict) -> dict:
    for key, value in data.items():
        if isinstance(value, str):
            if value.strip() == "":
                data[key] = None
            # Преобразуем даты к ISO-формату
            if value and ("date" in key or "issued" in key or "period" in key):
                try:
                    if "/" in value:
                        dt = datetime.strptime(value.strip(), "%d/%m/%Y")
                    else:
                        dt = datetime.strptime(value.strip(), "%Y-%m-%d")
                    data[key] = dt.strftime("%Y-%m-%d")
                except Exception:
                    data[key] = None
            # Преобразуем процент к числу
            if key == "vat_percent" and value:
                try:
                    data[key] = float(value.replace("%", "").replace(",", ".").strip())
                except Exception:
                    data[key] = None
        # Числовые поля
        if key.startswith("amount_") and value is not None:
            try:
                data[key] = float(str(value).replace(",", ".").replace("EUR", "").replace(" ", ""))
            except Exception:
                data[key] = None
    return data

def insert_invoice(data: dict):
    data = clean_invoice_data(data)
    supabase.table("invoices").insert(data).execute()

# === Telegram Handlers ===
main_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("загрузить инвойс")]],
    resize_keyboard=True
)

dashboard_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("Дашборд", web_app=WebAppInfo(url="https://concise-invoice-viewer.lovable.app"))]],
    resize_keyboard=True
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "для вызова дашборда нажми кнопку ниже.",
        reply_markup=dashboard_keyboard
    )

# Обработка нажатия кнопки
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "загрузить инвойс":
        await update.message.reply_text("Введи номер инвойса (например, ES-AEU-2025-407254):")
        # Сохраняем состояние пользователя (например, через context.user_data)
        context.user_data["awaiting_invoice_number"] = True
        return

    # Если ожидаем номер инвойса
    if context.user_data.get("awaiting_invoice_number"):
        invoice_number = update.message.text.strip()
        context.user_data["awaiting_invoice_number"] = False
        # Поиск инвойса в базе
        result = supabase.table("invoices").select("*").eq("invoice_number", invoice_number).execute()
        if result.data:
            file_url = result.data[0].get("file_url")
            original_file_name = result.data[0].get("original_file_name") or f"{invoice_number}.pdf"
            await update.message.reply_document(
                document=file_url,
                filename=original_file_name  # <-- вот это важно!
            )
        else:
            await update.message.reply_text("Инвойса с таким номером нет.")
        return

    # Остальные сообщения
    await update.message.reply_text("отправь PDF.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document: Document = update.message.document
    if document.mime_type != "application/pdf":
        await update.message.reply_text("Это не PDF-файл.")
        return

    file_name = document.file_name
    file_path = f"./{file_name}"

    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(file_path)

    # 1. Извлекаем текст
    text = pdf_to_text(file_path)

    # 2. Извлекаем данные через LLM
    try:
        invoice_data = extract_invoice_data_with_llm(text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка LLM: {e}")
        os.remove(file_path)
        return

    # 3. Загружаем PDF в Supabase Storage
    file_url, original_file_name = upload_pdf_to_supabase(file_path, file_name)
    invoice_data["file_url"] = file_url
    invoice_data["original_file_name"] = original_file_name

    # 4. Сохраняем в таблицу
    try:
        insert_invoice(invoice_data)
    except Exception as e:
        # Проверяем, что это ошибка уникальности по номеру инвойса
        if hasattr(e, "args") and e.args and "duplicate key value violates unique constraint" in str(e.args[0]):
            await update.message.reply_text(
                "Такой инвойс ест уже в базе."
            )
        else:
            await update.message.reply_text(f"Ошибка записи в базу: {e}")
        os.remove(file_path)
        return

    # Формируем красивое сообщение со всеми полями
    info = "\n".join(
        f"<b>{k}</b>: {v}" for k, v in invoice_data.items()
    )
    await update.message.reply_text(
        f"Инвойс загружен и сохранён в базу:\n\n{info}",
        parse_mode="HTML"
    )

    os.remove(file_path)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.run_polling()

if __name__ == "__main__":
    main()
