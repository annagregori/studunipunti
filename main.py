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

LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", 0))  # Chat ID per log

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
    # Esclusione bot di sistema
    if user.username == "GroupAnonymousBot":
        return

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
            {"$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            }}
        )

        existing_group = next(
            (g for g in member.get("groups", []) if g["chat_id"] == chat.id),
            None
        )

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
                {"$push": {"groups": group_info}, "$inc": {"total_points": points_delta}}
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

# --- Comandi ---
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
            chat_id=LOG_CHAT_ID,
            text=f"➕ {user.full_name} ha ricevuto {points} punti in {chat.title}"
        )

async def global_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = list(members_col.find().sort("total_points", -1).limit(10))
    if not top:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>🏆 Classifica Globale</b>\n"
    for i, m in enumerate(top, start=1):
        name = html.escape(m.get("first_name", "Utente") or "Utente")
        msg += f'{i}. <a href="tg://user?id={m["user_id"]}">{name}</a> — {m.get("total_points", 0)} punti\n'

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def list_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    members = list(members_col.find().sort("first_name", 1))
    if not members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>👥 Membri registrati globalmente:</b>\n"

    for i, m in enumerate(members, start=1):
        user_id = m["user_id"]
        name = html.escape(m.get("first_name", "Utente") or "Utente")
        total = m.get("total_points", 0)

        msg += f'{i}. <a href="tg://user?id={user_id}">{name}</a> — {total} punti\n'

        if len(msg) > 3500:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            msg = ""

    if msg:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- Gestione messaggi ---
async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    add_or_update_member(user, chat)

# --- Gestione uscite ---
async def member_status_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_member.new_chat_member.user
    new_status = update.chat_member.new_chat_member.status

    if user.username == "GroupAnonymousBot":
        return

    chat = update.effective_chat

    if new_status in ("left", "kicked"):
        member = members_col.find_one({"user_id": user.id})
        if not member:
            return

# Rimuove il gruppo dai suoi gruppi
members_col.update_one(
    {"user_id": user.id},
    {"$pull": {"groups": {"chat_id": chat.id}}}
)

# 🔥 Ricarica i dati aggiornati
updated = members_col.find_one({"user_id": user.id})

# Se non ha più gruppi E ha 0 punti → elimina dal DB
if updated and updated.get("total_points", 0) == 0 and len(updated.get("groups", [])) == 0:
    members_col.delete_one({"user_id": user.id})

            if LOG_CHAT_ID:
                await context.bot.send_message(
                    LOG_CHAT_ID,
                    f"🗑️ {user.full_name} uscito da {chat.title} ed eliminato dal DB (0 punti)"
                )
        else:
            if LOG_CHAT_ID:
                await context.bot.send_message(
                    LOG_CHAT_ID,
                    f"⚠️ {user.full_name} uscito da {chat.title}, ma mantenuto nel DB (ha punti)"
                )


# --- Pulizia automatica utenti usciti ---
async def clean_inactive_members(app):
    await asyncio.sleep(120)
    while True:
        logger.info("🧹 Avvio pulizia utenti...")
        all_members = list(members_col.find())

        for member in all_members:
            user_id = member["user_id"]
            total_points = member.get("total_points", 0)

            for group in list(member.get("groups", [])):
                chat_id = group["chat_id"]
                try:
                    chat_member = await app.bot.get_chat_member(chat_id, user_id)

                    if chat_member.status in ("left", "kicked"):

                        # Rimuovi il gruppo
                        members_col.update_one(
                            {"user_id": user_id},
                            {"$pull": {"groups": {"chat_id": chat_id}}}
                        )

                        logger.info(f"Rimosso gruppo {chat_id} da {user_id}")

                        # Ricarica i dati utente
                        updated = members_col.find_one({"user_id": user_id})

                        # Se total_points = 0 e non è più in nessun gruppo → elimina dal DB
                        if updated and updated.get("total_points", 0) == 0 and len(updated.get("groups", [])) == 0:
                            members_col.delete_one({"user_id": user_id})
                            if LOG_CHAT_ID:
                                await app.bot.send_message(
                                    LOG_CHAT_ID,
                                    f"🗑️ {chat_member.user.full_name} eliminato dal DB (0 punti)"
                                )

                except Forbidden:
                    pass
                except Exception as e:
                    logger.error(f"Errore pulizia {user_id} in {chat_id}: {e}")

        await asyncio.sleep(86400)


# --- Auto ban 0 punti dopo 6 mesi ---
async def auto_tasks(app):
    while True:

        # 🔍 LOG + INVIO MESSAGGIO OGNI ESECUZIONE
        logger.info("🔍 Controllo utenti con 0 punti da oltre 6 mesi avviato.")
        if LOG_CHAT_ID:
            await app.bot.send_message(LOG_CHAT_ID, "🔍 Avvio controllo utenti con 0 punti da oltre 6 mesi...")

        now = datetime.datetime.utcnow()
        six_months_ago = now - datetime.timedelta(days=180)

        users = list(members_col.find({
            "total_points": 0,
            "created_at": {"$lte": six_months_ago}
        }))

        for user in users:
            for g in user.get("groups", []):
                try:
                    member = await app.bot.get_chat_member(g["chat_id"], user["user_id"])
                    if member.status not in ("administrator", "creator"):
                        await app.bot.ban_chat_member(g["chat_id"], user["user_id"])
                        if LOG_CHAT_ID:
                            await app.bot.send_message(
                                LOG_CHAT_ID,
                                f"🚫 Bannato {user['user_id']} da {g['chat_id']} (0 punti da 6 mesi)"
                            )
                except Forbidden:
                    pass
                except Exception as e:
                    logger.error(f"Errore ban {user['user_id']}: {e}")

        await asyncio.sleep(86400)


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
    groups = member.get("groups", [])

    msg = f"👤 <b>I tuoi punti</b>\n\n"
    msg += f"🌍 <b>Punti globali:</b> {total}\n\n"
    msg += "<b>Punti nei gruppi:</b>\n"

    if not groups:
        msg += "• Nessun gruppo registrato.\n"
    else:
        for g in groups:
            title = html.escape(g.get("title", "Sconosciuto"))
            pts = g.get("points", 0)
            msg += f"• <b>{title}</b>: {pts} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)



# --- MAIN ---
if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    app_ = ApplicationBuilder().token(BOT_TOKEN).build()

    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("globalranking", global_ranking))
    app_.add_handler(CommandHandler("listmembers", list_members))
    app_.add_handler(CommandHandler("punto", punto))
    app_.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))
    app_.add_handler(ChatMemberHandler(member_status_update, ChatMemberHandler.CHAT_MEMBER))
    app_.add_handler(CommandHandler("imieipunti", imieipunti))


    async def start_auto_tasks(app__):
        app__.create_task(auto_tasks(app__))
        app__.create_task(clean_inactive_members(app__))
        logger.info("✅ Task di manutenzione avviati.")

    app_.post_init = start_auto_tasks

    logger.info("🤖 Bot avviato su Railway!")
    app_.run_polling()
