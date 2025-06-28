# bot.py
import os
import logging
import asyncio
import random
import secrets
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    TypeHandler,
    CallbackQueryHandler
)
from telegram.error import TelegramError, BadRequest

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, ForeignKey, DateTime, Boolean, Float, event
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.sql import func

# --- Configure Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration & Environment Variables ---
# Using os.getenv is best practice for production, but explicitly setting here for clarity
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")

# Directly setting your provided values for the bot for this final code
BOT_TOKEN = "7355412379:AAGwYmpX8xpZm6eGHDykvByA_cYDQFOAJF4" # YOUR BOT TOKEN
ADMIN_IDS = [5795267718] # YOUR ADMIN TELEGRAM ID(s) as a list of integers
CHANNEL_ID = -1002585009335 # YOUR CHANNEL ID

BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true" # Can be toggled by admin command

ADMIN_CONTACT_HANDLE = os.getenv("ADMIN_CONTACT_HANDLE", "@lij_hailemichael") # Fallback, recommend setting via ENV if used

# Conversation states
SELECT_TIER, SELECT_NUMBER, PAYMENT_PROOF = range(3)

# --- Database Setup ---
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# --- Models ---
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Integer, default=0)
    invite_code = Column(String(255), unique=True)
    invited_by_user_id = Column(Integer, ForeignKey('users.id'))
    invited_users_count = Column(Integer, default=0)

    tickets = relationship("Ticket", back_populates="user")
    invited_by = relationship("User", remote_side=[id], backref="invited_users_list")

    def generate_invite_code(self):
        if not self.invite_code:
            self.invite_code = secrets.token_urlsafe(8)

@event.listens_for(User, 'before_insert')
def receive_before_insert(mapper, connection, target):
    target.generate_invite_code()

class Ticket(Base):
    __tablename__ = 'tickets'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    number = Column(Integer, nullable=False)
    tier = Column(Integer, nullable=False)
    purchased_at = Column(DateTime, default=lambda: datetime.now(pytz.utc))
    is_approved = Column(Boolean, default=False)
    is_free_ticket = Column(Boolean, default=False)
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

def init_db():
    try:
        if DATABASE_URL.startswith('sqlite:'):
            if not os.path.exists(BACKUP_DIR):
                try:
                    os.makedirs(BACKUP_DIR, exist_ok=True)
                except OSError as e:
                    logger.warning(f"Could not create backup directory {BACKUP_DIR}: {e}. Backups will be skipped.")
            
        Base.metadata.create_all(engine)
        
        with Session() as session:
            for tier_value in [100, 200, 300]:
                if not session.query(LotterySettings).filter_by(tier=tier_value).first():
                    session.add(LotterySettings(tier=tier_value, total_tickets=100))
                session.commit()
            
        logger.info("Database initialized successfully and default tiers ensured.")
    except OperationalError as e:
        logger.critical(f"Database connection failed during initialization: {e}")
        raise
    except Exception as e:
        logger.critical(f"Unhandled error during database initialization: {e}")
        raise

# --- Backup System ---
def backup_db():
    try:
        if DATABASE_URL.startswith('postgres'):
            logger.info("Skipping backup for PostgreSQL database (managed by cloud provider).")
            return
        
        if not DATABASE_URL.startswith('sqlite:'):
            logger.warning(f"Backup not implemented for database type: {DATABASE_URL.split('://')[0]}. Skipping.")
            return

        db_file = DATABASE_URL.split("///")[-1]
        if not os.path.exists(db_file):
            logger.warning(f"SQLite database file not found at {db_file}. Cannot backup.")
            return

        if not os.path.exists(BACKUP_DIR):
            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
            except OSError as e:
                logger.error(f"Failed to create backup directory {BACKUP_DIR}: {e}. Skipping backup.")
                return

        timestamp = datetime.now(pytz.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"backup_{timestamp}.db")
        
        import shutil
        shutil.copy2(db_file, backup_path)
        logger.info(f"Database backed up to {backup_path}")
        clean_old_backups()
    except Exception as e:
        logger.error(f"Backup failed: {e}")

def clean_old_backups(keep_last=5):
    try:
        if not os.path.exists(BACKUP_DIR):
            return

        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".db")])
        if len(backups) <= keep_last:
            return

        for old_backup in backups[:-keep_last]:
            os.remove(os.path.join(BACKUP_DIR, old_backup))
            logger.info(f"Cleaned up old backup: {old_backup}")
    except Exception as e:
        logger.error(f"Backup cleanup failed: {e}")

def clean_expired_reservations():
    try:
        expiry_time = datetime.now(pytz.utc) - timedelta(hours=24)
        with Session() as session:
            deleted_count = session.query(ReservedNumber).filter(ReservedNumber.reserved_at < expiry_time).delete()
            session.commit()
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired reservations.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during expired reservation cleanup: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during expired reservation cleanup: {e}")

# --- Lottery Bot Implementation Class ---
class LotteryBot:
    def __init__(self):
        self._validate_config()
        self.application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        self.user_activity = {}
        self._setup_handlers()

    def _validate_config(self):
        if not BOT_TOKEN:
            logger.critical("TELEGRAM_BOT_TOKEN is not set. Bot cannot start.")
            raise ValueError("TELEGRAM_BOT_TOKEN required")
        if not ADMIN_IDS:
            logger.warning("ADMIN_IDS is not set or empty. Admin commands will be disabled.")
        if CHANNEL_ID is None:
            logger.warning("CHANNEL_ID is not set or invalid. Channel announcements will be disabled.")
        if not ADMIN_CONTACT_HANDLE:
            logger.warning("ADMIN_CONTACT_HANDLE is not set. Defaulting to @lij_hailemichael.")

    @staticmethod
    def init_schedulers_standalone():
        try:
            scheduler = BackgroundScheduler(timezone=pytz.utc)
            scheduler.add_job(backup_db, 'interval', hours=6, id='db_backup_job')
            scheduler.add_job(clean_expired_reservations, 'interval', hours=1, id='clean_reservations_job')
            scheduler.start()
            logger.info("APScheduler background tasks started.")
        except Exception as e:
            logger.error(f"Failed to start APScheduler: {e}")

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
            if update.message:
                await update.message.reply_text("🔧 The bot is currently under maintenance. Please try again later.")
            return True
        return False

    def _setup_handlers(self):
        self.application.add_handler(TypeHandler(Update, self._check_spam), group=-1)
        self.application.add_handler(TypeHandler(Update, self._check_maintenance), group=-1)
        
        self.application.add_handler(CommandHandler("maintenance_on", self._enable_maintenance))
        self.application.add_handler(CommandHandler("maintenance_off", self._disable_maintenance))
        self.application.add_handler(CommandHandler("approve", self._approve_payment))
        self.application.add_handler(CommandHandler("pending", self._show_pending_approvals))
        self.application.add_handler(CommandHandler("approve_all", self._approve_all_pending))
        self.application.add_handler(CommandHandler("draw", self._manual_draw))
        self.application.add_handler(CommandHandler("announce_100", lambda u,c: self._announce_winners(u,c,100)))
        self.application.add_handler(CommandHandler("announce_200", lambda u,c: self._announce_winners(u,c,200)))
        self.application.add_handler(CommandHandler("announce_300", lambda u,c: self._announce_winners(u,c,300)))
        self.application.add_handler(CommandHandler("give_free_ticket", self._give_free_ticket_admin))

        self.application.add_handler(CommandHandler("start", self._start))
        self.application.add_handler(CommandHandler("numbers", self._available_numbers))
        self.application.add_handler(CommandHandler("mytickets", self._show_user_tickets))
        self.application.add_handler(CommandHandler("progress", self._show_progress))
        self.application.add_handler(CommandHandler("winners", self._show_past_winners))
        self.application.add_handler(CommandHandler("invite", self._generate_invite_link))

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('buy', self._start_purchase)],
            states={
                SELECT_TIER: [
                    MessageHandler(filters.Regex(r'^(100|200|300)$'), self._select_tier_text_input),
                    CallbackQueryHandler(pattern=r'^tier_(100|200|300)$', callback=self._select_tier_callback)
                ],
                SELECT_NUMBER: [
                    MessageHandler(filters.Regex(r'^([1-9][0-9]?|100)$'), self._select_number_text_input),
                    CallbackQueryHandler(pattern=r'^num_([1-9][0-9]?|100)$', callback=self._select_number_callback),
                    CallbackQueryHandler(pattern=r'^show_all_numbers_([1-9][0-9]?|100)$', callback=self._select_number_callback)
                ],
                PAYMENT_PROOF: [MessageHandler(filters.PHOTO, self._receive_payment_proof)]
            },
            fallbacks=[CommandHandler('cancel', self._cancel_purchase)]
        )
        self.application.add_handler(conv_handler)
        
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_unknown_message))
        self.application.add_handler(MessageHandler(filters.COMMAND, self._handle_unknown_command))

    async def start_polling(self):
        logger.info("Starting Telegram Bot polling in main asyncio loop...")
        try:
            await self.application.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.critical(f"Telegram bot polling failed: {e}", exc_info=True)


    # ============= ADMIN COMMANDS =============
    async def _enable_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        global MAINTENANCE
        MAINTENANCE = True
        await update.message.reply_text("🛠 Maintenance mode ENABLED. Users will be informed that the bot is temporarily unavailable.")

    async def _disable_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        global MAINTENANCE
        MAINTENANCE = False
        await update.message.reply_text("✅ Maintenance mode DISABLED. Bot is fully operational again.")

    async def _give_free_ticket_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        args = context.args
        if len(args) < 2 or len(args) > 3:
            await update.message.reply_text("Usage: /give_free_ticket <user_telegram_id> <tier> [number]\n"
                                            "Example: /give_free_ticket 123456789 100 50 (gives ticket #50 in 100 Birr tier)\n"
                                            "Example: /give_free_ticket 123456789 200 (gives a random ticket in 200 Birr tier)")
            return

        try:
            target_user_id = int(args[0])
            ticket_tier = int(args[1])
            if ticket_tier not in [100, 200, 300]:
                await update.message.reply_text("Invalid tier. Please specify 100, 200, or 300.")
                return
            chosen_number = int(args[2]) if len(args) == 3 else None
        except ValueError:
            await update.message.reply_text("Invalid arguments. Please provide numeric values for user ID, tier, and optional number.")
            return

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=target_user_id).first()
                if not user:
                    await update.message.reply_text(f"❌ User with Telegram ID {target_user_id} not found. Please ensure they have used /start first.")
                    return

                available_numbers = self._get_available_numbers(ticket_tier)
                if not available_numbers:
                    await update.message.reply_text(f"❌ No numbers currently available for the {ticket_tier} Birr tier to give a free ticket.")
                    return

                if chosen_number:
                    if chosen_number not in available_numbers:
                        await update.message.reply_text(f"❌ The specified number #{chosen_number} is not available for Tier {ticket_tier}. Please choose an available one.")
                        return
                    selected_number = chosen_number
                else:
                    selected_number = random.choice(available_numbers)

                new_ticket = Ticket(
                    user_id=user.id,
                    number=selected_number,
                    tier=ticket_tier,
                    is_approved=True,
                    is_free_ticket=True,
                    purchased_at=datetime.now(pytz.utc)
                )
                session.add(new_ticket)
                session.commit()

                await update.message.reply_text(
                    f"✅ Free ticket #{selected_number} (Tier {ticket_tier} Birr) has been successfully given to @{user.username or user.telegram_id}."
                )
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=f"🎁 Congratulations! You've received a FREE ticket: #{selected_number} (Tier {ticket_tier} Birr)! Check your tickets with /mytickets. Good luck in the draw!"
                    )
                except TelegramError as e:
                    logger.error(f"Failed to notify user {target_user_id} about free ticket: {e}")

            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error giving free ticket: {e}")
                await update.message.reply_text("❌ An error occurred while processing the free ticket. Please check logs for details.")
            except Exception as e:
                logger.error(f"Unexpected error giving free ticket: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs for details.")


    # ============= USER MANAGEMENT & INVITATION =============
    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_telegram_id = update.effective_user.id
        username = update.effective_user.username or f"user_{user_telegram_id}"
        
        invite_code = None
        if context.args:
            invite_code = context.args[0]
            logger.info(f"User {user_telegram_id} started with invite code: {invite_code}")

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                
                if not user:
                    invited_by_user_id = None
                    if invite_code:
                        inviter = session.query(User).filter_by(invite_code=invite_code).first()
                        if inviter:
                            invited_by_user_id = inviter.id
                            logger.info(f"New user {user_telegram_id} was invited by user {inviter.telegram_id}.")
                        else:
                            logger.warning(f"Invalid invite code '{invite_code}' used by new user {user_telegram_id}.")
                    
                    user = User(
                        telegram_id=user_telegram_id,
                        username=username,
                        invited_by_user_id=invited_by_user_id
                    )
                    session.add(user)
                    session.commit()

                    welcome_message = (
                        f"🎉 Welcome to Lottery Bot, <b>{username}</b>! 🎉\n\n"
                        "Get ready to try your luck and win big prizes!\n\n"
                        "Here's what you can do:\n"
                        "🎟️ /buy - Get your lottery ticket now!\n"
                        "🔢 /numbers - See all currently available lottery numbers.\n"
                        "🎫 /mytickets - Check your purchased and reserved tickets.\n"
                        "📈 /progress - See how many tickets are sold for each tier.\n"
                        "🏆 /winners - Browse our list of past lucky winners.\n"
                        "💌 /invite - Invite your friends and earn fantastic rewards!\n\n"
                        "Good luck! We hope you're our next big winner!"
                    )
                    await update.message.reply_text(welcome_message, parse_mode='HTML')
                else:
                    if not user.invite_code:
                        user.generate_invite_code()
                        session.add(user)
                        session.commit()
                    await update.message.reply_text(f"👋 Welcome back, <b>{username}</b>! What would you like to do today?\n\n"
                                                    "If you need a reminder of commands, use /help.", parse_mode='HTML')

            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error during /start for {user_telegram_id}: {e}")
                await update.message.reply_text("❌ An error occurred while setting up your account. Please try again or contact support.")
            except TelegramError as e:
                logger.error(f"Telegram API error during /start for {user_telegram_id}: {e}")

    async def _generate_invite_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_telegram_id = update.effective_user.id
        
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
            if not user:
                await update.message.reply_text("Please use /start first to register and get your invite code.")
                return
            
            if not user.invite_code:
                user.generate_invite_code()
                session.add(user)
                session.commit()

            try:
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username
            except TelegramError as e:
                logger.error(f"Failed to get bot username for invite link: {e}")
                await update.message.reply_text("❌ Could not generate invite link. Please try again later.")
                return

            invite_link = f"https://t.me/{bot_username}?start={user.invite_code}"

            await update.message.reply_text(
                f"💌 <b>Invite Your Friends & Earn Rewards!</b>\n\n"
                f"Share your unique invite link below. When your friends join using this link and become active, you get rewards!\n\n"
                f"🔗 Your unique invite link:\n<code>{invite_link}</code>\n\n"
                "✨ <b>Reward System</b> ✨\n"
                "🎁 Invite <b>10 active new users</b>: Get a FREE <b>200 Birr ticket</b>!\n"
                "💰 Buy <b>10 tickets</b> yourself: Get a FREE <b>100 Birr ticket</b>!\n\n"
                "(An 'active' user is someone who purchases at least one approved paid ticket.)\n\n"
                "Let the inviting begin!",
                parse_mode='HTML'
            )

    async def _check_and_award_invite_rewards(self, session, inviter_user_id: int, context: ContextTypes.DEFAULT_TYPE):
        inviter = session.query(User).filter_by(id=inviter_user_id).first()
        if not inviter:
            return

        active_invited_count = session.query(User).join(Ticket).filter(
            User.invited_by_user_id == inviter.id,
            Ticket.user_id == User.id,
            Ticket.is_approved == True,
            Ticket.is_free_ticket == False
        ).distinct(User.id).count()

        logger.info(f"Inviter {inviter.telegram_id} (DB ID: {inviter_user_id}) has {active_invited_count} active invited users.")

        eligible_rewards = active_invited_count // 10
        current_awarded_tickets = session.query(Ticket).filter(
            Ticket.user_id == inviter.id,
            Ticket.is_free_ticket == True,
            Ticket.tier == 200
        ).count()

        rewards_to_award = eligible_rewards - current_awarded_tickets

        if rewards_to_award > 0:
            logger.info(f"Awarding {rewards_to_award} invite rewards to user {inviter.telegram_id}.")
            for _ in range(rewards_to_award):
                available_numbers = self._get_available_numbers(200)
                if not available_numbers:
                    logger.warning(f"No numbers available for 200 Birr tier to award invite reward to {inviter.telegram_id}. Skipping this reward.")
                    for admin_id in ADMIN_IDS:
                        try:
                            await context.bot.send_message(chat_id=admin_id, text=f"⚠️ Warning: No 200 Birr numbers available to award invite reward to user {inviter.telegram_id}. Manual intervention may be needed.")
                        except TelegramError as e:
                            logger.error(f"Failed to send admin warning about no 200 Birr numbers: {e}")
                    continue
                
                chosen_number = random.choice(available_numbers)
                free_ticket = Ticket(
                    user_id=inviter.id,
                    number=chosen_number,
                    tier=200,
                    is_approved=True,
                    is_free_ticket=True,
                    purchased_at=datetime.now(pytz.utc)
                )
                session.add(free_ticket)
                
                try:
                    await context.bot.send_message(
                        chat_id=inviter.telegram_id,
                        text=f"🌟 Congratulations! You've invited another 10 active users and received a FREE 200 Birr ticket: #{chosen_number}! Check /mytickets to see your new ticket!"
                    )
                except TelegramError as e:
                    logger.error(f"Failed to notify inviter {inviter.telegram_id} about reward: {e}")
            session.commit()

    async def _check_and_award_bulk_purchase_rewards(self, session, user_id: int, context: ContextTypes.DEFAULT_TYPE):
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return

        purchased_tickets_count = session.query(Ticket).filter(
            Ticket.user_id == user.id,
            Ticket.is_approved == True,
            Ticket.is_free_ticket == False
        ).count()

        logger.info(f"User {user.telegram_id} has {purchased_tickets_count} approved non-free tickets.")

        # --- THIS IS THE UPDATED LINE FOR THE 10 TICKET REWARD ---
        eligible_rewards = purchased_tickets_count // 10 # Changed from 100 to 10
        # --- END OF UPDATED LINE ---

        current_awarded_tickets = session.query(Ticket).filter(
            Ticket.user_id == user.id,
            Ticket.is_free_ticket == True,
            Ticket.tier == 100
        ).count()

        rewards_to_award = eligible_rewards - current_awarded_tickets

        if rewards_to_award > 0:
            logger.info(f"Awarding {rewards_to_award} bulk purchase rewards to user {user.telegram_id}.")
            for _ in range(rewards_to_award):
                available_numbers = self._get_available_numbers(100)
                if not available_numbers:
                    logger.warning(f"No numbers available for 100 Birr tier to award bulk purchase reward to {user.telegram_id}. Skipping this reward.")
                    for admin_id in ADMIN_IDS:
                        try:
                            await context.bot.send_message(chat_id=admin_id, text=f"⚠️ Warning: No 100 Birr numbers available to award bulk purchase reward to user {user.telegram_id}. Manual intervention may be needed.")
                        except TelegramError as e:
                            logger.error(f"Failed to send admin warning about no 100 Birr numbers: {e}")
                    continue
                
                chosen_number = random.choice(available_numbers)
                free_ticket = Ticket(
                    user_id=user.id,
                    number=chosen_number,
                    tier=100,
                    is_approved=True,
                    is_free_ticket=True,
                    purchased_at=datetime.now(pytz.utc)
                )
                session.add(free_ticket)
                
                try:
                    await context.bot.send_message(
                        chat_id=user.telegram_id,
                        text=f"🎉 Congratulations! You've bought 10 tickets and received a FREE 100 Birr ticket: #{chosen_number}! Check /mytickets to see your new ticket!" # Updated text
                    )
                except TelegramError as e:
                    logger.error(f"Failed to notify user {user.telegram_id} about bulk purchase reward: {e}")
            session.commit()

    # ============= TICKET MANAGEMENT =============
    def _get_available_numbers(self, tier: int) -> List[int]:
        with Session() as session:
            try:
                reserved = {r.number for r in session.query(ReservedNumber.number).filter_by(tier=tier).all()}
                confirmed = {t.number for t in session.query(Ticket.number).filter_by(tier=tier, is_approved=True).all()}
                
                available_numbers_set = set(range(1, 101)) - reserved - confirmed
                return sorted(list(available_numbers_set))
            except SQLAlchemyError as e:
                logger.error(f"Database error fetching available numbers for tier {tier}: {e}")
                return []

    def _is_number_available(self, number: int, tier: int) -> bool:
        return number in self._get_available_numbers(tier)

    async def _available_numbers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with Session() as session:
            try:
                tiers_settings = session.query(LotterySettings).filter_by(is_active=True).order_by(LotterySettings.tier).all()
                message = "🔢 <b>Available Numbers for Lottery Tiers</b>:\n\n"
                
                if not tiers_settings:
                    message += "<i>No active lottery tiers found. Please contact an admin.</i>"
                else:
                    for settings in tiers_settings:
                        available = self._get_available_numbers(settings.tier)
                        display_numbers = available[:15]
                        remaining_count = len(available) - len(display_numbers)
                        
                        message += f"<b>{settings.tier} Birr Tier</b>:\n"
                        if display_numbers:
                            message += f" {', '.join(map(str, display_numbers))}"
                        else:
                            message += " <i>No numbers currently available.</i>"

                        if remaining_count > 0:
                            message += f" (and {remaining_count} more...)"
                        message += "\n\n"
                
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logger.error(f"Database error showing available numbers: {e}")
                await update.message.reply_text("❌ An error occurred while fetching available numbers. Please try again later.")
            except TelegramError as e:
                logger.error(f"Telegram API error sending available numbers: {e}")


    # ============= PURCHASE FLOW =============
    async def _start_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if await self._check_maintenance(update, context):
            return ConversationHandler.END
            
        clean_expired_reservations()
        
        keyboard = [
            [InlineKeyboardButton("100 Birr Ticket", callback_data="tier_100")],
            [InlineKeyboardButton("200 Birr Ticket", callback_data="tier_200")],
            [InlineKeyboardButton("300 Birr Ticket", callback_data="tier_300")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🎟️ <b>Start Your Ticket Purchase!</b>\n\n"
            "First, please select your desired ticket tier:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return SELECT_TIER

    async def _select_tier_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        tier = int(query.data.split('_')[1])
        context.user_data['tier'] = tier
        
        available = self._get_available_numbers(tier)
        
        if not available:
            await query.edit_message_text(f"❌ No numbers currently available for the <b>{tier} Birr tier</b>. Please choose another tier or try again later.", parse_mode='HTML')
            return ConversationHandler.END
            
        buttons = []
        for num in available[:20]:
            buttons.append(InlineKeyboardButton(str(num), callback_data=f"num_{num}"))
        
        keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
        
        if len(available) > 20:
            keyboard.append([InlineKeyboardButton("Show All Numbers", callback_data=f"show_all_numbers_{tier}")])
            
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_text(
                f"🔢 <b>Select Your Lucky Number for {tier} Birr!</b>\n\n"
                "Choose from the available numbers below (first 20 shown, or click 'Show All Numbers' to see more):",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except BadRequest as e:
            logger.warning(f"Telegram API error editing message in _select_tier_callback: {e}. Sending new message instead.")
            await query.message.reply_text(
                f"🔢 <b>Select Your Lucky Number for {tier} Birr!</b>\n\n"
                "Choose from the available numbers below (first 20 shown, or click 'Show All Numbers' to see more):",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        return SELECT_NUMBER

    async def _select_tier_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            tier = int(update.message.text)
            if tier not in [100, 200, 300]:
                await update.message.reply_text("Invalid tier. Please select 100, 200, or 300 Birr using the buttons or by typing the exact value.")
                return SELECT_TIER
            
            context.user_data['tier'] = tier
            
            available = self._get_available_numbers(tier)
            
            if not available:
                await update.message.reply_text(f"❌ No numbers currently available for the <b>{tier} Birr tier</b>. Please choose another tier or try again later.", parse_mode='HTML')
                return ConversationHandler.END
                
            buttons = []
            for num in available[:20]:
                buttons.append(InlineKeyboardButton(str(num), callback_data=f"num_{num}"))
            
            keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
            
            if len(available) > 20:
                keyboard.append([InlineKeyboardButton("Show All Numbers", callback_data=f"show_all_numbers_{tier}")])
                
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"🔢 <b>Select Your Lucky Number for {tier} Birr!</b>\n\n"
                "Choose from the available numbers below (first 20 shown, or click 'Show All Numbers' to see more):",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return SELECT_NUMBER
            
        except ValueError:
            await update.message.reply_text("Please enter a valid tier (100, 200, or 300).")
            return SELECT_TIER
        except TelegramError as e:
            logger.error(f"Telegram API error in _select_tier_text_input: {e}")
            await update.message.reply_text("❌ An error occurred while processing your tier selection. Please try again.")
            return ConversationHandler.END


    async def _select_number_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        if query.data.startswith('show_all_numbers_'):
            tier = int(query.data.split('_')[3])
            available = self._get_available_numbers(tier)
            
            buttons = []
            for num in available:
                buttons.append(InlineKeyboardButton(str(num), callback_data=f"num_{num}"))
            
            keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(
                    f"🔢 <b>All Available Numbers for {tier} Birr</b>:\n\n"
                    "Select your preferred number:",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            except BadRequest as e:
                logger.warning(f"Telegram API error editing message in _select_number_callback (show_all): {e}. Sending new message instead.")
                await query.message.reply_text(
                    f"🔢 <b>All Available Numbers for {tier} Birr</b>:\n\n"
                    "Select your preferred number:",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            return SELECT_NUMBER
            
        number = int(query.data.split('_')[1])
        tier = context.user_data.get('tier')
        user_id = query.from_user.id
        
        if not tier:
            await query.edit_message_text("❌ Missing tier information. Please start the purchase again with /buy.")
            return ConversationHandler.END

        if not self._is_number_available(number, tier):
            await query.edit_message_text("❌ This number is no longer available. Please choose another one from the updated list below or press 'Show All Numbers' if available.")
            available = self._get_available_numbers(tier)
            if not available:
                await query.edit_message_text(f"❌ No numbers available for {tier} Birr tier. Please choose another tier or try later.")
                return ConversationHandler.END
            
            buttons = [InlineKeyboardButton(str(num), callback_data=f"num_{num}") for num in available[:20]]
            keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
            if len(available) > 20:
                keyboard.append([InlineKeyboardButton("Show All Numbers", callback_data=f"show_all_numbers_{tier}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(f"🔢 <b>Available Numbers for {tier} Birr</b>:\n\nSelect your preferred number:", reply_markup=reply_markup, parse_mode='HTML')
            except BadRequest:
                await query.message.reply_text(f"🔢 <b>Available Numbers for {tier} Birr</b>:\n\nSelect your preferred number:", reply_markup=reply_markup, parse_mode='HTML')
            return SELECT_NUMBER

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_id).first()
                if not user:
                    await query.edit_message_text("❌ User not found. Please /start again to register your account.")
                    return ConversationHandler.END
                    
                existing_reservation = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    tier=tier
                ).first()

                if existing_reservation:
                    if existing_reservation.number != number:
                        session.delete(existing_reservation)
                        session.commit()
                        logger.info(f"User {user_id} changed reservation from {existing_reservation.number} to {number} for tier {tier}")
                        
                        if not self._is_number_available(number, tier):
                            await query.edit_message_text("❌ This number is no longer available. Please choose another one from the list.")
                            return SELECT_NUMBER

                        reserved = ReservedNumber(
                            number=number,
                            tier=tier,
                            user_id=user.id
                        )
                        session.add(reserved)
                        session.commit()
                        logger.info(f"User {user_id} successfully updated reservation for tier {tier} to number {number}")
                    else:
                        await query.edit_message_text(f"You have already selected number <b>#{number}</b> for <b>{tier} Birr</b>. Please proceed with payment.", parse_mode='HTML')
                        return PAYMENT_PROOF
                else:
                    reserved = ReservedNumber(
                        number=number,
                        tier=tier,
                        user_id=user.id
                    )
                    session.add(reserved)
                    session.commit()
                    logger.info(f"User {user_id} successfully reserved number {number} for tier {tier}")
                
                context.user_data['number'] = number
                
                await query.edit_message_text(
                    f"✅ <b>Number #{number} Reserved for {tier} Birr!</b>\n\n"
                    f"To finalize your ticket purchase, please send payment of <b>{tier} Birr</b> to:\n"
                    "<code>CBE: 1000295626473</code>\n\n"
                    "Once paid, upload your payment receipt photo directly to this chat. Your reservation is valid for <b>24 hours</b>.",
                    parse_mode='HTML'
                )
                return PAYMENT_PROOF
            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error during number reservation for user {user_id}, number {number}, tier {tier}: {e}")
                await query.edit_message_text("❌ An error occurred during your number reservation. Please try again.")
                return ConversationHandler.END
            except TelegramError as e:
                logger.error(f"Telegram API error after number selection: {e}")
                await query.edit_message_text("❌ An error occurred while communicating with Telegram. Please try again.")
                return ConversationHandler.END


    async def _select_number_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            number = int(update.message.text)
            if not (1 <= number <= 100):
                await update.message.reply_text("Invalid number. Please choose a number between 1 and 100.")
                return SELECT_NUMBER

            tier = context.user_data.get('tier')
            if not tier:
                await update.message.reply_text("❌ Missing tier information. Please start the purchase again with /buy.")
                return ConversationHandler.END

            class MockCallbackQuery:
                def __init__(self, from_user, data, message):
                    self.from_user = from_user
                    self.data = data
                    self.message = message

                async def answer(self): pass

                async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
                    await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

            mock_query = MockCallbackQuery(
                from_user=update.effective_user,
                data=f"num_{number}",
                message=update.message
            )
            return await self._select_number_callback(mock_query, context)

        except ValueError:
            await update.message.reply_text("Please enter a valid number (1-100).")
            return SELECT_NUMBER
        except TelegramError as e:
            logger.error(f"Telegram API error in _select_number_text_input: {e}")
            await update.message.reply_text("❌ An error occurred while processing your number. Please try again.")
            return ConversationHandler.END


    async def _receive_payment_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = update.effective_user.id
        photo = update.message.photo[-1]
        file_id = photo.file_id
        
        tier = context.user_data.get('tier')
        number = context.user_data.get('number')

        if not tier or not number:
            await update.message.reply_text("❌ It seems like your ticket selection was not complete. Please start again with /buy.")
            return ConversationHandler.END
        
        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_id).first()
                if not user:
                    await update.message.reply_text("❌ User not found. Please /start again.")
                    return ConversationHandler.END
                
                reserved_entry = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    tier=tier,
                    number=number
                ).first()

                if not reserved_entry:
                    await update.message.reply_text("❌ Your reservation for this number was not found or has expired. Please select a number again with /buy.")
                    return SELECT_NUMBER

                reserved_entry.photo_id = file_id
                session.add(reserved_entry)
                session.commit()

                await update.message.reply_text(
                    f"📸 Thank you! Your payment proof for ticket <b>#{number} (Tier {tier} Birr)</b> has been received and will be reviewed by an admin shortly. "
                    "You will be notified once your ticket is approved. Use /mytickets to check your pending tickets status.",
                    parse_mode='HTML'
                )

                admin_message = (
                    f"💰 <b>New Payment Proof Received!</b> 💰\n\n"
                    f"User: <b>{update.effective_user.full_name}</b> (@{update.effective_user.username or 'N/A'})\n"
                    f"Telegram ID: <code>{user_id}</code>\n"
                    f"Ticket: <b>#{number} (Tier {tier} Birr)</b>\n"
                    f"To approve: <code>/approve {user_id} {number} {tier}</code>"
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=admin_message, parse_mode='HTML')
                    except TelegramError as e:
                        logger.error(f"Failed to send payment proof to admin {admin_id}: {e}")
                
                context.user_data.pop('tier', None)
                context.user_data.pop('number', None)
                return ConversationHandler.END
            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error storing payment proof for user {user_id}, number {number}, tier {tier}: {e}")
                await update.message.reply_text("❌ An error occurred while saving your payment proof. Please try again.")
                return ConversationHandler.END
            except TelegramError as e:
                logger.error(f"Telegram API error receiving payment proof for user {user_id}: {e}")
                await update.message.reply_text("❌ An error occurred while processing your photo. Please try again.")
                return ConversationHandler.END


    async def _cancel_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = update.effective_user.id
        tier = context.user_data.get('tier')
        number = context.user_data.get('number')

        if tier and number:
            with Session() as session:
                try:
                    user_db = session.query(User).filter_by(telegram_id=user_id).first()
                    if user_db:
                        deleted_count = session.query(ReservedNumber).filter_by(
                            user_id=user_db.id,
                            tier=tier,
                            number=number
                        ).delete()
                        session.commit()
                        if deleted_count > 0:
                            logger.info(f"Reservation for user {user_db.id}, number {number}, tier {tier} cancelled.")
                except SQLAlchemyError as e:
                    session.rollback()
                    logger.error(f"Database error during reservation cancellation for user {user_id}: {e}")
        
        context.user_data.pop('tier', None)
        context.user_data.pop('number', None)
        await update.message.reply_text("🚫 Your ticket purchase has been cancelled.")
        return ConversationHandler.END

    # ============= ADMIN APPROVALS =============
    async def _show_pending_approvals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        with Session() as session:
            try:
                pending_reservations = session.query(ReservedNumber).filter(
                    ReservedNumber.photo_id.isnot(None)
                ).order_by(ReservedNumber.reserved_at).all()

                if not pending_reservations:
                    await update.message.reply_text("✅ No pending approvals at the moment. All caught up!")
                    return

                message_parts = ["⏳ <b>Pending Ticket Approvals</b>:\n\n"]
                for i, res in enumerate(pending_reservations):
                    user = session.query(User).filter_by(id=res.user_id).first()
                    username = user.username if user else "Unknown User"
                    
                    part = (
                        f"<b>{i+1}.</b> User: <b>{user.full_name if user and update.effective_chat.type == 'private' else ('@' + username) }</b> (<code>{user.telegram_id if user else 'N/A'}</code>)\n"
                        f"Ticket: <b>#{res.number} (Tier {res.tier} Birr)</b>\n"
                        f"Reserved At: {res.reserved_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                        f"Approve: <code>/approve {user.telegram_id if user else 'N/A'} {res.number} {res.tier}</code>\n"
                        f"Photo ID: <code>{res.photo_id}</code>\n\n"
                    )
                    
                    if len(message_parts[-1]) + len(part) > 4000:
                        message_parts.append(part)
                    else:
                        message_parts[-1] += part
                
                for part in message_parts:
                    await update.message.reply_text(part, parse_mode='HTML')

            except SQLAlchemyError as e:
                logger.error(f"Database error showing pending approvals: {e}")
                await update.message.reply_text("❌ An error occurred while fetching pending approvals. Please try again later.")
            except TelegramError as e:
                logger.error(f"Telegram API error showing pending approvals: {e}")


    async def _approve_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        args = context.args
        if len(args) != 3:
            await update.message.reply_text("Usage: /approve <user_telegram_id> <number> <tier>\n"
                                            "Example: /approve 123456789 50 100")
            return

        try:
            target_user_id = int(args[0])
            ticket_number = int(args[1])
            ticket_tier = int(args[2])
        except ValueError:
            await update.message.reply_text("Invalid arguments. Please provide numeric values for user ID, number, and tier.")
            return

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=target_user_id).first()
                if not user:
                    await update.message.reply_text(f"❌ User with Telegram ID {target_user_id} not found. They might need to /start the bot first.")
                    return

                reserved_ticket = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    number=ticket_number,
                    tier=ticket_tier
                ).first()

                if not reserved_ticket or not reserved_ticket.photo_id:
                    await update.message.reply_text(f"❌ No pending payment proof found for user {target_user_id}, ticket #{ticket_number} (Tier {ticket_tier}). "
                                                    "Or the reservation has expired/was already approved/cancelled. Check /pending.")
                    return

                if session.query(Ticket).filter_by(number=ticket_number, tier=ticket_tier, is_approved=True).first():
                    await update.message.reply_text(f"❌ Number #{ticket_number} for Tier {ticket_tier} is already approved and taken by another user or duplicate entry.")
                    session.delete(reserved_ticket)
                    session.commit()
                    return

                new_ticket = Ticket(
                    user_id=user.id,
                    number=ticket_number,
                    tier=ticket_tier,
                    is_approved=True,
                    is_free_ticket=False,
                    purchased_at=reserved_ticket.reserved_at
                )
                session.add(new_ticket)
                
                lottery_setting = session.query(LotterySettings).filter_by(tier=ticket_tier).first()
                if lottery_setting:
                    lottery_setting.sold_tickets += 1
                    lottery_setting.prize_pool += ticket_tier * 0.8
                    session.add(lottery_setting)
                else:
                    logger.warning(f"LotterySettings for tier {ticket_tier} not found. Skipping prize pool update for approved ticket.")

                session.delete(reserved_ticket)
                session.commit()

                await update.message.reply_text(f"✅ Ticket <b>#{ticket_number} (Tier {ticket_tier} Birr)</b> for user <b>@{user.username or user.telegram_id}</b> (ID: <code>{target_user_id}</code>) has been successfully approved.", parse_mode='HTML')
                
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=f"🎉 Your ticket <b>#{ticket_number} (Tier {ticket_tier} Birr)</b> has been approved! It's now officially entered into the draw. Good luck!",
                        parse_mode='HTML'
                    )
                except TelegramError as e:
                    logger.error(f"Failed to notify user {target_user_id} about ticket approval: {e}")
                
                if user.invited_by_user_id:
                    asyncio.create_task(self._check_and_award_invite_rewards(session, user.invited_by_user_id, context))
                
                asyncio.create_task(self._check_and_award_bulk_purchase_rewards(session, user.id, context))

            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error approving ticket for user {target_user_id}, number {ticket_number}, tier {ticket_tier}: {e}")
                await update.message.reply_text("❌ An error occurred during ticket approval. Please check logs for details.")
            except Exception as e:
                logger.error(f"Unexpected error during ticket approval: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs for details.")


    async def _approve_all_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        approved_count = 0
        failed_count = 0
        messages = []
        reward_checks = []

        with Session() as session:
            try:
                pending_reservations = session.query(ReservedNumber).filter(
                    ReservedNumber.photo_id.isnot(None)
                ).all()

                if not pending_reservations:
                    await update.message.reply_text("✅ No pending approvals to approve in bulk at this time.")
                    return

                for res in pending_reservations:
                    user = session.query(User).filter_by(id=res.user_id).first()
                    if not user:
                        messages.append(f"Skipping reservation for unknown user DB ID {res.user_id}.")
                        failed_count += 1
                        continue

                    if not self._is_number_available(res.number, res.tier):
                        messages.append(f"Skipping ticket #{res.number} (Tier {res.tier}) for @{user.username or user.telegram_id} as it's no longer available. (Conflict detected)")
                        session.delete(res)
                        session.commit()
                        failed_count += 1
                        continue

                    try:
                        new_ticket = Ticket(
                            user_id=user.id,
                            number=res.number,
                            tier=res.tier,
                            is_approved=True,
                            is_free_ticket=False,
                            purchased_at=res.reserved_at
                        )
                        session.add(new_ticket)
                        
                        lottery_setting = session.query(LotterySettings).filter_by(tier=res.tier).first()
                        if lottery_setting:
                            lottery_setting.sold_tickets += 1
                            lottery_setting.prize_pool += res.tier * 0.8
                            session.add(lottery_setting)

                        session.delete(res)
                        session.commit()
                        
                        approved_count += 1
                        messages.append(f"Approved: Ticket <b>#{res.number} (Tier {res.tier} Birr)</b> for @{user.username or user.telegram_id}")

                        try:
                            await context.bot.send_message(
                                chat_id=user.telegram_id,
                                text=f"🎉 Your ticket <b>#{res.number} (Tier {res.tier} Birr)</b> has been approved! Good luck in the draw!",
                                parse_mode='HTML'
                            )
                        except TelegramError as e:
                            logger.error(f"Failed to notify user {user.telegram_id} about ticket approval: {e}")
                            messages.append(f"Failed to notify user {user.username or user.telegram_id} (ID: {user.telegram_id}).")
                        
                        if user.invited_by_user_id:
                            reward_checks.append((self._check_and_award_invite_rewards, user.invited_by_user_id, context))
                        reward_checks.append((self._check_and_award_bulk_purchase_rewards, user.id, context))

                    except SQLAlchemyError as e:
                        session.rollback()
                        logger.error(f"Database error during batch approval for user {user.telegram_id}, number {res.number}, tier {res.tier}: {e}")
                        messages.append(f"Failed to approve ticket <b>#{res.number} (Tier {res.tier})</b> for @{user.username or user.telegram_id} due to DB error.")
                        failed_count += 1
                    except Exception as e:
                        session.rollback()
                        logger.error(f"Unexpected error during batch approval for user {user.telegram_id}, number {res.number}, tier {res.tier}: {e}")
                        messages.append(f"Failed to approve ticket <b>#{res.number} (Tier {res.tier})</b> for @{user.username or user.telegram_id} due to unexpected error.")
                        failed_count += 1

                final_message = (
                    f"Batch approval process complete:\n"
                    f"✅ <b>Approved: {approved_count} tickets</b>\n"
                    f"❌ <b>Failed: {failed_count} tickets</b>\n\n"
                    + "\n".join(messages)
                )
                
                if len(final_message) > 4096:
                    chunks = [final_message[i:i+4000] for i in range(0, len(final_message), 4000)]
                    for chunk in chunks:
                        await update.message.reply_text(chunk, parse_mode='HTML')
                else:
                    await update.message.reply_text(final_message, parse_mode='HTML')

                for func_to_call, user_id, context_obj in reward_checks:
                    asyncio.create_task(func_to_call(session, user_id, context_obj))

            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error during _approve_all_pending initial query: {e}")
                await update.message.reply_text("❌ An error occurred while fetching pending approvals for batch processing.")
            except TelegramError as e:
                logger.error(f"Telegram API error during _approve_all_pending: {e}")


    # ============= DRAW & ANNOUNCEMENT =============
    async def _manual_draw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        if len(context.args) < 1 or len(context.args) > 2:
            await update.message.reply_text("Usage: /draw <tier_number_e.g._100_200_300> [force]\n"
                                            "Example: /draw 100\n"
                                            "Example: /draw 200 force")
            return
        
        try:
            tier_to_draw = int(context.args[0])
            if tier_to_draw not in [100, 200, 300]:
                await update.message.reply_text("Invalid tier. Please specify 100, 200, or 300 Birr.")
                return
            force_draw = (len(context.args) == 2 and context.args[1].lower() == 'force')
        except ValueError:
            await update.message.reply_text("Invalid tier. Please specify a numeric tier (e.g., 100).")
            return

        with Session() as session:
            try:
                eligible_tickets = session.query(Ticket).filter_by(tier=tier_to_draw, is_approved=True).all()
                if not eligible_tickets:
                    await update.message.reply_text(f"❌ No approved tickets available for the <b>{tier_to_draw} Birr tier</b>. Cannot perform draw.", parse_mode='HTML')
                    return
                
                latest_draw = session.query(LotteryDraw).filter_by(tier=tier_to_draw).order_by(LotteryDraw.drawn_at.desc()).first()
                if latest_draw and latest_draw.status == 'pending':
                    await update.message.reply_text(f"❌ A draw for the <b>{tier_to_draw} Birr tier</b> is already pending announcement. Please announce it first with /announce_{tier_to_draw}.", parse_mode='HTML')
                    return
                
                lottery_setting = session.query(LotterySettings).filter_by(tier=tier_to_draw).first()
                if lottery_setting and lottery_setting.sold_tickets < lottery_setting.total_tickets:
                    if not force_draw:
                        await update.message.reply_text(f"⚠️ Not all tickets ({lottery_setting.sold_tickets}/{lottery_setting.total_tickets}) for <b>{tier_to_draw} Birr tier</b> are sold. If you want to draw anyway, use <code>/draw {tier_to_draw} force</code>.", parse_mode='HTML')
                        return
                    else:
                        logger.warning(f"Forcing draw for tier {tier_to_draw} even though not all tickets are sold.")
                        await update.message.reply_text(f"Admin forced draw for {tier_to_draw} Birr tier. Proceeding despite not all tickets being sold.", parse_mode='HTML')
                
                winning_ticket = random.choice(eligible_tickets)
                winning_number = winning_ticket.number
                winner_user = session.query(User).filter_by(id=winning_ticket.user_id).first()
                
                prize_pool = lottery_setting.prize_pool if lottery_setting else 0
                
                new_draw = LotteryDraw(
                    winning_number=winning_number,
                    tier=tier_to_draw,
                    status='pending'
                )
                session.add(new_draw)
                session.flush()

                winner_entry = Winner(
                    draw_id=new_draw.id,
                    user_id=winner_user.id,
                    number=winning_number,
                    tier=tier_to_draw,
                    prize=prize_pool
                )
                session.add(winner_entry)
                session.commit()

                await update.message.reply_text(
                    f"🎉 Lottery Draw Complete for <b>{tier_to_draw} Birr Tier</b>!\n\n"
                    f"Winning Number: <b>#{winning_number}</b>\n"
                    f"Winner: <b>@{winner_user.username or 'N/A'}</b> (ID: <code>{winner_user.telegram_id}</code>)\n"
                    f"Prize: <b>{prize_pool:.2f} Birr</b>.\n\n"
                    "Please use <code>/announce_{tier}</code> to publicly announce this winner to the channel.",
                    parse_mode='HTML'
                )

            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error during manual draw for tier {tier_to_draw}: {e}")
                await update.message.reply_text("❌ An error occurred during the draw. Please check logs for details.")
            except Exception as e:
                logger.error(f"Unexpected error during manual draw: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs for details.")


    async def _announce_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE, tier_to_announce: int):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
        
        if CHANNEL_ID is None:
            await update.message.reply_text("❌ Channel ID is not configured. Please set the CHANNEL_ID environment variable to enable announcements.")
            return

        with Session() as session:
            try:
                latest_draw = session.query(LotteryDraw).filter_by(
                    tier=tier_to_announce,
                    status='pending'
                ).order_by(LotteryDraw.drawn_at.desc()).first()

                if not latest_draw:
                    await update.message.reply_text(f"❌ No pending draw found for <b>{tier_to_announce} Birr tier</b> to announce. Please run <code>/draw {tier_to_announce}</code> first.", parse_mode='HTML')
                    return

                winner_entry = session.query(Winner).filter_by(draw_id=latest_draw.id).first()
                if not winner_entry:
                    await update.message.reply_text(f"❌ No winner recorded for draw ID {latest_draw.id}. This is an internal error, please check logs.")
                    return
                
                winner_user = session.query(User).filter_by(id=winner_entry.user_id).first()
                if not winner_user:
                    await update.message.reply_text(f"❌ Winner user data not found for ID {winner_entry.user_id}. This is an internal error, please check logs.")
                    return

                announcement_text = (
                    f"🎉 <b>LOTTERY DRAW RESULT - {tier_to_announce} BIRR TIER</b> 🎉\n\n"
                    f"The winning number is: <b>#{latest_draw.winning_number}</b>!\n\n"
                    f"Massive congratulations to our lucky winner: "
                    f"<b>@{winner_user.username or 'Our Dear Participant'}</b>!\n\n"
                    f"🏆 Prize Won: <b>{winner_entry.prize:.2f} Birr</b>\n\n"
                    f"Winner, please contact {ADMIN_CONTACT_HANDLE} immediately to claim your prize!\n\n"
                    f"New tickets for the <b>{tier_to_announce} Birr tier</b> are now available! Don't miss your chance to be the next winner. Use /buy to participate!"
                )

                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=announcement_text,
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
                    
                    latest_draw.status = 'announced'
                    session.add(latest_draw)

                    lottery_setting = session.query(LotterySettings).filter_by(tier=tier_to_announce).first()
                    if lottery_setting:
                        lottery_setting.sold_tickets = 0
                        lottery_setting.prize_pool = 0
                        session.add(lottery_setting)
                        
                        session.query(Ticket).filter_by(tier=tier_to_announce, is_free_ticket=False).delete()
                        session.query(ReservedNumber).filter_by(tier=tier_to_announce).delete()
                    else:
                        logger.warning(f"LotterySettings for tier {tier_to_announce} not found during announcement reset. This should not happen.")

                    session.commit()
                    await update.message.reply_text(f"✅ Winner for <b>{tier_to_announce} Birr tier</b> announced successfully to channel and tier reset for new tickets.", parse_mode='HTML')
                except TelegramError as e:
                    session.rollback()
                    logger.error(f"Failed to send announcement to channel {CHANNEL_ID}: {e}")
                    await update.message.reply_text(f"❌ Failed to announce winner to channel. Error: {e}. Please check bot permissions in the channel.")
                
            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database error during winner announcement for tier {tier_to_announce}: {e}")
                await update.message.reply_text("❌ An error occurred during winner announcement. Please check logs for details.")
            except Exception as e:
                session.rollback()
                logger.error(f"Unexpected error during winner announcement: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs for details.")


    # ============= INFO COMMANDS =============
    async def _show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_telegram_id = update.effective_user.id

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                if not user:
                    await update.message.reply_text("❌ You don't have any tickets yet. Please use /start to register, then /buy to get started!")
                    return

                user_tickets = session.query(Ticket).filter_by(user_id=user.id).order_by(Ticket.purchased_at.desc()).all()
                user_reservations = session.query(ReservedNumber).filter_by(user_id=user.id).order_by(ReservedNumber.reserved_at.desc()).all()

                message = "🎫 <b>Your Tickets & Reservations</b>:\n\n"

                if not user_tickets and not user_reservations:
                    message += "You haven't purchased or reserved any tickets yet. Use /buy to get one!"
                else:
                    if user_tickets:
                        message += "<b>Purchased & Awarded Tickets</b>:\n"
                        for ticket in user_tickets:
                            ticket_type = "🎁 FREE" if ticket.is_free_ticket else "💸 Paid"
                            status = "✅ Approved" if ticket.is_approved else "⏳ Pending Approval"
                            message += (f"  - Ticket #{ticket.number} (Tier {ticket.tier} Birr, {ticket_type}) - "
                                        f"{status} (Purchased: {ticket.purchased_at.strftime('%Y-%m-%d %H:%M:%S UTC')})\n")
                        message += "\n"
                    
                    if user_reservations:
                        message += "<b>Reserved Numbers (Awaiting Payment Proof)</b>:\n"
                        for res in user_reservations:
                            status = "⏳ Awaiting Photo" if not res.photo_id else "📷 Proof Received"
                            message += (f"  - Number #{res.number} (Tier {res.tier} Birr) - "
                                        f"{status} (Reserved: {res.reserved_at.strftime('%Y-%m-%d %H:%M:%S UTC')})\n")
                        message += "\n<i>Note: Reservations expire after 24 hours if payment proof isn't sent.</i>\n"

                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logger.error(f"Database error showing user tickets for {user_telegram_id}: {e}")
                await update.message.reply_text("❌ An error occurred while fetching your tickets. Please try again later.")
            except TelegramError as e:
                logger.error(f"Telegram API error sending user tickets: {e}")


    async def _show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with Session() as session:
            try:
                tiers_settings = session.query(LotterySettings).order_by(LotterySettings.tier).all()
                message = "📈 <b>Current Lottery Progress</b>:\n\n"

                if not tiers_settings:
                    message += "<i>No lottery tiers configured. Please contact an admin.</i>"
                else:
                    for settings in tiers_settings:
                        total = settings.total_tickets
                        sold = settings.sold_tickets
                        remaining = total - sold
                        percentage = (sold / total * 100) if total > 0 else 0

                        message += (
                            f"<b>{settings.tier} Birr Tier</b>:\n"
                            f"  Tickets Sold: {sold} / {total} ({percentage:.2f}% complete)\n"
                            f"  Tickets Remaining: {remaining}\n"
                            f"  Current Prize Pool: <b>{settings.prize_pool:.2f} Birr</b>\n"
                            f"  Status: {'Active and Open for Sales' if settings.is_active else 'Inactive'}\n\n"
                        )
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logger.error(f"Database error showing progress: {e}")
                await update.message.reply_text("❌ An error occurred while fetching progress data. Please try again later.")
            except TelegramError as e:
                logger.error(f"Telegram API error sending progress: {e}")


    async def _show_past_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with Session() as session:
            try:
                past_winners = session.query(Winner).order_by(Winner.draw_id.desc()).limit(10).all()

                if not past_winners:
                    await update.message.reply_text("🏆 No past winners yet recorded. Be the first to win!")
                    return

                message = "🏆 <b>Our Lucky Past Winners!</b> 🏆\n\n"
                for i, winner_entry in enumerate(past_winners):
                    user = session.query(User).filter_by(id=winner_entry.user_id).first()
                    draw = session.query(LotteryDraw).filter_by(id=winner_entry.draw_id).first()
                    
                    username = user.username if user else "Unknown User"
                    draw_date = draw.drawn_at.strftime('%Y-%m-%d') if draw else "N/A"

                    message += (
                        f"<b>{i+1}. Draw Date:</b> {draw_date}\n"
                        f"   <b>Tier:</b> {winner_entry.tier} Birr\n"
                        f"   <b>Winning Number:</b> #{winner_entry.number}\n"
                        f"   <b>Winner:</b> @{username}\n"
                        f"   <b>Prize:</b> {winner_entry.prize:.2f} Birr\n\n"
                    )
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logger.error(f"Database error showing past winners: {e}")
                await update.message.reply_text("❌ An error occurred while fetching past winners. Please try again later.")
            except TelegramError as e:
                logger.error(f"Telegram API error sending past winners: {e}")


    # ============= UNKNOWN COMMAND/MESSAGE HANDLING =============
    async def _handle_unknown_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message and update.message.text:
            await update.message.reply_text(
                "I'm sorry, I don't quite understand that message. "
                "Please use one of the available commands to interact with me:\n\n"
                "🎟️ /buy - Get a ticket\n"
                "🔢 /numbers - See available numbers\n"
                "🎫 /mytickets - Your tickets\n"
                "📈 /progress - Lottery status\n"
                "💌 /invite - Invite friends\n"
                "Need more help? Type /help."
            )

    async def _handle_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message and update.message.text:
            await update.message.reply_text(
                "I'm sorry, that command is not recognized. "
                "Please use one of the valid commands from the menu or type /help for assistance."
            )

# --- Bot Initialization and Startup ---
lottery_bot_instance: Optional[LotteryBot] = None

_initialization_done = False

async def main_bot_startup():
    global _initialization_done, lottery_bot_instance
    if _initialization_done:
        logger.info("Bot components already initialized.")
        return

    logger.info("Performing one-time bot application initialization...")
    
    init_db()
    
    LotteryBot.init_schedulers_standalone()

    try:
        lottery_bot_instance = LotteryBot()
        await lottery_bot_instance.start_polling() 
    except Exception as e:
        logger.critical(f"Failed to initialize and start Telegram Bot: {e}", exc_info=True)
        raise

    _initialization_done = True
    logger.info("Bot application components initialized and polling started.")

if __name__ == "__main__":
    try:
        asyncio.run(main_bot_startup())
    except KeyboardInterrupt:
        logger.info("Bot stopped by KeyboardInterrupt.")
    except Exception as e:
        logger.critical(f"Unhandled exception in main_bot_startup: {e}", exc_info=True)

