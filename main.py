import os
import logging
import asyncio
import random
from threading import Thread
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    TypeHandler
)
from telegram.error import TelegramError
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, ForeignKey, DateTime, Boolean, Float
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.sql import func

# --- Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_IDS = [int(id_str) for id_str in os.getenv("ADMIN_IDS", "").split(",") if id_str]
try:
    CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
except ValueError:
    CHANNEL_ID = None
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"
ADMIN_CONTACT_HANDLE = os.getenv("ADMIN_CONTACT_HANDLE", "@admin")

# --- Database Setup ---
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Integer, default=0)
    tickets = relationship("Ticket", back_populates="user")

class Ticket(Base):
    __tablename__ = 'tickets'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    number = Column(Integer, nullable=False)
    tier = Column(Integer, nullable=False)
    purchased_at = Column(DateTime, default=lambda: datetime.now(pytz.utc))
    is_approved = Column(Boolean, default=False)
    user = relationship("User", back_populates="tickets")

class LotteryDraw(Base):
    __tablename__ = 'draws'
    id = Column(Integer, primary_key=True)
    winning_number = Column(Integer)
    tier = Column(Integer)
    drawn_at = Column(DateTime, default=lambda: datetime.now(pytz.utc))
    status = Column(String(20), default='pending')

class Winner(Base):
    __tablename__ = 'winners'
    id = Column(Integer, primary_key=True)
    draw_id = Column(Integer, ForeignKey('draws.id'))
    user_id = Column(Integer, ForeignKey('users.id'))
    number = Column(Integer)
    tier = Column(Integer)
    prize = Column(Float)
    draw = relationship("LotteryDraw", backref="winners")

class LotterySettings(Base):
    __tablename__ = 'lottery_settings'
    tier = Column(Integer, primary_key=True)
    total_tickets = Column(Integer, default=100)
    sold_tickets = Column(Integer, default=0)
    prize_pool = Column(Float, default=0)
    is_active = Column(Boolean, default=True)

class ReservedNumber(Base):
    __tablename__ = 'reserved_numbers'
    number = Column(Integer, primary_key=True)
    tier = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    reserved_at = Column(DateTime, default=lambda: datetime.now(pytz.utc))
    photo_id = Column(String(255))

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

def init_db():
    try:
        if DATABASE_URL.startswith('sqlite:') and not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR, exist_ok=True)
        
        Base.metadata.create_all(engine)
        
        with Session() as session:
            for tier_value in [100, 200, 300]:
                if not session.query(LotterySettings).filter_by(tier=tier_value).first():
                    session.add(LotterySettings(tier=tier_value))
            session.commit()
    except Exception as e:
        logging.critical(f"Database initialization failed: {e}")
        raise

def backup_db():
    try:
        if not DATABASE_URL.startswith('sqlite:'):
            return
            
        db_file = DATABASE_URL.split("///")[-1]
        if not os.path.exists(db_file):
            return

        timestamp = datetime.now(pytz.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"backup_{timestamp}.db")
        import shutil
        shutil.copy2(db_file, backup_path)
        clean_old_backups()
    except Exception as e:
        logging.error(f"Backup failed: {e}")

def clean_old_backups(keep_last=5):
    try:
        if not os.path.exists(BACKUP_DIR):
            return

        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".db")])
        for old_backup in backups[:-keep_last]:
            os.remove(os.path.join(BACKUP_DIR, old_backup))
    except Exception as e:
        logging.error(f"Backup cleanup failed: {e}")

def clean_expired_reservations():
    try:
        expiry_time = datetime.now(pytz.utc) - timedelta(hours=24)
        with Session() as session:
            deleted_count = session.query(ReservedNumber).filter(ReservedNumber.reserved_at < expiry_time).delete()
            session.commit()
    except Exception as e:
        logging.error(f"Reservation cleanup failed: {e}")

# --- Flask App ---
app = Flask(__name__)

@app.route('/health')
def health_check():
    try:
        with engine.connect() as conn:
            conn.execute("SELECT 1")
        return jsonify(
            status="MAINTENANCE" if MAINTENANCE else "OK",
            database="connected",
            maintenance_mode=MAINTENANCE
        ), 503 if MAINTENANCE else 200
    except Exception as e:
        return jsonify(
            status="ERROR",
            database="disconnected",
            error=str(e)
        ), 500

# --- Bot Implementation ---
class LotteryBot:
    def __init__(self):
        self._validate_config()
        self.application = ApplicationBuilder().token(BOT_TOKEN).build()
        self.user_activity = {}
        self._setup_handlers()

    def _validate_config(self):
        if not BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not ADMIN_IDS:
            logging.warning("No ADMIN_IDS configured")
        if not CHANNEL_ID:
            logging.warning("No CHANNEL_ID configured")

    @staticmethod
    def init_schedulers():
        scheduler = BackgroundScheduler(timezone=pytz.utc)
        scheduler.add_job(backup_db, 'interval', hours=6)
        scheduler.add_job(clean_expired_reservations, 'interval', hours=1)
        scheduler.start()

    def _is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

    async def _check_spam(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        now = datetime.now().timestamp()
        if user_id in self.user_activity and now - self.user_activity[user_id] < 2:
            return True
        self.user_activity[user_id] = now
        return False

    async def _check_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if MAINTENANCE and not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🔧 Bot is under maintenance. Please try again later.")
            return True
        return False

    def _setup_handlers(self):
        self.application.add_handler(TypeHandler(Update, self._check_spam), -1)
        self.application.add_handler(TypeHandler(Update, self._check_maintenance), -1)
        
        # Admin commands
        admin_handlers = [
            CommandHandler("maintenance_on", self._enable_maintenance),
            CommandHandler("maintenance_off", self._disable_maintenance),
            CommandHandler("approve", self._approve_payment),
            CommandHandler("pending", self._show_pending_approvals),
            CommandHandler("approve_all", self._approve_all_pending),
            CommandHandler("draw", self._manual_draw),
            CommandHandler("announce_100", lambda u,c: self._announce_winners(u,c,100)),
            CommandHandler("announce_200", lambda u,c: self._announce_winners(u,c,200)),
            CommandHandler("announce_300", lambda u,c: self._announce_winners(u,c,300))
        ]
        self.application.add_handlers(admin_handlers)
        
        # User commands
        user_handlers = [
            CommandHandler("start", self._start),
            CommandHandler("numbers", self._available_numbers),
            CommandHandler("mytickets", self._show_user_tickets),
            CommandHandler("progress", self._show_progress),
            CommandHandler("winners", self._show_past_winners)
        ]
        self.application.add_handlers(user_handlers)
        
        # Conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('buy', self._start_purchase)],
            states={
                SELECT_TIER: [
                    MessageHandler(filters.Regex(r'^(100|200|300)$'), self._select_tier),
                    MessageHandler(filters.CallbackQuery(pattern=r'^tier_(100|200|300)$'), self._select_tier_callback)
                ],
                SELECT_NUMBER: [
                    MessageHandler(filters.Regex(r'^([1-9][0-9]?|100)$'), self._select_number),
                    MessageHandler(filters.CallbackQuery(pattern=r'^num_([1-9][0-9]?|100)$'), self._select_number_callback),
                    MessageHandler(filters.CallbackQuery(pattern=r'^show_all_numbers_([1-9][0-9]?|100)$'), self._select_number_callback)
                ],
                PAYMENT_PROOF: [MessageHandler(filters.PHOTO, self._receive_payment_proof)]
            },
            fallbacks=[CommandHandler('cancel', self._cancel_purchase)]
        )
        self.application.add_handler(conv_handler)
        
        # Fallback handlers
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_unknown_message))
        self.application.add_handler(MessageHandler(filters.COMMAND, self._handle_unknown_command))

    # Conversation states
    SELECT_TIER, SELECT_NUMBER, PAYMENT_PROOF = range(3)

    # ... [Rest of your bot methods remain unchanged] ...

    async def run_polling_bot(self):
        try:
            await self.application.run_polling(drop_pending_updates=True)
        except Exception as e:
            logging.critical(f"Bot polling failed: {e}")

# --- Application Entry Points ---
telegram_bot_instance = None

def run(environ, start_response):
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    try:
        init_db()
        
        global telegram_bot_instance
        if telegram_bot_instance is None:
            LotteryBot.init_schedulers()
            
            def start_bot():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                global telegram_bot_instance
                telegram_bot_instance = LotteryBot()
                loop.run_until_complete(telegram_bot_instance.run_polling_bot())
            
            Thread(target=start_bot, daemon=True).start()
            
    except Exception as e:
        logging.critical(f"Startup failed: {e}")
    
    return app(environ, start_response)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    Thread(target=lambda: app.run(host='0.0.0.0', port=5000), daemon=True).start()
    LotteryBot.init_schedulers()
    
    bot = LotteryBot()
    asyncio.run(bot.run_polling_bot())
