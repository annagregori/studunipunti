import logging
import time
import datetime
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import html
import os

# --- Caricamento variabili d'ambiente ---
if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

if not BOT_TOKEN or not MONGO_URI:
    raise Exception("BOT_TOKEN o MONGO_URI non configurati!")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
users_col = db["users"]
points_col = db["points"]
groups_col = db["groups"]

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
        {"$set": {"chat_id": chat.id, "title": chat.title}},
        upsert=True
    )

def add_points(user_id, chat_id, reason, amount=1):
    points_col.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "timestamp": int(time.time()),
        "reason": reason,
        "amount": amount
    })

def get_points(user_id):
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}}
    ]
    result = list(points_col.aggregate(pipeline))
    return result[0]["total"] if result else 0

def get_group_title(chat_id):
    group = groups_col.find_one({"chat_id": chat_id})
    return group["title"] if group else None

def get_top_users(limit=10):
    pipeline = [
        {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}},
        {"$limit": limit}
    ]
    return list(points_col.aggregate(pipeline))

def get_users_with_no_points():
    users_with_points = points_col.distinct("user_id")
    return list(users_col.find({"user_id": {"$nin": users_with_points}}))

def clear_points(user_id):
    points_col.delete_many({"user_id": user_id})

def get_user_mention(user):
    safe_name = html.escape(user.first_name or "Utente")
    return f"<a href='tg://user?id={user.id}'>{safe_name}</a>"

def safe_mention(user):
    name = html.escape(user.get("first_name", "Utente"))
    return f"<a href='tg://user?id={user['user_id']}'>{name}</a>"

# --- Controllo admin ---
async def is_admin(update: Update, user_id=None) -> bool:
    user = user_id or update.effective_user.id
    try:
        member = await update.effective_chat.get_member(user)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

# --- Comandi ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot per la gestione dei punti attivo!")
    add_user(update.message.from_user)
    add_group(update.effective_chat)

async def give_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono assegnare punti.")
        return

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        add_user(target)
        amount = 1
        reason = "Ottimo contributo!"

        if context.args:
            try:
                if context.args[0].isdigit():
                    amount = int(context.args[0])
                    reason = " ".join(context.args[1:]) or reason
                else:
                    reason = " ".join(context.args)
            except Exception:
                pass

        amount = max(1, min(amount, 100))
        add_points(target.id, update.effective_chat.id, reason, amount)
        total = get_points(target.id)

        mention = get_user_mention(target)
        msg = f"✨ {mention} ha ricevuto <b>{amount}</b> punto{'i' if amount > 1 else ''}!\nTotale punti: <b>{total}</b>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Rispondi a un messaggio per assegnare punti.")

async def show_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    total = get_points(user.id)
    mention = get_user_mention(user)
    await update.message.reply_text(f"{mention} ha <b>{total}</b> punto{'i' if total != 1 else ''}.", parse_mode=ParseMode.HTML)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = get_top_users()
    if not top_users:
        await update.message.reply_text("Nessun punto assegnato ancora.")
        return

    msg = "<b>🏆 Classifica Punti</b>\n"
    for i, entry in enumerate(top_users, start=1):
        user_data = users_col.find_one({"user_id": entry["_id"]})
        name = html.escape(user_data.get("first_name", "Utente")) if user_data else f"ID {entry['_id']}"
        msg += f"{i}. <a href='tg://user?id={entry['_id']}'>{name}</a> — {entry['total']} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def no_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_users_with_no_points()
    if not users:
        await update.message.reply_text("Tutti hanno ricevuto almeno un punto!")
        return

    msg = "<b>Utenti senza punti:</b>\n"
    for i, user in enumerate(users, start=1):
        msg += f"{i}. {safe_mention(user)}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def clear_points_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono azzerare i punti.")
        return

    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
        clear_points(user.id)
        await update.message.reply_text(f"Punti azzerati per {get_user_mention(user)}.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Rispondi a un messaggio per azzerare i punti.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Errore: {context.error}")

# --- MAIN ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("punto", give_points))
    app.add_handler(CommandHandler("punti", show_points))
    app.add_handler(CommandHandler("classifica", leaderboard))
    app.add_handler(CommandHandler("senza_punti", no_points))
    app.add_handler(CommandHandler("azzera_punti", clear_points_command))
    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()
