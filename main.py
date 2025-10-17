import logging
import datetime
import html
import os
import asyncio
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

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

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Collezioni ---
members_col = db["members"]

# --- Utility DB ---

def add_or_update_member(user, chat, points_delta=0):
    """Registra o aggiorna un membro a livello globale."""
    member = members_col.find_one({"user_id": user.id})
    now = datetime.datetime.utcnow()

    group_info = {
        "chat_id": chat.id,
        "title": chat.title,
        "joined_at": now,
        "points": max(0, points_delta),
        "last_message_at": now
    }

    if member:
        # Aggiorna info base utente
        members_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            }}
        )

        # Controlla se già nel gruppo
        existing_group = next((g for g in member.get("groups", []) if g["chat_id"] == chat.id), None)
        if existing_group:
            members_col.update_one(
                {"user_id": user.id, "groups.chat_id": chat.id},
                {
                    "$inc": {"groups.$.points": points_delta, "total_points": points_delta},
                    "$set": {"groups.$.last_message_at": now, "groups.$.title": chat.title}
                }
            )
        else:
            members_col.update_one(
                {"user_id": user.id},
                {"$push": {"groups": group_info}, "$inc": {"total_points": points_delta}}
            )
    else:
        # Nuovo utente globale
        members_col.insert_one({
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "groups": [group_info],
            "total_points": points_delta,
            "created_at": now  # 🔥 Data registrazione per controllo 6 mesi
        })


def get_user_mention(user):
    name = html.escape(user.first_name or "Utente")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

# --- Eventi Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_member(update.message.from_user, update.effective_chat)
    await update.message.reply_text("🤖 Ciao! Sto tracciando utenti e punti globalmente.")

async def global_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra la classifica globale di tutti i gruppi."""
    top_members = list(members_col.find().sort("total_points", -1).limit(10))
    if not top_members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>🏆 Classifica Globale Utenti</b>\n"
    for i, m in enumerate(top_members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        mention = f"<a href='tg://user?id={m['user_id']}'>{name}</a>"
        msg += f"{i}. {mention} — {m.get('total_points', 0)} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def list_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista di tutti gli utenti conosciuti globalmente."""
    members = list(members_col.find().sort("first_name", 1))
    if not members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>👥 Membri registrati globalmente:</b>\n"
    for i, m in enumerate(members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        mention = f"<a href='tg://user?id={m['user_id']}'>{name}</a>"
        msg += f"{i}. {mention} — {m.get('total_points', 0)} punti totali\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def punto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assegna punti a un membro del gruppo."""
    if not update.message.reply_to_message:
        await update.message.reply_text("Rispondi a un messaggio per assegnare punti a un utente.")
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
        f"✅ <b>{html.escape(user.first_name)}</b> ha ricevuto <b>{points}</b> punti!\n"
        f"Totale globale: <b>{total}</b> punti."
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update "{update}" caused error "{context.error}"')

# --- BAN AUTOMATICO OGNI 6 MESI ---

async def auto_ban_zero_points(app):
    """
    Ogni giorno controlla chi ha total_points == 0 e
    si è registrato da più di 6 mesi → ban automatico (esclusi admin).
    """
    while True:
        now = datetime.datetime.utcnow()
        six_months_ago = now - datetime.timedelta(days=180)

        logger.info("🔁 Controllo utenti con 0 punti registrati da oltre 6 mesi...")

        candidates = members_col.find({
            "total_points": 0,
            "created_at": {"$lt": six_months_ago}
        })

        for u in candidates:
            user_id = u["user_id"]
            for g in u.get("groups", []):
                chat_id = g["chat_id"]
                try:
                    # Evita di bannare admin
                    admins = await app.bot.get_chat_administrators(chat_id)
                    admin_ids = [a.user.id for a in admins]
                    if user_id in admin_ids:
                        logger.info(f"⏭️ {user_id} è admin di {chat_id}, salto il ban.")
                        continue

                    await app.bot.ban_chat_member(chat_id, user_id)
                    logger.info(f"🚫 Bannato {user_id} da {chat_id} (0 punti da oltre 6 mesi).")

                except Exception as e:
                    logger.warning(f"⚠️ Errore nel bannare {user_id} da {chat_id}: {e}")

        # Aggiorna data ultima esecuzione
        db["meta"].update_one({"_id": "autoban"}, {"$set": {"last_run": now}}, upsert=True)

        await asyncio.sleep(86400)  # ogni 24 ore

# --- Avvio bot ---
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("globalranking", global_ranking))
    app.add_handler(CommandHandler("listmembers", list_members))
    app.add_handler(CommandHandler("punto", punto))
    app.add_handler(CommandHandler("classifica", global_ranking))
    app.add_error_handler(error_handler)

    # ✅ avvia il task periodico dopo l’avvio del bot
    async def on_startup(app):
        asyncio.create_task(auto_ban_zero_points(app))
        logger.info("✅ Task auto_ban_zero_points avviato correttamente.")

    app.post_init = on_startup

    # 🚀 IMPORTANTE: usa direttamente run_polling(), SENZA asyncio.run()
    await app.initialize()
    await on_startup(app)
    await app.start()
    logger.info("🤖 Bot avviato e in ascolto...")
    await app.updater.start_polling()
    await app.updater.idle()

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        print("Bot arrestato manualmente.")

if __name__ == "__main__":
    asyncio.run(main())
