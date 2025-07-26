import logging
import time
import datetime
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)
import html
import os
from pymongo import MongoClient


# Carica dotenv SOLO se eseguito in locale
if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = MongoClient(MONGO_URI)
DB_NAME = os.getenv("DB_NAME")
db = mongo_client[DB_NAME]

if not BOT_TOKEN or not MONGO_URI:
    raise Exception("BOT_TOKEN o MONGO_URI non configurati!")


mongo_client = MongoClient(MONGO_URI)

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Connessione a MongoDB ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
users_col = db["users"]
warnings_col = db["warnings"]
groups_col = db["groups"]

# --- Utility DB ---

def add_user(user):
    users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name
        }},
        upsert=True
    )

def add_group(chat):
    groups_col.update_one(
        {"chat_id": chat.id},
        {"$set": {
            "chat_id": chat.id,
            "title": chat.title
        }},
        upsert=True
    )

def add_warning(user_id, chat_id, reason):
    warnings_col.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "timestamp": int(time.time()),
        "reason": reason
    })

def get_warnings(user_id):
    return list(warnings_col.find({"user_id": user_id}).sort("timestamp", -1))

def get_group_title(chat_id):
    group = groups_col.find_one({"chat_id": chat_id})
    return group["title"] if group else None

def get_top_warned_users(limit=10):
    pipeline = [
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit}
    ]
    return list(warnings_col.aggregate(pipeline))

def get_user_mention(user):
    safe_name = html.escape(user.first_name or "Utente")
    return f"<a href='tg://user?id={user.id}'>{safe_name}</a>"

async def is_admin(update: Update, user_id=None) -> bool:
    user = user_id or update.effective_user.id
    try:
        chat_member = await update.effective_chat.get_member(user)
        return chat_member.status in ['administrator', 'creator']
    except Exception:
        return False

def get_users_with_no_warnings():
    warned_user_ids = warnings_col.distinct("user_id")
    return list(users_col.find({"user_id": {"$nin": warned_user_ids}}))

def clear_warnings(user_id):
    warnings_col.delete_many({"user_id": user_id})

def safe_mention(user):
    name = html.escape(user.get("first_name", "Utente"))
    return f"<a href='tg://user?id={user['user_id']}'>{name}</a>"

# --- Comandi Bot ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot per la gestione dei punti.")
    add_user(update.message.from_user)
    add_group(update.effective_chat)

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono usare questo comando.")
        return

    if update.message.reply_to_message:
        warned_user = update.message.reply_to_message.from_user

        amount = 1
        reason = "Nessun motivo fornito."

        if context.args:
            try:
                if context.args[0].isdigit():
                    amount = int(context.args[0])
                    reason = " ".join(context.args[1:]) if len(context.args) > 1 else reason
                else:
                    reason = " ".join(context.args)
            except Exception:
                pass

        amount = max(1, min(amount, 100))
        add_user(warned_user)

        for _ in range(amount):
            add_warning(warned_user.id, update.effective_chat.id, reason)

        warnings = get_warnings(warned_user.id)
        warn_count = len(warnings)

        mention = get_user_mention(warned_user)
        escaped_reason = html.escape(reason)
        escaped_total = html.escape(str(warn_count))
        escaped_amount = html.escape(str(amount))

        message = (
            f"+ 1 punto per {mention}.\n"
            f"Totale punti: {escaped_total}"
        )

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)

    else:
        await update.message.reply_text("Rispondi a un messaggio per warnare l'utente.")

async def warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono usare questo comando.")
        return

    user_id_to_check = None
    user_to_mention = None

    if update.message.reply_to_message:
        user_id_to_check = update.message.reply_to_message.from_user.id
        user_to_mention = get_user_mention(update.message.reply_to_message.from_user)
    elif context.args:
        try:
            user_id_to_check = int(context.args[0])
            user_info = await context.bot.get_chat(user_id_to_check)
            user_to_mention = get_user_mention(user_info)
        except ValueError:
            await update.message.reply_text("ID utente non valido.")
            return
        except Exception:
            await update.message.reply_text("Utente non trovato.")
            return
    else:
        user_id_to_check = update.effective_user.id
        user_to_mention = get_user_mention(update.effective_user)

    warnings_data = get_warnings(user_id_to_check)
    if warnings_data:
        warning_text = f"Warning per {user_to_mention}:\n"
        for entry in warnings_data:
            dt_object = datetime.datetime.fromtimestamp(entry["timestamp"])
            group_title = get_group_title(entry["chat_id"])
            group_info = f" in {group_title}" if group_title else ""
            warning_text += f"- {dt_object.strftime('%Y-%m-%d %H:%M:%S')}{group_info}: {html.escape(entry['reason'])}\n"
        await update.message.reply_text(warning_text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            f"{user_to_mention} non ha punti.",
            parse_mode=ParseMode.HTML
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update "{update}" caused error "{context.error}"')

async def top_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = get_top_warned_users()
    if not top_users:
        await update.message.reply_text("Nessun warning registrato.")
        return

    message = "<b>Classifica utenti con più punti:</b>\n"
    for idx, entry in enumerate(top_users, start=1):
        user_data = users_col.find_one({"user_id": entry["_id"]})
        if user_data:
            name = html.escape(user_data.get("first_name", "Utente"))
            mention = f"<a href='tg://user?id={entry['_id']}'>{name}</a>"
        else:
            mention = f"ID {entry['_id']}"
        message += f"{idx}. {mention} — {entry['count']} warning\n"

    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

async def no_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_users_with_no_warnings()

    if not users:
        await update.message.reply_text("Tutti gli utenti hanno ricevuto almeno un warning.")
        return

    message = "<b>Utenti senza alcun punto:</b>\n"
    for idx, user in enumerate(users, start=1):
        name = html.escape(user.get("first_name", "Utente"))
        mention = f"<a href='tg://user?id={user['user_id']}'>{name}</a>"
        message += f"{idx}. {mention}\n"

    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

async def clear_warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono usare questo comando.")
        return

    user_id_to_clear = None
    user_mention = None

    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
        user_id_to_clear = user.id
        user_mention = get_user_mention(user)
    elif context.args:
        try:
            user_id_to_clear = int(context.args[0])
            user_info = await context.bot.get_chat(user_id_to_clear)
            user_mention = get_user_mention(user_info)
        except ValueError:
            await update.message.reply_text("ID utente non valido.")
            return
        except Exception:
            await update.message.reply_text("Utente non trovato.")
            return
    else:
        await update.message.reply_text("Rispondi a un messaggio o specifica l'ID dell'utente da pulire.")
        return

    clear_warnings(user_id_to_clear)
    await update.message.reply_text(
        f"Tutti i warning per {user_mention} sono stati rimossi.",
        parse_mode=ParseMode.HTML
    )

# --- Avvio Bot ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("punto", warn))
    app.add_handler(CommandHandler("warnings", warnings_command))
    app.add_handler(CommandHandler("topwarnings", top_warnings))
    app.add_handler(CommandHandler("nowarnings", no_warnings))
    app.add_handler(CommandHandler("clearwarnings", clear_warnings_command))

    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()
