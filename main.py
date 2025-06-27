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
from telegram.error import TelegramError # Import for specific Telegram API error handling

# Import pytz for timezone-aware datetimes
import pytz
from apscheduler.schedulers.background import BackgroundScheduler # Import here for clarity
# Removed 'text' from sqlalchemy import as it's for SQLAlchemy 1.x (as per your requirements.txt)
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, ForeignKey, DateTime, Boolean, Float
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.sql import func

# --- Configuration & Environment Variables ---
# Centralized loading of environment variables with clearer error handling
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Safely parse ADMIN_IDS, ensuring it's a list of integers
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

# --- ADMIN CONTACT HANDLE ---
# This handle will be displayed to users in winner announcements for claiming prizes.
ADMIN_CONTACT_HANDLE = "@lij_hailemichael" 

# Conversation states
SELECT_TIER, SELECT_NUMBER, PAYMENT_PROOF = range(3)

# --- Database Setup ---
# SQLAlchemy setup
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# --- Models ---
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Integer, default=0) # Consider making this BigInteger if balances can grow very large
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
    status = Column(String(20), default='pending')  # pending, announced

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
    """
    Initializes the database by creating all defined tables
    and ensures initial lottery tiers settings are present.
    """
    try:
        # Create backup directory only if using SQLite and it's not a read-only environment
        if DATABASE_URL.startswith('sqlite:'):
            if not os.path.exists(BACKUP_DIR):
                try:
                    os.makedirs(BACKUP_DIR, exist_ok=True)
                except OSError as e:
                    logging.warning(f"Could not create backup directory {BACKUP_DIR}: {e}. Backups will be skipped.")
        
        Base.metadata.create_all(engine)
        
        # Initialize ticket tiers if they don't exist
        with Session() as session:
            for tier_value in [100, 200, 300]:
                if not session.query(LotterySettings).filter_by(tier=tier_value).first():
                    session.add(LotterySettings(tier=tier_value, total_tickets=100)) # Default to 100 tickets
            session.commit()
            
        logging.info("Database initialized successfully and default tiers ensured.")
    except OperationalError as e:
        logging.critical(f"Database connection failed during initialization: {e}")
        raise # Re-raise to prevent app from starting without DB
    except Exception as e:
        logging.critical(f"Unhandled error during database initialization: {e}")
        raise

# --- Backup System ---
def backup_db():
    """
    Creates a timestamped database backup.
    Skips for PostgreSQL as it's typically managed by the provider.
    For SQLite, copies the database file.
    """
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

        # Ensure backup directory exists and is writable
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
        clean_old_backups() # Clean up old backups after successful new backup
    except Exception as e:
        logging.error(f"Backup failed: {e}")

def clean_old_backups(keep_last=5):
    """Rotates backup files, keeping only the most recent 'keep_last' backups."""
    try:
        if not os.path.exists(BACKUP_DIR):
            return # No backup directory means nothing to clean

        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".db")])
        if len(backups) <= keep_last:
            return # Not enough backups to clean

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


# --- Flask Health Check ---
app = Flask(__name__)

@app.route('/health')
def health_check():
    """
    Health check endpoint for the Flask application.
    Checks database connectivity and bot maintenance mode.
    """
    try:
        # For SQLAlchemy 1.x, use connection.execute() directly for raw SQL
        with engine.connect() as connection:
            connection.execute("SELECT 1") # Corrected for SQLAlchemy 1.x compatibility
        
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
    def __init__(self): # Simplified init: always builds its own application
        self._validate_config()
        self.application = ApplicationBuilder().token(BOT_TOKEN).build() # Build here
        
        self.user_activity = {} # For anti-spam
        self._setup_handlers()

    def _validate_config(self):
        """Verifies that essential environment variables are set."""
        if not BOT_TOKEN:
            logging.critical("TELEGRAM_BOT_TOKEN environment variable is missing. Bot cannot start.")
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable required")
        if not ADMIN_IDS:
            logging.warning("ADMIN_IDS environment variable is not set or empty. Admin commands will be disabled.")
        if CHANNEL_ID is None: # Use `is None` because CHANNEL_ID could be 0 (a valid chat ID)
            logging.warning("CHANNEL_ID environment variable is not set or invalid. Channel announcements will be disabled.")

    # This is a standalone function for APScheduler, called by the main run function.
    def init_schedulers_standalone():
        """Initializes and starts APScheduler background tasks."""
        try:
            # Initialize BackgroundScheduler with pytz.utc timezone explicitly
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
            return True # Indicate that the event was handled and should stop processing
        self.user_activity[user_id] = now
        return False # Continue processing the event

    async def _check_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Checks if the bot is in maintenance mode and informs non-admin users."""
        if MAINTENANCE and not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🔧 The bot is currently under maintenance. Please try again later.")
            return True # Indicate that the event was handled
        return False # Continue processing

    def _setup_handlers(self):
        """Confgures all bot command and message handlers."""
        # Anti-spam and maintenance checks must be at the highest group (lowest number) to run first.
        self.application.add_handler(TypeHandler(Update, self._check_spam), group=-1)
        self.application.add_handler(TypeHandler(Update, self._check_maintenance), group=-1)
        
        # Admin commands
        self.application.add_handler(CommandHandler("maintenance_on", self._enable_maintenance))
        self.application.add_handler(CommandHandler("maintenance_off", self._disable_maintenance))
        self.application.add_handler(CommandHandler("approve", self._approve_payment))
        self.application.add_handler(CommandHandler("pending", self._show_pending_approvals))
        self.application.add_handler(CommandHandler("approve_all", self._approve_all_pending))
        self.application.add_handler(CommandHandler("draw", self._manual_draw))
        self.application.add_handler(CommandHandler("announce_100", lambda u,c: self._announce_winners(u,c,100)))
        self.application.add_handler(CommandHandler("announce_200", lambda u,c: self._announce_winners(u,c,200)))
        self.application.add_handler(CommandHandler("announce_300", lambda u,c: self._announce_winners(u,c,300)))
        
        # User commands
        self.application.add_handler(CommandHandler("start", self._start))
        self.application.add_handler(CommandHandler("numbers", self._available_numbers))
        self.application.add_handler(CommandHandler("mytickets", self._show_user_tickets))
        self.application.add_handler(CommandHandler("progress", self._show_progress))
        self.application.add_handler(CommandHandler("winners", self._show_past_winners))
        
        # Purchase conversation
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('buy', self._start_purchase)],
            states={
                SELECT_TIER: [
                    MessageHandler(filters.Regex(r'^(100|200|300)$'), self._select_tier),
                    # Use filters.CallbackQuery for callback data regex matching
                    MessageHandler(filters.CallbackQuery(pattern=r'^tier_(100|200|300)$'), self._select_tier_callback)
                ],
                SELECT_NUMBER: [
                    MessageHandler(filters.Regex(r'^([1-9][0-9]?|100)$'), self._select_number),
                    # Use filters.CallbackQuery for callback data regex matching
                    MessageHandler(filters.CallbackQuery(pattern=r'^num_([1-9][0-9]?|100)$'), self._select_number_callback),
                    MessageHandler(filters.CallbackQuery(pattern=r'^show_all_numbers_([1-9][0-9]?|100)$'), self._select_number_callback)
                ],
                PAYMENT_PROOF: [MessageHandler(filters.PHOTO, self._receive_payment_proof)]
            },
            fallbacks=[CommandHandler('cancel', self._cancel_purchase)]
        )
        self.application.add_handler(conv_handler)
        # Add a catch-all for unknown commands/messages to provide user feedback
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_unknown_message))
        self.application.add_handler(MessageHandler(filters.COMMAND, self._handle_unknown_command))


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
                return ConversationHandler.END # End conversation due to communication issue


    async def _select_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handles text input for number selection (fallback if not using inline keyboard)."""
        # This function might be hit if user types number directly.
        # It calls the same logic as the callback version.
        try:
            number = int(update.message.text)
            if not (1 <= number <= 100):
                await update.message.reply_text("Please enter a valid number between 1 and 100.")
                return SELECT_NUMBER
            
            # Simulate callback query for consistent logic
            class MockQuery:
                def __init__(self, data, from_user, message): # Added message attribute
                    self.data = data
                    self.from_user = from_user
                    self.message = message # Store message object
                async def answer(self): pass
                async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
                    await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode) # Use message to reply
            
            mock_query = MockQuery(f"num_{number}", update.effective_user, update.message)
            return await self._select_number_callback(mock_query, context)
            
        except ValueError:
            await update.message.reply_text("Please enter a valid number.")
            return SELECT_NUMBER
        except TelegramError as e:
            logging.error(f"Telegram API error in _select_number: {e}")
            await update.message.reply_text("❌ An error occurred. Please try again.")
            return ConversationHandler.END


    async def _receive_payment_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handles the receipt of payment proof (photo)."""
        user_id = update.effective_user.id
        photo_id = update.message.photo[-1].file_id # Get the highest resolution photo file_id
        number = context.user_data.get('number')
        tier = context.user_data.get('tier')
        
        if not number or not tier:
            await update.message.reply_text("❌ An error occurred with your reservation details. Please start the purchase process again with /buy.")
            return ConversationHandler.END

        with Session() as session:
            try:
                user_db = session.query(User).filter_by(telegram_id=user_id).first()
                if not user_db:
                    await update.message.reply_text("❌ User not found. Please /start again.")
                    return ConversationHandler.END

                # Find the reservation for the current user and the specific number/tier
                reservation = session.query(ReservedNumber).filter_by(
                    user_id=user_db.id,
                    number=number,
                    tier=tier
                ).first()
                
                if not reservation:
                    await update.message.reply_text("❌ Your reservation expired or was not found. Please start over with /buy.")
                    return ConversationHandler.END
                    
                # Update reservation with photo ID
                reservation.photo_id = photo_id
                session.commit()
                
                # Notify all configured admins
                for admin_id in ADMIN_IDS:
                    try:
                        await self.application.bot.send_photo(
                            chat_id=admin_id,
                            photo=photo_id,
                            caption=(f"🔄 Payment Proof Received 🔄\n\n"
                                       f"<b>User:</b> @{update.effective_user.username or user_id}\n"
                                       f"<b>Number:</b> #{number}\n"
                                       f"<b>Tier:</b> {tier} Birr\n\n"
                                       f"To approve, use: <code>/approve {number} {tier}</code>"),
                            parse_mode='HTML'
                        )
                    except TelegramError as e:
                        logging.error(f"Failed to send payment proof to admin {admin_id}: {e}")
                
                await update.message.reply_text(
                    "📨 <b>Payment Received!</b>\n\n"
                    "Your payment proof has been submitted. An admin will verify your payment shortly.\n"
                    "We appreciate your patience!",
                    parse_mode='HTML'
                )
                return ConversationHandler.END
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error saving payment proof for user {user_id}: {e}")
                await update.message.reply_text("❌ An error occurred while saving your payment proof. Please try again.")
                return ConversationHandler.END
            except TelegramError as e:
                logging.error(f"Telegram API error receiving payment proof: {e}")
                return ConversationHandler.END

    async def _approve_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command to approve a payment and issue a ticket."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        try:
            if len(context.args) < 2:
                await update.message.reply_text("Usage: /approve NUMBER TIER (e.g., /approve 5 100)")
                return
                
            number = int(context.args[0])
            tier = int(context.args[1])
            
            with Session() as session:
                try:
                    # Find the pending reservation with a photo_id
                    reservation = session.query(ReservedNumber).filter_by(
                        number=number,
                        tier=tier
                    ).first()
                    
                    if not reservation:
                        await update.message.reply_text(f"❌ No pending reservation found for number #{number} tier {tier}.")
                        return
                    
                    # Double-check: ensure no approved ticket exists for this user/number/tier
                    existing_ticket = session.query(Ticket).filter_by(
                        user_id=reservation.user_id,
                        number=number,
                        tier=tier,
                        is_approved=True
                    ).first()

                    if existing_ticket:
                        await update.message.reply_text(f"⚠️ Ticket #{number} (Tier {tier}) for user {reservation.user_id} is already approved. Cleaning up reservation.")
                        session.delete(reservation) # Clean up the stale reservation
                        session.commit()
                        return

                    # Create and record the new approved ticket
                    ticket = Ticket(
                        user_id=reservation.user_id,
                        number=number,
                        tier=tier,
                        purchased_at=reservation.reserved_at, # Use reservation time for consistency
                        is_approved=True
                    )
                    session.add(ticket)
                    
                    # Update prize pool and sold ticket count for the tier
                    settings = session.query(LotterySettings).filter_by(tier=tier).first()
                    if settings:
                        settings.sold_tickets += 1
                        settings.prize_pool += tier * 0.5
                    else:
                        logging.warning(f"LotterySettings for tier {tier} not found. Prize pool not updated.")
                    
                    # After approving, check if this tier is now sold out to trigger a draw
                    if settings and settings.sold_tickets >= settings.total_tickets:
                        await self._conduct_draw(session, tier)
                    
                    # Delete the reservation as it's now approved
                    session.delete(reservation)
                    session.commit() # Commit all changes in this block

                    # Notify the user whose ticket was approved
                    user = session.query(User).get(ticket.user_id)
                    if user:
                        try:
                            await self.application.bot.send_message(
                                chat_id=user.telegram_id,
                                text=f"🎉 <b>Payment Approved!</b>\n\nYour ticket <b>#{number}</b> for {tier} Birr is now confirmed and entered into the draw! Good luck!",
                                parse_mode='HTML'
                            )
                        except TelegramError as e:
                            logging.error(f"Failed to notify user {user.telegram_id} after approval: {e}")
                    else:
                        logging.error(f"User {ticket.user_id} not found to notify after approval.")
                    
                    await update.message.reply_text(f"✅ Approved ticket #{number} (Tier {tier}).")

                except SQLAlchemyError as e:
                    session.rollback() # Rollback changes if a database error occurs
                    logging.error(f"Database error during payment approval: {e}")
                    await update.message.reply_text("❌ A database error occurred during approval. Please check logs.")
                except Exception as e:
                    logging.error(f"Unhandled error during payment approval: {e}")
                    await update.message.reply_text(f"❌ An unexpected error occurred: {e}. Please check logs.")

        except ValueError:
            await update.message.reply_text("Invalid arguments. Usage: /approve NUMBER TIER")
        except TelegramError as e:
            logging.error(f"Telegram API error in _approve_payment: {e}")


    async def _show_pending_approvals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lists all tickets awaiting payment approval for admins."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        with Session() as session:
            try:
                # Retrieve reservations that have a photo_id but are not yet approved
                # (is_approved check is implicitly handled by the reservation not being deleted)
                pending = session.query(ReservedNumber).filter(
                    ReservedNumber.photo_id.isnot(None)
                ).order_by(ReservedNumber.reserved_at).all()
                
                if not pending:
                    await update.message.reply_text("No pending approvals at this time.")
                    return
                    
                message = "🔄 Pending Approvals:\n\n"
                for item in pending:
                    user = session.query(User).get(item.user_id)
                    username = f"@{user.username}" if user and user.username else f"User ID: {item.user_id}"
                    
                    message += (
                        f"<b>Ticket:</b> #{item.number} ({item.tier} Birr)\n"
                        f"<b>User:</b> {username}\n"
                        f"<b>Reserved At:</b> {item.reserved_at.strftime('%Y-%m-%d %H:%M %Z')}\n"
                        f"<b>Approve:</b> <code>/approve {item.number} {item.tier}</code>\n\n"
                    )
                
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error fetching pending approvals: {e}")
                await update.message.reply_text("❌ A database error occurred while fetching pending approvals.")
            except TelegramError as e:
                logging.error(f"Telegram API error showing pending approvals: {e}")

    async def _approve_all_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command to bulk approve all pending payments."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        with Session() as session:
            try:
                pending = session.query(ReservedNumber).filter(
                    ReservedNumber.photo_id.isnot(None)
                ).all()
                
                count = 0
                for item in pending:
                    try:
                        # Prevent duplicate approvals
                        existing_ticket = session.query(Ticket).filter_by(
                            user_id=item.user_id,
                            number=item.number,
                            tier=item.tier,
                            is_approved=True
                        ).first()

                        if existing_ticket:
                            logging.info(f"Skipping approval for ticket #{item.number} (Tier {item.tier}) for user {item.user_id} - already approved. Deleting reservation.")
                            session.delete(item)
                            continue

                        # Create and add the new ticket
                        ticket = Ticket(
                            user_id=item.user_id,
                            number=item.number,
                            tier=item.tier,
                            purchased_at=item.reserved_at,
                            is_approved=True
                        )
                        session.add(ticket)
                        
                        # Update lottery settings for the tier
                        settings = session.query(LotterySettings).filter_by(tier=item.tier).first()
                        if settings:
                            settings.sold_tickets += 1
                            settings.prize_pool += item.tier * 0.5
                        
                        # Check for draw trigger
                        if settings and settings.sold_tickets >= settings.total_tickets:
                            await self._conduct_draw(session, item.tier)
                        
                        # Notify the user
                        user = session.query(User).get(item.user_id)
                        if user:
                            try:
                                await self.application.bot.send_message(
                                    chat_id=user.telegram_id,
                                    text=f"🎉 Payment Approved!\n\nYour ticket #{item.number} for {item.tier} Birr is now confirmed!"
                                )
                            except TelegramError as e:
                                logging.error(f"Failed to notify user {user.telegram_id} during bulk approval: {e}")
                        
                        session.delete(item) # Delete reservation after successful processing
                        count += 1
                    except SQLAlchemyError as inner_e:
                        logging.error(f"Database error processing item {item.number} (Tier {item.tier}) during bulk approval: {inner_e}")
                        session.rollback() # Rollback internal transaction if error, but try next item
                    except Exception as inner_e:
                        logging.error(f"Unexpected error processing item {item.number} (Tier {item.tier}) during bulk approval: {inner_e}")
            
                session.commit() # Final commit for all processed items
                await update.message.reply_text(f"✅ Approved {count} tickets in bulk.")
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during bulk approval operation: {e}")
                await update.message.reply_text("❌ A database error occurred during bulk approval. Some tickets might not have been processed.")
            except TelegramError as e:
                logging.error(f"Telegram API error sending bulk approval confirmation: {e}")


    # ============= DRAW SYSTEM =============
    async def _conduct_draw(self, session, tier: int):
        """Conducts a lottery draw for a specific tier if it's sold out."""
        # Find the last announced draw for this tier to ensure we only consider
        # tickets purchased *after* that draw.
        last_draw = session.query(LotteryDraw).filter_by(
            tier=tier, status='announced'
        ).order_by(LotteryDraw.drawn_at.desc()).first()
        
        # Query for all *approved* tickets for this tier.
        query = session.query(Ticket).filter_by(tier=tier, is_approved=True)
        if last_draw:
            query = query.filter(Ticket.purchased_at > last_draw.drawn_at)
            
        tickets = query.all()
        
        # If no eligible tickets, log warning and potentially reset counters
        if not tickets:
            logging.warning(f"No new approved tickets found for tier {tier} draw since last announcement. Checking settings.")
            settings = session.query(LotterySettings).filter_by(tier=tier).first()
            if settings and settings.sold_tickets >= settings.total_tickets:
                # If tier is marked as sold out but no tickets, reset for next round
                settings.sold_tickets = 0
                settings.prize_pool = 0
                session.commit()
                if ADMIN_IDS:
                    try:
                        await self.application.bot.send_message(
                            chat_id=ADMIN_IDS[0], # Notify first admin
                            text=f"⚠️ Tier {tier} was marked as sold out, but no new unique tickets found for a draw. Counters have been reset."
                        )
                    except TelegramError as e:
                        logging.error(f"Failed to notify admin about no-ticket draw reset: {e}")
            return # Exit as no draw can be conducted

        # Randomly select the winning ticket
        winner_ticket = random.choice(tickets)
        
        # Get the current prize pool for this tier
        settings = session.query(LotterySettings).filter_by(tier=tier).first()
        prize = settings.prize_pool if settings else 0.0 # Default to 0.0 if settings not found
        
        # Record the draw
        draw = LotteryDraw(
            winning_number=winner_ticket.number,
            tier=tier,
            status='pending', # Draw is pending announcement by admin
            drawn_at=datetime.now(pytz.utc)
        )
        session.add(draw)
        session.flush()  # Ensures draw.id is populated before winner_entry depends on it
        
        # Record the winner
        winner_entry = Winner(
            draw_id=draw.id,
            user_id=winner_ticket.user_id,
            number=winner_ticket.number,
            tier=tier,
            prize=prize
        )
        session.add(winner_entry)
        
        # Reset tier counters immediately after a draw has been determined
        if settings:
            settings.sold_tickets = 0
            settings.prize_pool = 0
        
        session.commit() # Commit all changes from the draw
        
        # Notify admins about the draw and prompt for announcement
        for admin_id in ADMIN_IDS:
            try:
                await self.application.bot.send_message(
                    chat_id=admin_id,
                    text=(f"🎰 Automatic Draw Complete (Tier {tier} Birr) 🎰\n\n"
                                f"<b>Winning Number:</b> #{winner_ticket.number}\n"
                                f"<b>Winner User ID:</b> {winner_ticket.user_id}\n"
                                f"<b>Prize:</b> {prize:.2f} Birr\n\n"
                                f"<b>Action Required:</b> Please use <code>/announce_{tier}</code> to publish this winner to the channel and make it official!"),
                    parse_mode='HTML'
                )
            except TelegramError as e:
                logging.error(f"Failed to notify admin {admin_id} about automatic draw: {e}")

    async def _manual_draw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin-triggered manual draw for a specific tier."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
            
        try:
            if not context.args or not context.args[0].isdigit():
                await update.message.reply_text("Usage: /draw <TIER> (e.e.g., /draw 100). Valid tiers: 100, 200, 300.")
                return

            tier = int(context.args[0])
            if tier not in [100, 200, 300]:
                await update.message.reply_text("Invalid tier. Please use 100, 200, or 300.")
                return

            await update.message.reply_text(f"Attempting to conduct manual draw for Tier {tier}...")
            with Session() as session:
                await self._conduct_draw(session, tier) # Call the core draw logic
            await update.message.reply_text(f"Manual draw process initiated for Tier {tier}. Check admin notifications for results and announcement command.")
        except SQLAlchemyError as e:
            logging.error(f"Database error during manual draw: {e}")
            await update.message.reply_text("❌ A database error occurred during the manual draw. Please try again.")
        except Exception as e:
            logging.error(f"Unexpected error during manual draw: {e}")
            await update.message.reply_text(f"❌ An unexpected error occurred: {e}. Please check logs.")
        except TelegramError as e:
            logging.error(f"Telegram API error in _manual_draw: {e}")


    async def _announce_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE, tier: int):
        """Admin command to publish the results of a draw to the public channel."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        if CHANNEL_ID is None:
            await update.message.reply_text("❌ CHANNEL_ID environment variable is not configured or invalid. Cannot announce winners.")
            return

        with Session() as session:
            try:
                # Find the most recent draw for this tier that is still 'pending' announcement
                winner_entry = session.query(Winner).join(LotteryDraw).filter(
                    LotteryDraw.tier == tier,
                    LotteryDraw.status == 'pending'
                ).order_by(LotteryDraw.drawn_at.desc()).first()
                
                if not winner_entry:
                    await update.message.reply_text(f"No pending winners to announce for Tier {tier}.")
                    # Provide info on last announced winner if no pending ones
                    last_announced_winner = session.query(Winner).join(LotteryDraw).filter(
                        LotteryDraw.tier == tier,
                        LotteryDraw.status == 'announced'
                    ).order_by(LotteryDraw.drawn_at.desc()).first()
                    if last_announced_winner:
                        user_last = session.query(User).get(last_announced_winner.user_id)
                        username_last = f"@{user_last.username}" if user_last and user_last.username else f"User ID: {last_announced_winner.user_id}"
                        await update.message.reply_text(f"Last announced winner for Tier {tier} was #{last_announced_winner.number} ({username_last}) on {last_announced_winner.draw.drawn_at.strftime('%Y-%m-%d %H:%M %Z')}.")
                    return
                
                # Mark the draw as 'announced'
                winner_entry.draw.status = 'announced'
                session.commit()
                
                # Get user information for the winner to display in the announcement
                user = session.query(User).get(winner_entry.user_id)
                username = f"@{user.username}" if user and user.username else f"User ID: {winner_entry.user_id}"
                
                message = (
                    f"🏆 **Tier {tier} Birr Winner Announcement!** 🏆\n\n"
                    f"🎫 Winning Number: `{winner_entry.number}`\n"
                    f"💰 Prize Amount: `{winner_entry.prize:.2f} Birr`\n"
                    f"👤 Winner: {username}\n\n"
                    f"Congratulations to our lucky winner!\n"
                    f"Please contact {ADMIN_CONTACT_HANDLE} to claim your prize!"
                )
                
                try:
                    await self.application.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=message,
                        parse_mode='Markdown' # Use Markdown for bold and monospace
                    )
                    await update.message.reply_text(f"✅ Tier {tier} results announced successfully to channel!")
                except TelegramError as e:
                    logging.error(f"Failed to announce winner to channel {CHANNEL_ID}: {e}")
                    await update.message.reply_text(f"❌ Failed to announce to channel (Error: {e}). Check CHANNEL_ID and bot permissions.")
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during winner announcement: {e}")
                await update.message.reply_text("❌ A database error occurred during winner announcement. Please check logs.")
            except Exception as e:
                logging.error(f"Unexpected error during winner announcement: {e}")
                await update.message.reply_text(f"❌ An unexpected error occurred: {e}. Please check logs.")


    # ============= USER COMMANDS =============
    async def _show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays the current ticket sales progress for all active tiers."""
        with Session() as session:
            try:
                tiers = session.query(LotterySettings).filter_by(is_active=True).order_by(LotterySettings.tier).all()
                
                message = "📊 <b>Ticket Sales Progress</b>\n\n"
                if not tiers:
                    message += "No active lottery tiers found at the moment."
                else:
                    for settings in tiers:
                        remaining = settings.total_tickets - settings.sold_tickets
                        message += (
                            f"<b>Tier {settings.tier} Birr:</b>\n"
                            f"• Sold: {settings.sold_tickets} / {settings.total_tickets}\n"
                            f"• Remaining: {remaining}\n"
                            f"• Current Prize Pool: {settings.prize_pool:.2f} Birr\n\n"
                        )
                
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing progress: {e}")
                await update.message.reply_text("❌ An error occurred while fetching progress data. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error showing progress: {e}")

    async def _show_past_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays the last 5 announced winners across all tiers."""
        with Session() as session:
            try:
                winners = session.query(Winner, LotteryDraw).join(LotteryDraw).filter(
                    LotteryDraw.status == 'announced'
                ).order_by(LotteryDraw.drawn_at.desc()).limit(5).all()
                
                if not winners:
                    await update.message.reply_text("No past winners yet. Be the first to win!")
                    return
                    
                message = "🏆 <b>Past Winners (Last 5)</b> 🏆\n\n"
                for winner_entry, draw in winners:
                    user = session.query(User).get(winner_entry.user_id)
                    username = f"@{user.username}" if user and user.username else f"User ID: {winner_entry.user_id}"
                    
                    message += (
                        f"<b>Tier {winner_entry.tier} Birr:</b>\n"
                        f"• Winning Number: #{winner_entry.number}\n"
                        f"• Winner: {username}\n"
                        f"• Prize: {winner_entry.prize:.2f} Birr\n"
                        f"• Draw Date: {draw.drawn_at.strftime('%Y-%m-%d %H:%M %Z')}\n\n"
                    )
                
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing past winners: {e}")
                await update.message.reply_text("❌ An error occurred while fetching past winners. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error showing past winners: {e}")


    async def _show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays the current user's purchased tickets."""
        user_telegram_id = update.effective_user.id
        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                if not user:
                    await update.message.reply_text("You need to /start first to register your account and see your tickets!")
                    return
                    
                tickets = session.query(Ticket).filter_by(user_id=user.id).order_by(Ticket.purchased_at.desc()).all()
                
                if not tickets:
                    await update.message.reply_text("You don't have any tickets yet! Use /buy to get one and try your luck!")
                    return
                    
                message = "🎫 <b>Your Tickets:</b>\n\n"
                for ticket in tickets:
                    status_text = "Approved ✅" if ticket.is_approved else "Pending Verification ⏳"
                    message += (
                        f"• Ticket #{ticket.number} (Tier {ticket.tier} Birr)\n"
                        f"  <i>Purchased: {ticket.purchased_at.strftime('%Y-%m-%d %H:%M %Z')}</i>\n"
                        f"  <b>Status:</b> {status_text}\n\n"
                    )
                
                await update.message.reply_text(message, parse_mode='HTML')
            except SQLAlchemyError as e:
                logging.error(f"Database error showing user tickets for {user_telegram_id}: {e}")
                await update.message.reply_text("❌ An error occurred while fetching your tickets. Please try again later.")
            except TelegramError as e:
                logging.error(f"Telegram API error showing user tickets: {e}")


    async def _cancel_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancels the current purchase conversation and deletes any active reservation."""
        user_telegram_id = update.effective_user.id
        number_reserved = context.user_data.get('number')
        tier_reserved = context.user_data.get('tier')
        
        with Session() as session:
            try:
                user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                if user and number_reserved and tier_reserved: # Only try to delete if details are present
                    # Find and delete the specific reservation for this user, number, and tier
                    reservation_to_delete = session.query(ReservedNumber).filter_by(
                        user_id=user.id,
                        number=number_reserved,
                        tier=tier_reserved
                    ).first()
                    if reservation_to_delete:
                        session.delete(reservation_to_delete)
                        session.commit()
                        logging.info(f"Reservation for user {user_telegram_id}, number {number_reserved}, tier {tier_reserved} cancelled and deleted.")
                    else:
                        logging.info(f"No active reservation found for user {user_telegram_id} during cancellation (possibly expired or already processed).")
                elif not user:
                    logging.info(f"User {user_telegram_id} attempted to cancel but not found in DB.")
                else:
                    logging.info(f"User {user_telegram_id} cancelled, but no active reservation data in user_data.")

                await update.message.reply_text("❌ Purchase cancelled. You can start a new purchase with /buy.")
                # Clear user_data for the conversation to ensure a clean slate for next purchase
                context.user_data.clear() 
                return ConversationHandler.END
            except SQLAlchemyError as e:
                session.rollback()
                logging.error(f"Database error during purchase cancellation for user {user_telegram_id}: {e}")
                await update.message.reply_text("❌ An error occurred while cancelling your purchase. Please try again.")
                return ConversationHandler.END
            except TelegramError as e:
                logging.error(f"Telegram API error during purchase cancellation: {e}")
                return ConversationHandler.END

    async def _handle_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles unknown commands."""
        if update.message: # Ensure there's a message object
            await update.message.reply_text(f"Sorry, I don't understand the command '{update.message.text}'. Please use one of the available commands like /start or /buy.")

    async def _handle_unknown_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles unknown non-command messages."""
        if update.message and update.message.chat.type == "private" and not context.user_data: # If no active conversation
            await update.message.reply_text("I'm a lottery bot! Use commands like /start, /buy, /progress. If you need help, type /help.")

    async def run_polling_bot(self):
        """Starts the bot's polling mechanism."""
        logging.info("Starting Telegram bot polling...")
        try:
            # The .run_polling() method needs an event loop to be present in its thread.
            # asyncio.run() will manage this for the calling thread.
            await self.application.run_polling(drop_pending_updates=True)
        except TelegramError as e:
            logging.critical(f"Telegram Bot polling failed: {e}")
        except Exception as e:
            logging.critical(f"Unhandled exception in bot polling loop: {e}")


# --- Global instance of the bot for internal use (e.g., scheduled tasks) ---
# This will be initialized only once when the `run` function is called by Gunicorn
telegram_bot_instance: Optional[LotteryBot] = None

# --- Main Application Start Point for Gunicorn ---
def run(environ, start_response):
    """
    Initializes the database, starts the Telegram bot in a background thread,
    and returns the Flask application for Gunicorn to serve.
    """
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    logging.info("Starting Lottery Bot application...")

    try:
        init_db()
    except Exception as e:
        logging.critical(f"Failed to initialize database during startup: {e}. Flask health check will likely fail.")
        raise # Re-raise to ensure Render service restarts if DB is critical for startup
    
    global telegram_bot_instance # Declare intent to modify global variable
    if telegram_bot_instance is None: # Corrected: use 'is None' for identity check in Python
        try:
            # Start APScheduler in its own thread. This does NOT need an asyncio loop.
            scheduler_thread = Thread(target=LotteryBot.init_schedulers_standalone, daemon=True)
            scheduler_thread.start()
            logging.info("APScheduler background thread launched.")

            # Function to run the bot's asyncio polling loop in its own thread
            def start_bot_async_loop_dedicated_thread():
                # This ensures an event loop is explicitly created and set for THIS thread.
                # All async operations for the bot instance will use this loop.
                bot_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(bot_loop)
                
                # Instantiate the LotteryBot *within* this thread, after the event loop is set.
                global telegram_bot_instance # Access and assign to the global instance
                telegram_bot_instance = LotteryBot() # Bot object now built in this thread
                
                bot_loop.run_until_complete(telegram_bot_instance.run_polling_bot())

            # Start the bot's polling in a new, dedicated daemon thread
            bot_polling_thread = Thread(target=start_bot_async_loop_dedicated_thread, daemon=True)
            bot_polling_thread.start()
            
            logging.info("Telegram bot polling background thread started.")
        except ValueError as e:
            logging.critical(f"Bot initialization failed due to configuration error: {e}. Bot will not run.")
            # If bot_token is missing, we don't start the bot.
        except Exception as e:
            logging.critical(f"Unexpected error during bot initialization: {e}. Bot may not be running.")
    else:
        logging.info("Telegram bot already initialized for this Gunicorn worker.")
    
    logging.info("Returning Flask WSGI application to Gunicorn.")
    return app(environ, start_response)


# --- Local Development/Testing Entry Point ---
if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    logging.info("Running application in local development mode.")

    try:
        init_db()
    except Exception as e:
        logging.critical(f"Failed to initialize database: {e}")
        exit(1)
        
    # Start Flask health check server in a separate thread for local development
    flask_thread = Thread(target=lambda: app.run(host='0.0.0.0', port=5000), daemon=True)
    flask_thread.start()
    logging.info("Flask health check running on port 5000 (local dev mode)")
    
    # Local bot instance setup
    try:
        # Function to run the bot's asyncio polling loop in its own thread
        async def start_local_bot_async_dedicated_thread():
            # Create and set a new event loop for this thread
            local_bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(local_bot_loop)

            # Instantiate the LotteryBot *within* this thread, after the event loop is set.
            local_bot_instance_inner = LotteryBot() 
            
            await local_bot_instance_inner.run_polling_bot()
        
        # Start APScheduler for local dev instance in its own thread
        scheduler_thread_local = Thread(target=LotteryBot.init_schedulers_standalone, daemon=True)
        scheduler_thread_local.start()
        logging.info("APScheduler background thread launched (local dev mode).")

        # Start the bot's polling in a new, dedicated daemon thread
        bot_polling_thread_local = Thread(target=lambda: asyncio.run(start_local_bot_async_dedicated_thread()), daemon=True)
        bot_polling_thread_local.start()
        logging.info("Telegram bot polling started in background (local dev mode)")
        
        # Keep the main thread alive.
        flask_thread.join() 
        bot_polling_thread_local.join()

    except ValueError as e:
        logging.critical(f"Local bot initialization failed due to configuration error: {e}.")
    except Exception as e:
        logging.critical(f"Unexpected error during local bot setup: {e}.")
