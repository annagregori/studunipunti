import logging
import asyncio
import datetime
import html
import os
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, ChatMemberHandler, filters
)
from telegram.error import Forbidden

# --- Carica dotenv solo in locale ---
if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", 0))

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

# ---------------------------
#   FUNZIONI DATABASE
# ---------------------------

def add_or_update_member(user, chat, points_delta=0):
    if user.username == "GroupAnonymousBot":
        return

    now = datetime.datetime.utcnow()
    member = members_col.find_one({"user_id": user.id})

    group_info = {
        "chat_id": chat.id,
        "title": chat.title or "Senza titolo",
        "joined_at": now,
        "points": max(0, points_delta),
        "last_message_at": now
    }

    if member:
        members_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            }}
        )

        existing_group = next((g for g in member.get("groups", []) if g["chat_id"] == chat.id), None)

        if existing_group:
            members_col.update_one(
                {"user_id": user.id, "groups.chat_id": chat.id},
                {
                    "$inc": {"groups.$.points": points_delta, "total_points": points_delta},
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

# ---------------------------
#   CONTROLLI
# ---------------------------

async def is_admin(update: Update) -> bool:
    try:
        member = await update.effective_chat.get_member(update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ---------------------------
#   COMANDI
# ---------------------------

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

    if user.username == "GroupAnonymousBot":
        await update.message.reply_text("❌ Non puoi assegnare punti a GroupAnonymousBot.")
        return

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

    if LOG_CHAT_ID:
        await context.bot.send_message(
            LOG_CHAT_ID,
            f"➕ {user.full_name} ha ricevuto {points} punti in {chat.title}"
        )

async def imieipunti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    # Deve essere in chat privata
    if chat.type != "private":
        await update.message.reply_text("⚠️ Usa questo comando in chat privata con il bot.")
        return

    user = update.effective_user
    member = members_col.find_one({"user_id": user.id})

    if not member:
        await update.message.reply_text("⚠️ Non sei ancora registrato nel database.")
        return

    total = member.get("total_points", 0)

    msg = (
        "👤 <b>I tuoi punti</b>\n\n"
        f"🌍 <b>Punti globali:</b> {total}\n\n"
        "ℹ️ I punti sono calcolati globalmente su tutti i gruppi."
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def list_members(update: Update, context):
    members = list(members_col.find().sort("first_name", 1))

    if not members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>👥 Membri registrati:</b>\n"

    for i, m in enumerate(members, 1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. <a href='tg://user?id={m['user_id']}'>{name}</a> — {m.get('total_points',0)} punti\n"

        if len(msg) > 3500:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            msg = ""

    if msg:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ---------------------------
#   TRACK MESSAGGI
# ---------------------------

async def track_message(update: Update, context):
    if update.effective_user and update.effective_chat:
        add_or_update_member(update.effective_user, update.effective_chat)

# ---------------------------
#   CHAT MEMBER UPDATE
# ---------------------------

async def member_status_update(update: Update, context):
    user = update.chat_member.new_chat_member.user
    status = update.chat_member.new_chat_member.status
    chat = update.effective_chat

    if user.username == "GroupAnonymousBot":
        return

    if status not in ("left", "kicked"):
        return

    member = members_col.find_one({"user_id": user.id})
    if not member:
        return

    # rimuove il gruppo
    members_col.update_one(
        {"user_id": user.id},
        {"$pull": {"groups": {"chat_id": chat.id}}}
    )

    updated = members_col.find_one({"user_id": user.id})

    # se ha 0 punti e non ha più gruppi → elimina
    if updated and updated.get("total_points", 0) == 0 and len(updated.get("groups", [])) == 0:
        members_col.delete_one({"user_id": user.id})
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, f"🗑️ {user.full_name} eliminato dal DB (0 punti)")
    else:
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID,
                f"⚠️ {user.full_name} uscito da {chat.title} ma mantenuto nel DB (ha punti)")

# ---------------------------
#   CLEAN DB AUTOMATICO
# ---------------------------

async def clean_inactive_members(app):
    await asyncio.sleep(120)

    while True:
        logger.info("🧹 Avvio pulizia utenti...")
        all_members = list(members_col.find())

        for member in all_members:
            user_id = member["user_id"]

            for group in list(member.get("groups", [])):
                chat_id = group["chat_id"]

                try:
                    chat_member = await app.bot.get_chat_member(chat_id, user_id)
                    if chat_member.status in ("left", "kicked"):

                        members_col.update_one(
                            {"user_id": user_id},
                            {"$pull": {"groups": {"chat_id": chat_id}}}
                        )

                        updated = members_col.find_one({"user_id": user_id})

                        if updated and updated.get("total_points", 0) == 0 and len(updated.get("groups", [])) == 0:
                            members_col.delete_one({"user_id": user_id})

                            if LOG_CHAT_ID:
                                await app.bot.send_message(LOG_CHAT_ID,
                                    f"🗑️ {chat_member.user.full_name} eliminato dal DB (0 punti)")

                except Forbidden:
                    pass

        await asyncio.sleep(120)

# ---------------------------
#   AUTO BAN 6 MESI
# ---------------------------

# --- AUTO BAN 6 MESI + DELETE DB ---
async def auto_tasks(app):
    while True:
        logger.info("🔍 Controllo utenti con 0 punti da 6 mesi...")
        if LOG_CHAT_ID:
            await app.bot.send_message(
                LOG_CHAT_ID,
                "🔍 Controllo utenti con 0 punti da oltre 6 mesi avviato."
            )

        now = datetime.datetime.utcnow()
        six_months_ago = now - datetime.timedelta(days=180)

        users = list(members_col.find({
            "total_points": 0,
            "created_at": {"$lte": six_months_ago}
        }))

        for user in users:
            user_id = user["user_id"]
            banned_anywhere = False

            for g in user.get("groups", []):
                chat_id = g["chat_id"]

                try:
                    member = await app.bot.get_chat_member(chat_id, user_id)

                    if member.status not in ("administrator", "creator"):
                        # 🚫 RIMUOVE DAL GRUPPO (ban + unban = kick)
                        await app.bot.ban_chat_member(chat_id, user_id)
                        await app.bot.unban_chat_member(chat_id, user_id)
                        banned_anywhere = True

                except Forbidden:
                    pass
                except Exception as e:
                    logger.error(f"Errore auto-ban {user_id}: {e}")

            # 🗑️ DELETE DB UNA SOLA VOLTA
            if banned_anywhere:
                members_col.delete_one({"user_id": user_id})
                if LOG_CHAT_ID:
                    await app.bot.send_message(
                        LOG_CHAT_ID,
                        f"🚫🗑️ Utente {user_id} rimosso dai gruppi ed eliminato dal DB (0 punti, 6 mesi)"
                    )

        await asyncio.sleep(86400)



# ---------------------------
#   MAIN
# ---------------------------

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("punto", punto))
    app.add_handler(CommandHandler("listmembers", list_members))
    app.add_handler(CommandHandler("globalranking", list_members))
    app.add_handler(CommandHandler("imieipunti", imieipunti))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))
    app.add_handler(ChatMemberHandler(member_status_update, ChatMemberHandler.CHAT_MEMBER))

    async def start_background_tasks(app_):
        app_.create_task(auto_tasks(app_))
        app_.create_task(clean_inactive_members(app_))
        logger.info("✅ Task automatici avviati.")

    app.post_init = start_background_tasks

    logger.info("🤖 Bot avviato!")
    app.run_polling()
