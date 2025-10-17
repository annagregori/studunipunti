import logging
import asyncio
import datetime
import html
import os
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import Forbidden

# --- Carica dotenv solo in locale ---
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
members_col = db["members"]

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Utility DB ---
def add_or_update_member(user, chat, points_delta=0):
    now = datetime.datetime.utcnow()
    member = members_col.find_one({"user_id": user.id})

    group_info = {
        "chat_id": chat.id,
        "title": chat.title,
        "joined_at": now,
        "points": max(0, points_delta),
        "last_message_at": now
    }

    if member:
        members_col.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name
                }
            }
        )
        existing_group = next(
            (g for g in member.get("groups", []) if g["chat_id"] == chat.id),
            None
        )
        if existing_group:
            members_col.update_one(
                {"user_id": user.id, "groups.chat_id": chat.id},
                {
                    "$inc": {
                        "groups.$.points": points_delta,
                        "total_points": points_delta
                    },
                    "$set": {"groups.$.last_message_at": now}
                }
            )
        else:
            members_col.update_one(
                {"user_id": user.id},
                {
                    "$push": {"groups": group_info},
                    "$inc": {"total_points": points_delta}
                }
            )
    else:
        members_col.insert_one({
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "groups": [group_info],
            "total_points": points_delta,
            "created_at": now
        })

# --- Controllo admin ---
async def is_admin(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    try:
        member = await chat.get_member(user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# --- Comandi Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_member(update.message.from_user, update.effective_chat)
    await update.message.reply_text("🤖 Ciao! Sto tracciando utenti e punti globalmente.")

async def punto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono assegnare punti.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Rispondi a un messaggio per assegnare punti.")
        return

    user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    points = 1
    if context.args and context.args[0].isdigit():
        points = int(context.args[0])

    add_or_update_member(user, chat, points_delta=points)
    member = members_col.find_one({"user_id": user.id})
    total = member.get("total_points", 0)

    await update.message.reply_html(
        f"✅ {html.escape(user.first_name)} ha ricevuto <b>{points}</b> punti!\n"
        f"Totale globale: <b>{total}</b> punti."
    )

async def global_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = list(members_col.find().sort("total_points", -1).limit(10))
    if not top:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>🏆 Classifica Globale</b>\n"
    for i, m in enumerate(top, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. <a href='tg://user?id={m['user_id']}'>{name}</a> — {m.get('total_points', 0)} punti\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def list_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    members = list(members_col.find().sort("first_name", 1))
    if not members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>👥 Membri registrati globalmente:</b>\n"
    for i, m in enumerate(members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. <a href='tg://user?id={m['user_id']}'>{name}</a> — {m.get('total_points', 0)} punti\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# --- AUTO BAN ---
async def auto_ban_zero_points(app):
    while True:
        logger.info("🔁 Controllo utenti con 0 punti registrati da oltre 6 mesi...")
        now = datetime.datetime.utcnow()
        six_months_ago = now - datetime.timedelta(days=180)

        users = list(members_col.find({
            "total_points": 0,
            "created_at": {"$lte": six_months_ago}
        }))

        for user in users:
            user_id = user["user_id"]
            for g in user.get("groups", []):
                chat_id = g["chat_id"]
                try:
                    member = await app.bot.get_chat_member(chat_id, user_id)
                    if member.status in ("administrator", "creator"):
                        logger.info(f"⏭️ Salto admin {user_id} in chat {chat_id}")
                        continue

                    await app.bot.ban_chat_member(chat_id, user_id)
                    logger.info(f"🚫 Bannato {user_id} da {chat_id} (0 punti da 6 mesi)")
                except Forbidden:
                    logger.warning(f"❌ Non ho permessi per bannare in {chat_id}")
                except Exception as e:
                    logger.error(f"Errore durante ban di {user_id}: {e}")

        await asyncio.sleep(86400)

# --- MAIN ---
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("globalranking", global_ranking))
    app.add_handler(CommandHandler("listmembers", list_members))
    app.add_handler(CommandHandler("punto", punto))
    app.add_handler(CommandHandler("classifica", global_ranking))
    app.add_error_handler(error_handler)

    async def post_init(app):
        asyncio.create_task(auto_ban_zero_points(app))
        logger.info("✅ Task auto_ban_zero_points avviato correttamente.")

    app.post_init = post_init

    logger.info("🤖 Bot avviato e in ascolto...")
    await app.run_polling(close_loop=False)

# --- Avvio compatibile con Railway / ambienti con loop attivo ---
if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(main())
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("🛑 Arresto manuale del bot.")
