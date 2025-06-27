import os
import logging
import asyncio # Import asyncio
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
    TypeHandler,
    CallbackQueryHandler
)
from telegram.error import TelegramError

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, ForeignKey, DateTime, Boolean, Float
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.sql import func

# --- Configure Logging Early ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Configuration & Environment Variables ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = []
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = [int(id_str.strip()) for id_str in ADMIN_IDS_STR.split(',') if id_str.strip()]
    except ValueError:
        logging.critical("ADMIN_IDS environment variable contains non-integer values. Admin commands may not work.")

CHANNEL_ID_STR = os.environ.get("CHANNEL_ID")
CHANNEL_ID = None
if CHANNEL_ID_STR:
    try:
        CHANNEL_ID = int(CHANNEL_ID_STR)
    except ValueError:
        logging.critical("CHANNEL_ID environment variable is not a valid integer. Channel announcements may fail.")

BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

ADMIN_CONTACT_HANDLE = "@lij_hailemichael" 

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

def init_db():
    """Initializes the database and ensures default tiers."""
    try:
        if DATABASE_URL.startswith('sqlite:'):
            if not os.path.exists(BACKUP_DIR):
                try:
                    os.makedirs(BACKUP_DIR, exist_ok=True)
                except OSError as e:
                    logging.warning(f"Could not create backup directory {BACKUP_DIR}: {e}. Backups will be skipped.")
            
        Base.metadata.create_all(engine)
        
        with Session() as session:
            for tier_value in [100, 200, 300]:
                if not session.query(LotterySettings).filter_by(tier=tier_value).first():
                    session.add(LotterySettings(tier=tier_value, total_tickets=100))
            session.commit()
            
        logging.info("Database initialized successfully and default tiers ensured.")
    except OperationalError as e:
        logging.critical(f"Database connection failed during initialization: {e}")
        raise
    except Exception as e:
        logging.critical(f"Unhandled error during database initialization: {e}")
        raise

# --- Backup System ---
def backup_db():
    """Creates a timestamped database backup."""
    try:
        if DATABASE_URL.startswith('postgres'):
            logging.info("Skipping backup for PostgreSQL database (managed by cloud provider).")
            return
        
        if not DATABASE_URL.startswith('sqlite:'):
            logging.warning(f"Backup not implemented for database type: {DATABASE_URL.split('://')[0]}. Skipping.")
            return

        db_file = DATABASE_URL.split("///")[-1]
        if not os.path.exists(db_file):
            logging.warning(f"SQLite database file not found at {db_file}. Cannot backup.")
            return

        if not os.path.exists(BACKUP_DIR):
            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
            except OSError as e:
                logging.error(f"Failed to create backup directory {BACKUP_DIR}: {e}. Skipping backup.")
                return

        timestamp = datetime.now(pytz.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"backup_{timestamp}.db")
        
        import shutil
        shutil.copy2(db_file, backup_path)
        logging.info(f"Database backed up to {backup_path}")
        clean_old_backups()
    except Exception as e:
        logging.error(f"Backup failed: {e}")

def clean_old_backups(keep_last=5):
    """Rotates backup files, keeping only the most recent 'keep_last' backups."""
    try:
        if not os.path.exists(BACKUP_DIR):
            return

        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".db")])
        if len(backups) <= keep_last:
            return

        for old_backup in backups[:-keep_last]:
            os.remove(os.path.join(BACKUP_DIR, old_backup))
            logging.info(f"Cleaned up old backup: {old_backup}")
    except Exception as e:
        logging.error(f"Backup cleanup failed: {e}")

def clean_expired_reservations():
    """Removes reservations older than 24 hours from the database."""
    try:
        expiry_time = datetime.now(pytz.utc) - timedelta(hours=24)
        with Session() as session:
            deleted_count = session.query(ReservedNumber).filter(ReservedNumber.reserved_at < expiry_time).delete()
            session.commit()
            if deleted_count > 0:
                logging.info(f"Cleaned up {deleted_count} expired reservations.")
    except SQLAlchemyError as e:
        logging.error(f"Database error during expired reservation cleanup: {e}")
    except Exception as e:
        logging.error(f"Unexpected error during expired reservation cleanup: {e}")


# --- Flask Application ---
run = Flask(__name__)

@run.route('/')
def home():
    """A simple home page for the Flask application."""
    return "Lottery Bot Service is running. Use the Telegram bot to interact!"

@run.route('/health')
def health_check():
    """Health check endpoint for the Flask application."""
    try:
        with engine.connect() as connection:
            connection.execute("SELECT 1")
        
        status = "MAINTENANCE" if MAINTENANCE else "OK"
        status_code = 503 if MAINTENANCE else 200
        
        return jsonify(
            status=status,
            database="connected",
            maintenance_mode=MAINTENANCE
        ), status_code
    except Exception as e:
        logging.error(f"Health check database error: {e}")
        return jsonify(
            status="ERROR",
            database="disconnected",
            error=str(e)
        ), 500

# --- Lottery Bot Implementation ---
class LotteryBot:
    def __init__(self):
        self._validate_config()
        self.application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        self.user_activity = {}
        self._setup_handlers()

    def _validate_config(self):
        """Verifies that essential environment variables are set."""
        if not BOT_TOKEN:
            logging.critical("TELEGRAM_BOT_TOKEN environment variable is missing. Bot cannot start.")
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable required")
        if not ADMIN_IDS:
            logging.warning("ADMIN_IDS environment variable is not set or empty. Admin commands will be disabled.")
        if CHANNEL_ID is None:
            logging.warning("CHANNEL_ID environment variable is not set or invalid. Channel announcements will be disabled.")

    def init_schedulers_standalone():
        """Initializes and starts APScheduler background tasks."""
        try:
            scheduler = BackgroundScheduler(timezone=pytz.utc) 
            scheduler.add_job(backup_db, 'interval', hours=6, id='db_backup_job')
            scheduler.add_job(clean_expired_reservations, 'interval', hours=1, id='clean_reservations_job')
            scheduler.start()
            logging.info("APScheduler background tasks started.")
        except Exception as e:
            logging.error(f"Failed to start APScheduler: {e}")

    def _is_admin(self, user_id: int) -> bool:
        """Helper to check if a user is an admin."""
        return user_id in ADMIN_IDS

    async def _check_spam(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Anti-spam protection with 2-second cooldown."""
        user_id = update.effective_user.id
        now = datetime.now().timestamp()
        if user_id in self.user_activity and now - self.user_activity[user_id] < 2:
            return True
        self.user_activity[user_id] = now
        return False

    async def _check_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Checks if the bot is in maintenance mode and informs non-admin users."""
        if MAINTENANCE and not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🔧 The bot is currently under maintenance. Please try again later.")
            return True
        return False

    def _setup_handlers(self):
        """Configures all bot command and message handlers."""
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
        
        self.application.add_handler(CommandHandler("start", self._start))
        self.application.add_handler(CommandHandler("numbers", self._available_numbers))
        self.application.add_handler(CommandHandler("mytickets", self._show_user_tickets))
        self.application.add_handler(CommandHandler("progress", self._show_progress))
        self.application.add_handler(CommandHandler("winners", self._show_past_winners))
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('buy', self._start_purchase)],
            states={
                SELECT_TIER: [
                    MessageHandler(filters.Regex(r'^(100|200|300)$'), self._select_tier),
                    CallbackQueryHandler(pattern=r'^tier_(100|200|300)$', callback=self._select_tier_callback)
                ],
                SELECT_NUMBER: [
                    MessageHandler(filters.Regex(r'^([1-9][0-9]?|100)$'), self._select_number),
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

    def start_polling_in_background(self):
        """Starts the Telegram bot polling in a non-blocking way."""
        logging.info("Starting Telegram Bot polling in a background thread...")
        # Create a new event loop for this thread and set it as the current one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Run the polling within the newly created event loop
            loop.run_until_complete(self.application.run_polling(allowed_updates=Update.ALL_TYPES))
        except Exception as e:
            logging.critical(f"Telegram bot polling failed: {e}", exc_info=True)
        finally:
            loop.close() # Close the loop when done


    # ============= ADMIN COMMANDS =============
    async def _enable_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enables maintenance mode (admin only)."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        global MAINTENANCE
        MAINTENANCE = True
        await update.message.reply_text("🛠 Maintenance mode ENABLED. Users will be informed.")

    async def _disable_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disables maintenance mode (admin only)."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        global MAINTENANCE
        MAINTENANCE = False
        await update.message.reply_text("✅ Maintenance mode DISABLED. Bot is fully operational.")

    # ============= USER MANAGEMENT =============
    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles the /start command, registers new users, or welcomes back existing ones."""
        user_telegram_id = update.effective_user.id
        username = update.effective_user.username or f"user_{user_telegram_id}" # Fallback for no username

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                if not user:
                    user = User(
                        telegram_id=user_telegram_id,
                        username=username
                    )
                    session.add(user)
                    session.commit()
                    await update.message.reply_text(
                        f"🎉 Welcome to Lottery Bot, {username}! Use /buy to get your first ticket."
                    )
                else:
                    await update.message.reply_text(f"👋 Welcome back, {username}!")
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during /start for {user_telegram_id}: {e}")
                await update.message.reply_text("❌ An error occurred while processing your request. Please try again.")
            except TelegramError as e:
                logging.error(f"Telegram API error during /start for {user_telegram_id}: {e}")
                # Message already failed to send, nothing more to do here.


    # ============= TICKET MANAGEMENT =============
    def _get_available_numbers(self, tier: int) -> List[int]:
        """Fetches available numbers for a given tier."""
        with Session() as session:
            try:
                # Get reserved numbers for the specific tier
                reserved = {r.number for r in session.query(ReservedNumber.number).filter_by(tier=tier).all()}
                
                # Get confirmed (approved) tickets for the specific tier
                confirmed = {t.number for t in session.query(Ticket.number).filter_by(tier=tier, is_approved=True).all()}
                
                # Numbers 1-100 are potential, subtract reserved and confirmed
                available_numbers_set = set(range(1, 101)) - reserved - confirmed
                return sorted(list(available_numbers_set))
            except SQLAlchemyError as e:
                logging.error(f"Database error fetching available numbers for tier {tier}: {e}")
                return [] # Return empty list on error

    def _is_number_available(self, number: int, tier: int) -> bool:
        """Checks if a specific number is available for a given tier."""
        return number in self._get_available_numbers(tier)

    async def _available_numbers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays available numbers for all active lottery tiers."""
        with Session() as session:
            try:
                tiers_settings = session.query(LotterySettings).filter_by(is_active=True).order_by(LotterySettings.tier).all()
                message = "🔢 Available Numbers:\n\n"
                
                if not tiers_settings:
                    message += "No active lottery tiers found. Please contact an admin."
                else:
                    for settings in tiers_settings:
                        available = self._get_available_numbers(settings.tier)
                        # Display up to 15 numbers directly, then summarize remaining
                        display_numbers = available[:15]
                        remaining_count = len(available) - len(display_numbers)
                        
                        message += f"<b>{settings.tier} Birr Tier</b>:\n"
                        if display_numbers:
                            message += f" {', '.join(map(str, display_numbers))}"
                        else:
                            message += " No numbers available."

                        if remaining_count > 0:
                            message += f" (+{remaining_count} more)"
                        message += "\n\n"
                
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing available numbers: {e}")
                await update.message.reply_text("❌ An error occurred while fetching available numbers. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error sending available numbers: {e}")


    # ============= PURCHASE FLOW =============
    async def _start_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Starts the ticket purchase conversation flow."""
        if MAINTENANCE:
            await update.message.reply_text("🚧 Bot is under maintenance. Please try again later.")
            return ConversationHandler.END
            
        # Ensure expired reservations are cleaned before starting a new one
        clean_expired_reservations()
        
        keyboard = [
            [InlineKeyboardButton("100 Birr", callback_data="tier_100")],
            [InlineKeyboardButton("200 Birr", callback_data="tier_200")],
            [InlineKeyboardButton("300 Birr", callback_data="tier_300")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🎟️ <b>Select Ticket Tier</b>\n\n"
            "Choose your desired tier:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return SELECT_TIER

    async def _select_tier_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handles inline keyboard callback for tier selection."""
        query = update.callback_query
        await query.answer() # Acknowledge the button press
        
        # Extract tier from callback_data (e.g., "tier_100" -> 100)
        tier = int(query.data.split('_')[1])
        context.user_data['tier'] = tier
        
        available = self._get_available_numbers(tier)
        
        if not available:
            await query.edit_message_text(f"❌ No numbers currently available for the {tier} Birr tier. Please choose another tier or try later.")
            # End conversation if no numbers are available after tier selection
            return ConversationHandler.END 
            
        # Create number selection keyboard
        buttons = []
        for num in available[:20]: # Show up to first 20 available numbers
            buttons.append(InlineKeyboardButton(str(num), callback_data=f"num_{num}"))
        
        # Arrange buttons in rows of 5 for better display
        keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
        
        # Add a "Show All" button if more than 20 numbers are available
        if len(available) > 20:
            keyboard.append([InlineKeyboardButton("Show All Numbers", callback_data=f"show_all_numbers_{tier}")])
            
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"🔢 Available Numbers for {tier} Birr:\n\n"
            "Select your preferred number (first 20 shown):",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return SELECT_NUMBER # Transition to next state

    async def _select_tier(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handles text input for tier selection (fallback if not using inline keyboard)."""
        # This function might be hit if user types tier directly.
        # It calls the same logic as the callback version.
        try:
            tier = int(update.message.text)
            if tier not in [100, 200, 300]:
                await update.message.reply_text("Invalid tier. Please select 100, 200, or 300 Birr.")
                return SELECT_TIER # Stay in same state
            
            # Simulate callback query for consistent logic
            class MockQuery:
                def __init__(self, data, from_user, message): # Added message attribute
                    self.data = data
                    self.from_user = from_user
                    self.message = message # Store message object
                async def answer(self): pass
                async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
                    await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode) # Use message to reply
            
            mock_query = MockQuery(f"tier_{tier}", update.effective_user, update.message)
            return await self._select_tier_callback(mock_query, context)
            
        except ValueError:
            await update.message.reply_text("Please enter a valid tier (100, 200, or 300).")
            return SELECT_TIER
        except TelegramError as e:
            logging.error(f"Telegram API error in _select_tier: {e}")
            await update.message.reply_text("❌ An error occurred. Please try again.")
            return ConversationHandler.END


    async def _select_number_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handles inline keyboard callback for number selection and reservation."""
        query = update.callback_query
        await query.answer() # Acknowledge the button press

        # Handle 'show_all_numbers' special callback
        if query.data.startswith('show_all_numbers_'):
            tier = int(query.data.split('_')[3])
            available = self._get_available_numbers(tier)
            
            buttons = []
            for num in available: # Show all available numbers
                buttons.append(InlineKeyboardButton(str(num), callback_data=f"num_{num}"))
            
            keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"🔢 All Available Numbers for {tier} Birr:\n\n"
                "Select your preferred number:",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return SELECT_NUMBER # Stay in the same state, allowing user to pick a number
            
        # Regular number selection
        number = int(query.data.split('_')[1]) # Extract number from callback_data
        tier = context.user_data.get('tier') # Retrieve tier from user_data
        user_id = query.from_user.id
        
        if not tier:
            await query.edit_message_text("❌ Missing tier information. Please start the purchase again with /buy.")
            return ConversationHandler.END

        if not self._is_number_available(number, tier):
            await query.edit_message_text("❌ This number is no longer available. Please choose another one from the list.")
            # Re-present available numbers if the selected one is taken
            available = self._get_available_numbers(tier)
            if not available:
                await query.edit_message_text(f"❌ No numbers available for {tier} Birr tier. Please choose another tier or try later.")
                return ConversationHandler.END
            buttons = [InlineKeyboardButton(str(num), callback_data=f"num_{num}") for num in available[:20]]
            keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
            if len(available) > 20:
                keyboard.append([InlineKeyboardButton("Show All Numbers", callback_data=f"show_all_numbers_{tier}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(f"🔢 Available Numbers for {tier} Birr:\n\nSelect your preferred number:", reply_markup=reply_markup, parse_mode='HTML')
            return SELECT_NUMBER
            
        # Attempt to reserve the number
        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_id).first()
                if not user:
                    await query.edit_message_text("❌ User not found. Please /start again.")
                    return ConversationHandler.END
                    
                # Check for and update existing reservation by this user for this tier
                existing_reservation = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    tier=tier
                ).first()

                if existing_reservation:
                    # If they have an existing reservation for this tier, update it
                    existing_reservation.number = number
                    existing_reservation.reserved_at = datetime.now(pytz.utc)
                    session.add(existing_reservation)
                    logging.info(f"User {user_id} updated reservation for tier {tier} to number {number}")
                else:
                    # Create a new reservation
                    reserved = ReservedNumber(
                        number=number,
                        tier=tier,
                        user_id=user.id
                    )
                    session.add(reserved)
                    logging.info(f"User {user_id} reserved number {number} for tier {tier}")
                
                session.commit()
                
                context.user_data['number'] = number # Store confirmed reserved number
                
                await query.edit_message_text(
                    f"✅ <b>Number #{number} Reserved for {tier} Birr</b>\n\n"
                    f"Send payment of {tier} Birr to:\n"
                    "<code>CBE: 1000295626473</code>\n\n" # Hardcoded, consider making configurable
                    "Then upload your payment receipt photo to this chat. Your reservation is valid for 24 hours.",
                    parse_mode='HTML'
                )
                return PAYMENT_PROOF # Transition to next state
            except SQLAlchemyError as e:
                session.rollback() # Rollback on database error
                logging.error(f"Database error during number reservation for user {user_id}, number {number}, tier {tier}: {e}")
                await query.edit_message_text("❌ An error occurred during reservation. Please try again.")
                return ConversationHandler.END
            except TelegramError as e:
                logging.error(f"Telegram API error after number selection: {e}")
                return ConversationHandler.END


    async def _select_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handles text input for number selection (fallback if not using inline keyboard)."""
        # This function should contain logic similar to _select_number_callback but for text input.
        # For simplicity, if the user types a number, we can attempt to process it directly.
        # However, it's generally better to guide users to use inline buttons.
        try:
            number = int(update.message.text)
            if not (1 <= number <= 100):
                await update.message.reply_text("Invalid number. Please choose a number between 1 and 100.")
                return SELECT_NUMBER

            tier = context.user_data.get('tier')
            if not tier:
                await update.message.reply_text("❌ Missing tier information. Please start the purchase again with /buy.")
                return ConversationHandler.END

            # Simulate callback query for consistent logic with _select_number_callback
            class MockQueryMessage: # Mock message object for query.edit_message_text
                def __init__(self, chat_id, message_id):
                    self.chat_id = chat_id
                    self.message_id = message_id
                async def edit_text(self, text, reply_markup=None, parse_mode=None):
                    await context.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            
            class MockQuery:
                def __init__(self, data, from_user, chat_id, message_id):
                    self.data = data
                    self.from_user = from_user
                    # Mimic query.message for edit_message_text compatibility
                    self.message = MockQueryMessage(chat_id, message_id) 
                async def answer(self): pass
                async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
                    # For a message handler, we need to reply, not edit an old message.
                    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            
            mock_query = MockQuery(f"num_{number}", update.effective_user, update.effective_chat.id, update.message.message_id)
            return await self._select_number_callback(mock_query, context)

        except ValueError:
            await update.message.reply_text("Please enter a valid number (1-100).")
            return SELECT_NUMBER
        except TelegramError as e:
            logging.error(f"Telegram API error in _select_number: {e}")
            await update.message.reply_text("❌ An error occurred. Please try again.")
            return ConversationHandler.END


    async def _receive_payment_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Receives payment proof photo and stores it."""
        user_id = update.effective_user.id
        photo = update.message.photo[-1] # Get the largest photo size
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
                
                # Check if there's an existing reservation for this user, tier, and number
                reserved_entry = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    tier=tier,
                    number=number
                ).first()

                if not reserved_entry:
                    await update.message.reply_text("❌ Your reservation for this number was not found or expired. Please select a number again.")
                    return SELECT_NUMBER # Go back to number selection

                # Update the reserved entry with the photo_id
                reserved_entry.photo_id = file_id
                session.add(reserved_entry)
                session.commit()

                await update.message.reply_text(
                    f"📸 Thank you! Your payment proof for ticket #{number} (Tier {tier} Birr) has been received and will be reviewed by an admin. "
                    "You will be notified once it's approved. Use /mytickets to see your pending tickets."
                )

                # Notify admins
                admin_message = (
                    f"💰 New payment proof received for User: {update.effective_user.full_name} (@{update.effective_user.username or 'N/A'})\n"
                    f"Telegram ID: <code>{user_id}</code>\n"
                    f"Ticket: #{number} (Tier {tier} Birr)\n"
                    f"To approve: /approve {user_id} {number} {tier}"
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=admin_message, parse_mode='HTML')
                    except TelegramError as e:
                        logging.error(f"Failed to send payment proof to admin {admin_id}: {e}")
                
                # Clear user data for this conversation
                context.user_data.pop('tier', None)
                context.user_data.pop('number', None)
                return ConversationHandler.END # End the conversation
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error storing payment proof for user {user_id}, number {number}, tier {tier}: {e}")
                await update.message.reply_text("❌ An error occurred while saving your payment proof. Please try again.")
                return ConversationHandler.END
            except TelegramError as e:
                logging.error(f"Telegram API error receiving payment proof for user {user_id}: {e}")
                await update.message.reply_text("❌ An error occurred while processing your photo. Please try again.")
                return ConversationHandler.END


    async def _cancel_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancels the current ticket purchase conversation."""
        user_id = update.effective_user.id
        tier = context.user_data.get('tier')
        number = context.user_data.get('number')

        if tier and number:
            with Session() as session:
                try:
                    user = session.query(User).filter_by(telegram_id=user_id).first()
                    if user:
                        # Remove the reservation if it exists
                        session.query(ReservedNumber).filter_by(
                            user_id=user.id,
                            tier=tier,
                            number=number
                        ).delete()
                        session.commit()
                        logging.info(f"Reservation for user {user_id}, number {number}, tier {tier} cancelled.")
                except SQLAlchemyError as e:
                    session.rollback()
                    logging.error(f"Database error during reservation cancellation for user {user_id}: {e}")
        
        context.user_data.pop('tier', None)
        context.user_data.pop('number', None)
        await update.message.reply_text("🚫 Ticket purchase cancelled.")
        return ConversationHandler.END


    # ============= ADMIN APPROVALS =============
    async def _show_pending_approvals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows all pending ticket approvals to admins."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        with Session() as session:
            try:
                pending_reservations = session.query(ReservedNumber).filter(
                    ReservedNumber.photo_id.isnot(None) # Only show those with a photo
                ).all()

                if not pending_reservations:
                    await update.message.reply_text("✅ No pending approvals at the moment.")
                    return

                message = "⏳ <b>Pending Ticket Approvals</b>:\n\n"
                for res in pending_reservations:
                    user = session.query(User).filter_by(id=res.user_id).first()
                    username = user.username if user else "Unknown User"
                    message += (
                        f"User: @{username} (<code>{user.telegram_id}</code>)\n"
                        f"Ticket: #{res.number} (Tier {res.tier} Birr)\n"
                        f"Reserved At: {res.reserved_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                        f"Approve with: /approve {user.telegram_id} {res.number} {res.tier}\n"
                        f"To view photo: Send the photo with file_id: <code>{res.photo_id}</code> to a chat (or use a tool to view)\n\n"
                    )
                
                # Telegram messages have a length limit, send in chunks if too long
                if len(message) > 4096:
                    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
                    for chunk in chunks:
                        await update.message.reply_text(chunk, parse_mode='HTML')
                else:
                    await update.message.reply_text(message, parse_mode='HTML')

            except SQLAlchemyError as e:
                logging.error(f"Database error showing pending approvals: {e}")
                await update.message.reply_text("❌ An error occurred while fetching pending approvals.")
            except TelegramError as e:
                logging.error(f"Telegram API error showing pending approvals: {e}")


    async def _approve_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Approves a user's payment and registers their ticket (admin only).
        Usage: /approve <user_telegram_id> <number> <tier>
        """
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        args = context.args
        if len(args) != 3:
            await update.message.reply_text("Usage: /approve <user_telegram_id> <number> <tier>")
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
                    await update.message.reply_text(f"❌ User with Telegram ID {target_user_id} not found.")
                    return

                # Check if there's a reservation for this user, number, and tier
                reserved_ticket = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    number=ticket_number,
                    tier=ticket_tier
                ).first()

                if not reserved_ticket or not reserved_ticket.photo_id:
                    await update.message.reply_text(f"❌ No pending payment proof found for user {target_user_id}, ticket #{ticket_number} (Tier {ticket_tier}).")
                    return

                # Check if the number is already taken by an approved ticket for this tier
                if session.query(Ticket).filter_by(number=ticket_number, tier=ticket_tier, is_approved=True).first():
                    await update.message.reply_text(f"❌ Number #{ticket_number} for Tier {ticket_tier} is already approved and taken.")
                    # Optionally, delete the incorrect reservation here
                    session.delete(reserved_ticket)
                    session.commit()
                    return

                # Create the approved ticket
                new_ticket = Ticket(
                    user_id=user.id,
                    number=ticket_number,
                    tier=ticket_tier,
                    is_approved=True,
                    purchased_at=reserved_ticket.reserved_at # Use reservation time as purchase time
                )
                session.add(new_ticket)
                
                # Update LotterySettings (increment sold tickets, add to prize pool)
                lottery_setting = session.query(LotterySettings).filter_by(tier=ticket_tier).first()
                if lottery_setting:
                    lottery_setting.sold_tickets += 1
                    # Assuming tier value is also the price per ticket
                    lottery_setting.prize_pool += ticket_tier * 0.8 # Example: 80% goes to prize pool
                    session.add(lottery_setting)
                else:
                    logging.warning(f"LotterySettings for tier {ticket_tier} not found. Skipping prize pool update.")

                # Delete the reservation as it's now approved
                session.delete(reserved_ticket)
                session.commit()

                await update.message.reply_text(f"✅ Ticket #{ticket_number} (Tier {ticket_tier} Birr) for user {user.username} (ID: {target_user_id}) has been approved.")
                
                # Notify the user their ticket is approved
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=f"🎉 Your ticket #{ticket_number} (Tier {ticket_tier} Birr) has been approved! Good luck in the draw!"
                    )
                except TelegramError as e:
                    logging.error(f"Failed to notify user {target_user_id} about ticket approval: {e}")

            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error approving ticket for user {target_user_id}, number {ticket_number}, tier {ticket_tier}: {e}")
                await update.message.reply_text("❌ An error occurred during ticket approval. Please check logs.")
            except Exception as e:
                logging.error(f"Unexpected error during ticket approval: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs.")


    async def _approve_all_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Approves all pending tickets with valid payment proofs (admin only)."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        approved_count = 0
        failed_count = 0
        messages = []

        with Session() as session:
            try:
                pending_reservations = session.query(ReservedNumber).filter(
                    ReservedNumber.photo_id.isnot(None)
                ).all()

                if not pending_reservations:
                    await update.message.reply_text("✅ No pending approvals to approve all.")
                    return

                for res in pending_reservations:
                    user = session.query(User).filter_by(id=res.user_id).first()
                    if not user:
                        messages.append(f"Skipping reservation for unknown user ID {res.user_id}.")
                        failed_count += 1
                        continue

                    # Double check if number is available before approving (race condition check)
                    if not self._is_number_available(res.number, res.tier):
                        messages.append(f"Skipping ticket #{res.number} (Tier {res.tier}) for @{user.username} as it's no longer available.")
                        session.delete(res) # Delete the conflicting reservation
                        session.commit()
                        failed_count += 1
                        continue

                    try:
                        new_ticket = Ticket(
                            user_id=user.id,
                            number=res.number,
                            tier=res.tier,
                            is_approved=True,
                            purchased_at=res.reserved_at
                        )
                        session.add(new_ticket)
                        
                        lottery_setting = session.query(LotterySettings).filter_by(tier=res.tier).first()
                        if lottery_setting:
                            lottery_setting.sold_tickets += 1
                            lottery_setting.prize_pool += res.tier * 0.8
                            session.add(lottery_setting)

                        session.delete(res) # Remove reservation after approval
                        session.commit() # Commit each approval to avoid losing progress on error
                        approved_count += 1
                        messages.append(f"Approved: Ticket #{res.number} (Tier {res.tier}) for @{user.username}")

                        try:
                            await context.bot.send_message(
                                chat_id=user.telegram_id,
                                text=f"🎉 Your ticket #{res.number} (Tier {res.tier} Birr) has been approved! Good luck in the draw!"
                            )
                        except TelegramError as e:
                            logging.error(f"Failed to notify user {user.telegram_id} about ticket approval: {e}")
                            messages.append(f"Failed to notify user {user.username} (ID: {user.telegram_id}).")

                    except SQLAlchemyError as e:
                        session.rollback()
                        logging.error(f"Database error during batch approval for user {user.telegram_id}, number {res.number}, tier {res.tier}: {e}")
                        messages.append(f"Failed to approve ticket #{res.number} (Tier {res.tier}) for @{user.username} due to DB error.")
                        failed_count += 1
                    except Exception as e:
                        session.rollback()
                        logging.error(f"Unexpected error during batch approval for user {user.telegram_id}, number {res.number}, tier {res.tier}: {e}")
                        messages.append(f"Failed to approve ticket #{res.number} (Tier {res.tier}) for @{user.username} due to unexpected error.")
                        failed_count += 1

                final_message = (
                    f"Batch approval complete:\n"
                    f"✅ Approved: {approved_count} tickets\n"
                    f"❌ Failed: {failed_count} tickets\n\n"
                    + "\n".join(messages)
                )
                
                # Split and send if too long
                if len(final_message) > 4096:
                    chunks = [final_message[i:i+4000] for i in range(0, len(final_message), 4000)]
                    for chunk in chunks:
                        await update.message.reply_text(chunk)
                else:
                    await update.message.reply_text(final_message)

            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during _approve_all_pending initial query: {e}")
                await update.message.reply_text("❌ An error occurred while fetching pending approvals for batch processing.")
            except TelegramError as e:
                logging.error(f"Telegram API error during _approve_all_pending: {e}")


    # ============= DRAW & ANNOUNCEMENT =============
    async def _manual_draw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually triggers a lottery draw for a specific tier (admin only).
        Usage: /draw <tier>
        """
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        if len(context.args) != 1:
            await update.message.reply_text("Usage: /draw <tier_number_e.g._100_200_300>")
            return
        
        try:
            tier_to_draw = int(context.args[0])
            if tier_to_draw not in [100, 200, 300]:
                await update.message.reply_text("Invalid tier. Please specify 100, 200, or 300.")
                return
        except ValueError:
            await update.message.reply_text("Invalid tier. Please specify a numeric tier (e.g., 100).")
            return

        with Session() as session:
            try:
                # Check for active tickets in this tier
                eligible_tickets = session.query(Ticket).filter_by(tier=tier_to_draw, is_approved=True).all()
                if not eligible_tickets:
                    await update.message.reply_text(f"❌ No approved tickets available for the {tier_to_draw} Birr tier. Cannot perform draw.")
                    return
                
                # Check if a draw for this tier is already pending or announced
                latest_draw = session.query(LotteryDraw).filter_by(tier=tier_to_draw).order_by(LotteryDraw.drawn_at.desc()).first()
                if latest_draw and latest_draw.status in ['pending', 'announced']:
                    # Additional check: If all tickets are sold for this tier and a draw is pending, maybe allow forced draw?
                    lottery_setting = session.query(LotterySettings).filter_by(tier=tier_to_draw).first()
                    if lottery_setting and lottery_setting.sold_tickets < lottery_setting.total_tickets:
                        await update.message.reply_text(f"❌ A draw for the {tier_to_draw} Birr tier is already in '{latest_draw.status}' status. Waiting for all tickets to be sold or previous draw to be announced.")
                        return
                    # If all tickets are sold, and a draw is pending, allow forced draw.
                    elif lottery_setting and lottery_setting.sold_tickets >= lottery_setting.total_tickets and latest_draw.status == 'pending':
                         await update.message.reply_text(f"Warning: All tickets sold, but previous draw for tier {tier_to_draw} is pending. Proceeding with a new draw.")
                    
                winning_ticket = random.choice(eligible_tickets)
                winning_number = winning_ticket.number
                winner_user = session.query(User).filter_by(id=winning_ticket.user_id).first()
                
                # Calculate prize (assuming 80% of sold tickets value)
                lottery_setting = session.query(LotterySettings).filter_by(tier=tier_to_draw).first()
                prize_pool = lottery_setting.prize_pool if lottery_setting else 0
                
                # Create new draw entry
                new_draw = LotteryDraw(
                    winning_number=winning_number,
                    tier=tier_to_draw,
                    status='pending' # Set to pending, admin announces later
                )
                session.add(new_draw)
                session.flush() # To get the new_draw.id

                # Record winner
                winner_entry = Winner(
                    draw_id=new_draw.id,
                    user_id=winner_user.id,
                    number=winning_number,
                    tier=tier_to_draw,
                    prize=prize_pool # Store the prize pool at the time of draw
                )
                session.add(winner_entry)
                session.commit()

                await update.message.reply_text(
                    f"🎉 Draw complete for {tier_to_draw} Birr tier!\n"
                    f"Winning Number: <b>#{winning_number}</b>\n"
                    f"Winner: @{winner_user.username or 'N/A'} (ID: <code>{winner_user.telegram_id}</code>)\n"
                    f"Prize: {prize_pool:.2f} Birr.\n\n"
                    "Please use /announce_{tier} to announce this winner publicly."
                , parse_mode='HTML')

            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during manual draw for tier {tier_to_draw}: {e}")
                await update.message.reply_text("❌ An error occurred during the draw. Please check logs.")
            except Exception as e:
                logging.error(f"Unexpected error during manual draw: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs.")


    async def _announce_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE, tier_to_announce: int):
        """Announces the winner of the latest draw for a specific tier to the channel (admin only).
        Resets the tier for new tickets.
        """
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
        
        if CHANNEL_ID is None:
            await update.message.reply_text("❌ Channel ID is not configured. Please set the CHANNEL_ID environment variable.")
            return

        with Session() as session:
            try:
                # Find the latest pending draw for this tier
                latest_draw = session.query(LotteryDraw).filter_by(
                    tier=tier_to_announce,
                    status='pending'
                ).order_by(LotteryDraw.drawn_at.desc()).first()

                if not latest_draw:
                    await update.message.reply_text(f"❌ No pending draw found for {tier_to_announce} Birr tier to announce.")
                    return

                winner_entry = session.query(Winner).filter_by(draw_id=latest_draw.id).first()
                if not winner_entry:
                    await update.message.reply_text(f"❌ No winner recorded for draw ID {latest_draw.id}.")
                    return
                
                winner_user = session.query(User).filter_by(id=winner_entry.user_id).first()
                if not winner_user:
                    await update.message.reply_text(f"❌ Winner user data not found for ID {winner_entry.user_id}.")
                    return

                # Build announcement message
                announcement_text = (
                    f"🎉 <b>Lottery Draw Result - {tier_to_announce} Birr Tier</b> 🎉\n\n"
                    f"The winning number is: <b>#{latest_draw.winning_number}</b>!\n\n"
                    f"Congratulations to our winner: "
                    f"<b>@{winner_user.username or winner_user.telegram_id}</b>!\n\n"
                    f"Prize: <b>{winner_entry.prize:.2f} Birr</b>\n\n"
                    f"Winner, please contact {ADMIN_CONTACT_HANDLE} to claim your prize!\n\n"
                    f"New tickets for the {tier_to_announce} Birr tier are now available! Use /buy to participate."
                )

                # Send announcement to channel
                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=announcement_text,
                        parse_mode='HTML'
                    )
                    # Update draw status to announced
                    latest_draw.status = 'announced'
                    session.add(latest_draw)

                    # Reset lottery settings for this tier to allow new sales
                    lottery_setting = session.query(LotterySettings).filter_by(tier=tier_to_announce).first()
                    if lottery_setting:
                        lottery_setting.sold_tickets = 0
                        lottery_setting.prize_pool = 0
                        session.add(lottery_setting)
                        # Delete all tickets and reservations for this tier
                        session.query(Ticket).filter_by(tier=tier_to_announce).delete()
                        session.query(ReservedNumber).filter_by(tier=tier_to_announce).delete()
                    else:
                        logging.warning(f"LotterySettings for tier {tier_to_announce} not found during announcement reset.")

                    session.commit()
                    await update.message.reply_text(f"✅ Winner for {tier_to_announce} Birr tier announced successfully to channel.")
                except TelegramError as e:
                    session.rollback() # Rollback if channel announcement fails
                    logging.error(f"Failed to send announcement to channel {CHANNEL_ID}: {e}")
                    await update.message.reply_text(f"❌ Failed to announce winner to channel. Error: {e}")
                
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during winner announcement for tier {tier_to_announce}: {e}")
                await update.message.reply_text("❌ An error occurred during winner announcement. Please check logs.")
            except Exception as e:
                session.rollback()
                logging.error(f"Unexpected error during winner announcement: {e}")
                await update.message.reply_text("❌ An unexpected error occurred. Please check logs.")


    # ============= INFO COMMANDS =============
    async def _show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays all tickets purchased by the user."""
        user_telegram_id = update.effective_user.id

        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                if not user:
                    await update.message.reply_text("❌ You don't have any tickets yet. Use /buy to get started!")
                    return

                # Fetch all tickets (approved or not) and reservations
                user_tickets = session.query(Ticket).filter_by(user_id=user.id).order_by(Ticket.purchased_at.desc()).all()
                user_reservations = session.query(ReservedNumber).filter_by(user_id=user.id).order_by(ReservedNumber.reserved_at.desc()).all()

                message = "🎫 <b>Your Tickets & Reservations</b>:\n\n"

                if not user_tickets and not user_reservations:
                    message += "You haven't purchased or reserved any tickets yet. Use /buy to get one!"
                else:
                    if user_tickets:
                        message += "<b>Approved Tickets</b>:\n"
                        for ticket in user_tickets:
                            status = "✅ Approved" if ticket.is_approved else "⏳ Pending Approval"
                            message += f"  - Ticket #{ticket.number} (Tier {ticket.tier} Birr) - {status} (Purchased: {ticket.purchased_at.strftime('%Y-%m-%d %H:%M:%S UTC')})\n"
                        message += "\n"
                    
                    if user_reservations:
                        message += "<b>Reserved Numbers (Awaiting Payment Proof)</b>:\n"
                        for res in user_reservations:
                            status = "⏳ Awaiting Photo" if not res.photo_id else "📷 Proof Received"
                            message += f"  - Number #{res.number} (Tier {res.tier} Birr) - {status} (Reserved: {res.reserved_at.strftime('%Y-%m-%d %H:%M:%S UTC')})\n"
                        message += "\n<i>Note: Reservations expire after 24 hours if payment proof isn't sent.</i>\n"

                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing user tickets for {user_telegram_id}: {e}")
                await update.message.reply_text("❌ An error occurred while fetching your tickets. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error sending user tickets: {e}")


    async def _show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays the progress of ticket sales for each tier."""
        with Session() as session:
            try:
                tiers_settings = session.query(LotterySettings).order_by(LotterySettings.tier).all()
                message = "📈 <b>Lottery Progress</b>:\n\n"

                if not tiers_settings:
                    message += "No lottery tiers configured. Please contact an admin."
                else:
                    for settings in tiers_settings:
                        total = settings.total_tickets
                        sold = settings.sold_tickets
                        remaining = total - sold
                        percentage = (sold / total * 100) if total > 0 else 0

                        message += (
                            f"<b>{settings.tier} Birr Tier</b>:\n"
                            f"  Sold: {sold} / {total} tickets ({percentage:.2f}%)\n"
                            f"  Remaining: {remaining} tickets\n"
                            f"  Current Prize Pool: {settings.prize_pool:.2f} Birr\n"
                            f"  Status: {'Active' if settings.is_active else 'Inactive'}\n\n"
                        )
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing progress: {e}")
                await update.message.reply_text("❌ An error occurred while fetching progress data. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error sending progress: {e}")


    async def _show_past_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays a list of past lottery winners."""
        with Session() as session:
            try:
                past_winners = session.query(Winner).order_by(Winner.draw_id.desc()).limit(10).all() # Show last 10 winners

                if not past_winners:
                    await update.message.reply_text("🏆 No past winners yet. Be the first!")
                    return

                message = "🏆 <b>Past Lottery Winners</b>:\n\n"
                for winner_entry in past_winners:
                    user = session.query(User).filter_by(id=winner_entry.user_id).first()
                    draw = session.query(LotteryDraw).filter_by(id=winner_entry.draw_id).first()
                    
                    username = user.username if user else "Unknown User"
                    draw_date = draw.drawn_at.strftime('%Y-%m-%d') if draw else "N/A"

                    message += (
                        f"Draw Date: {draw_date}\n"
                        f"Tier: {winner_entry.tier} Birr\n"
                        f"Winning Number: #{winner_entry.number}\n"
                        f"Winner: @{username}\n"
                        f"Prize: {winner_entry.prize:.2f} Birr\n\n"
                    )
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing past winners: {e}")
                await update.message.reply_text("❌ An error occurred while fetching past winners. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error sending past winners: {e}")


    # ============= UNKNOWN COMMAND/MESSAGE HANDLING =============
    async def _handle_unknown_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Replies to unknown text messages."""
        if update.message and update.message.text:
            await update.message.reply_text(
                "I'm sorry, I don't understand that command or message. "
                "Please use one of the available commands like /buy, /numbers, /mytickets, or /progress."
            )

    async def _handle_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Replies to unknown commands."""
        if update.message and update.message.text:
            await update.message.reply_text(
                "I'm sorry, that command is not recognized. "
                "Please use one of the available commands like /buy, /numbers, /mytickets, or /progress."
            )

# Global variable to hold the bot instance
lottery_bot_instance: Optional[LotteryBot] = None

# Global flag to ensure one-time initialization
_initialization_done = False

def initialize_application_components():
    global _initialization_done, lottery_bot_instance
    if _initialization_done:
        return

    logging.info("Performing one-time application initialization...")
    
    # Initialize DB and APScheduler
    init_db()
    LotteryBot.init_schedulers_standalone() # Starts APScheduler in a new thread

    # Initialize and start the Telegram bot in a background thread
    try:
        lottery_bot_instance = LotteryBot()
        bot_thread = Thread(target=lottery_bot_instance.start_polling_in_background)
        bot_thread.daemon = True # Daemon threads exit when the main program exits
        bot_thread.start()
        logging.info("Telegram Bot polling thread initiated.") # Changed message
    except Exception as e:
        logging.critical(f"Failed to start Telegram Bot: {e}", exc_info=True)
        raise

    _initialization_done = True
    logging.info("Application components initialized.")


# Call initialization function directly so it runs when Gunicorn imports main.py
initialize_application_components()

if __name__ == '__main__':
    logging.info("Running main.py directly for development/testing.")
    logging.info("Flask application 'run' is ready to be served by Gunicorn if deployed.")
