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

# --- Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]
CHANNEL_ID = os.getenv("CHANNEL_ID")
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

# Conversation states
SELECT_TIER, SELECT_NUMBER, PAYMENT_PROOF = range(3)

# --- Database Setup ---
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, ForeignKey, DateTime, Boolean, Float
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.sql import func

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
    balance = Column(Integer, default=0)
    tickets = relationship("Ticket", back_populates="user")

class Ticket(Base):
    __tablename__ = 'tickets'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    number = Column(Integer, nullable=False)
    tier = Column(Integer, nullable=False)
    purchased_at = Column(DateTime, default=datetime.utcnow)
    is_approved = Column(Boolean, default=False)
    user = relationship("User", back_populates="tickets")

class LotteryDraw(Base):
    __tablename__ = 'draws'
    id = Column(Integer, primary_key=True)
    winning_number = Column(Integer)
    tier = Column(Integer)
    drawn_at = Column(DateTime, default=datetime.utcnow)
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
    reserved_at = Column(DateTime, default=datetime.utcnow)
    photo_id = Column(String(255))

def init_db():
    """Initialize database with all required tables"""
    try:
        # Only create backup dir if not using Render's read-only filesystem
        if not DATABASE_URL.startswith('postgres'):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            
        Base.metadata.create_all(engine)
        
        # Initialize ticket tiers if they don't exist
        with Session() as session:
            for tier in [100, 200, 300]:
                if not session.query(LotterySettings).filter_by(tier=tier).first():
                    session.add(LotterySettings(tier=tier))
            session.commit()
            
        logging.info("Database initialized successfully")
    except OperationalError as e:
        logging.critical(f"Database connection failed: {e}")
        raise
    except Exception as e:
        logging.critical(f"Database initialization error: {e}")
        raise

# --- Backup System ---
def backup_db():
    """Create timestamped database backup"""
    try:
        if DATABASE_URL.startswith('postgres'):
            logging.info("Skipping backup for PostgreSQL (handled by Render)")
            return
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        backup_path = f"{BACKUP_DIR}/backup_{timestamp}.db"
        
        # For SQLite, we can just copy the file
        if DATABASE_URL.startswith("sqlite"):
            import shutil
            db_file = DATABASE_URL.split("///")[-1]
            shutil.copy2(db_file, backup_path)
            logging.info(f"Database backed up to {backup_path}")
            clean_old_backups()
    except Exception as e:
        logging.error(f"Backup failed: {str(e)}")

def clean_old_backups(keep_last=5):
    """Rotate backup files"""
    try:
        backups = sorted(os.listdir(BACKUP_DIR))
        for old_backup in backups[:-keep_last]:
            os.remove(f"{BACKUP_DIR}/{old_backup}")
    except Exception as e:
        logging.error(f"Backup cleanup failed: {str(e)}")

def clean_expired_reservations():
    """Remove reservations older than 24 hours"""
    expiry_time = datetime.utcnow() - timedelta(hours=24)
    with Session() as session:
        session.query(ReservedNumber).filter(ReservedNumber.reserved_at < expiry_time).delete()
        session.commit()

# --- Flask Health Check ---
app = Flask(__name__)

@app.route('/health')
def health_check():
    try:
        # Test database connection
        with Session() as session:
            session.execute("SELECT 1")
        
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

# --- Lottery Bot Implementation ---
class LotteryBot:
    def __init__(self):
        self._validate_config()
        self.application = ApplicationBuilder().token(BOT_TOKEN).build()
        self.user_activity = {}
        self._setup_handlers()
        self._init_schedulers()

    def _validate_config(self):
        """Verify required configuration"""
        if not BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable required")
        if not ADMIN_IDS:
            logging.warning("No ADMIN_IDS configured - admin commands disabled")

    def _init_schedulers(self):
        """Initialize scheduled tasks"""
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(backup_db, 'interval', hours=6)
        scheduler.add_job(clean_expired_reservations, 'interval', hours=1)
        scheduler.start()

    def _check_spam(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Anti-spam protection with 2-second cooldown"""
        user_id = update.effective_user.id
        now = datetime.now().timestamp()
        if user_id in self.user_activity and now - self.user_activity[user_id] < 2:
            update.message.reply_text("⚠️ Please wait before sending another request")
            return True
        self.user_activity[user_id] = now
        return False

    def _check_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check if bot is in maintenance mode"""
        if MAINTENANCE and update.effective_user.id not in ADMIN_IDS:
            update.message.reply_text("🔧 The bot is currently under maintenance. Please try again later.")
            return True
        return False

    def _setup_handlers(self):
        """Configure all bot command handlers"""
        # Anti-spam and maintenance checks
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
                SELECT_TIER: [MessageHandler(filters.Regex(r'^(100|200|300)$'), self._select_tier)],
                SELECT_NUMBER: [MessageHandler(filters.Regex(r'^([1-9][0-9]?|100)$'), self._select_number)],
                PAYMENT_PROOF: [MessageHandler(filters.PHOTO, self._receive_payment_proof)]
            },
            fallbacks=[CommandHandler('cancel', self._cancel_purchase)]
        )
        self.application.add_handler(conv_handler)

    # ============= ADMIN COMMANDS =============
    async def _enable_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enable maintenance mode (admin only)"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        global MAINTENANCE
        MAINTENANCE = True
        await update.message.reply_text("🛠 Maintenance mode ENABLED")

    async def _disable_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disable maintenance mode (admin only)"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        global MAINTENANCE
        MAINTENANCE = False
        await update.message.reply_text("✅ Maintenance mode DISABLED")

    # ============= USER MANAGEMENT =============
    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
            if not user:
                user = User(
                    telegram_id=update.effective_user.id,
                    username=update.effective_user.username
                )
                session.add(user)
                session.commit()
                await update.message.reply_text("🎉 Welcome to Lottery Bot!")
            else:
                await update.message.reply_text(f"👋 Welcome back, {user.username}!")

    # ============= TICKET MANAGEMENT =============
    def _get_available_numbers(self, tier: int) -> List[int]:
        """Get available numbers for a tier"""
        with Session() as session:
            # Get reserved numbers
            reserved = {r.number for r in session.query(ReservedNumber.number).filter_by(tier=tier).all()}
            
            # Get confirmed tickets
            confirmed = {t.number for t in session.query(Ticket.number).filter_by(tier=tier).all()}
            
            return sorted(list(set(range(1, 101)) - reserved - confirmed))

    def _is_number_available(self, number: int, tier: int) -> bool:
        """Check if number is available"""
        return number in self._get_available_numbers(tier)

    async def _available_numbers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show available numbers for all tiers"""
        tiers = {100: "💵 100 Birr", 200: "💰 200 Birr", 300: "💎 300 Birr"}
        message = "🔢 Available Numbers:\n\n"
        
        for tier, label in tiers.items():
            available = self._get_available_numbers(tier)
            message += f"{label}: {', '.join(map(str, available[:15]))}\n\n"
        
        await update.message.reply_text(message)

    # ============= PURCHASE FLOW =============
    async def _start_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start ticket purchase process"""
        if MAINTENANCE:
            await update.message.reply_text("🚧 Bot is under maintenance. Please try again later.")
            return ConversationHandler.END
            
        clean_expired_reservations()
        
        await update.message.reply_text(
            "🎟️ <b>Select Ticket Tier</b>\n\n"
            "1. 100 Birr\n2. 200 Birr\n3. 300 Birr\n\n"
            "Reply with the amount (100, 200, or 300)",
            parse_mode='HTML'
        )
        return SELECT_TIER

    async def _select_tier(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle tier selection"""
        tier = int(update.message.text)
        context.user_data['tier'] = tier
        available = self._get_available_numbers(tier)
        
        # Create number selection keyboard
        buttons = [
            InlineKeyboardButton(str(num), callback_data=f"num_{num}")
            for num in available[:20]  # Show first 20 numbers
        ]
        
        await update.message.reply_text(
            f"🔢 Available Numbers for {tier} Birr:",
            reply_markup=InlineKeyboardMarkup([buttons[i:i+5] for i in range(0, len(buttons), 5)])
        )
        return SELECT_NUMBER

    async def _select_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Reserve selected number"""
        number = int(update.message.text)
        tier = context.user_data['tier']
        user_id = update.effective_user.id
        
        if not self._is_number_available(number, tier):
            await update.message.reply_text("❌ Number already taken! Choose another:")
            return SELECT_NUMBER
        
        # Reserve number
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                await update.message.reply_text("❌ User not found. Please /start again.")
                return ConversationHandler.END
                
            reserved = ReservedNumber(
                number=number,
                tier=tier,
                user_id=user.id
            )
            session.add(reserved)
            session.commit()
        
        context.user_data['number'] = number
        
        await update.message.reply_text(
            f"✅ <b>Number #{number} Reserved</b>\n\n"
            f"Send payment of {tier} Birr to:\n"
            "<code>CBE: 1000295626473</code>\n\n"
            "Then upload receipt photo",
            parse_mode='HTML'
        )
        return PAYMENT_PROOF

    async def _receive_payment_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle payment proof upload"""
        user_id = update.effective_user.id
        photo_id = update.message.photo[-1].file_id
        number = context.user_data['number']
        tier = context.user_data['tier']
        
        # Store payment proof
        with Session() as session:
            reservation = session.query(ReservedNumber).filter_by(
                number=number,
                tier=tier
            ).first()
            
            if not reservation:
                await update.message.reply_text("❌ Reservation expired. Please start over.")
                return ConversationHandler.END
                
            reservation.photo_id = photo_id
            session.commit()
            
            # Notify all admins
            for admin_id in ADMIN_IDS:
                await self.application.bot.send_photo(
                    chat_id=admin_id,
                    photo=photo_id,
                    caption=f"🔄 Payment Proof\n\nUser: @{update.effective_user.username}\n"
                                   f"Number: #{number}\nTier: {tier} Birr\n\n"
                                   f"Approve: /approve {number} {tier}"
                )
        
        await update.message.reply_text(
            "📨 <b>Payment Received!</b>\n\n"
            "Admin will verify your payment shortly",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    async def _approve_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin approval of payment"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        try:
            if len(context.args) < 2:
                raise ValueError("Missing arguments")
                
            number = int(context.args[0])
            tier = int(context.args[1])
            
            with Session() as session:
                # Get reservation info
                reservation = session.query(ReservedNumber).filter_by(
                    number=number,
                    tier=tier
                ).first()
                
                if not reservation:
                    await update.message.reply_text("❌ No such reservation found")
                    return
                
                # Record ticket
                ticket = Ticket(
                    user_id=reservation.user_id,
                    number=number,
                    tier=tier,
                    is_approved=True
                )
                session.add(ticket)
                
                # Update prize pool (50% of ticket price)
                settings = session.query(LotterySettings).filter_by(tier=tier).first()
                settings.sold_tickets += 1
                settings.prize_pool += tier * 0.5
                
                # Check if tier is sold out
                if settings.sold_tickets >= settings.total_tickets:
                    await self._conduct_draw(session, tier)
                
                # Cleanup reservation
                session.delete(reservation)
                session.commit()
                
                # Notify user
                user = session.query(User).get(ticket.user_id)
                await self.application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"🎉 <b>Payment Approved!</b>\n\nTicket #{number} confirmed!",
                    parse_mode='HTML'
                )
                
                await update.message.reply_text(f"✅ Approved ticket #{number}")

        except Exception as e:
            await update.message.reply_text("Usage: /approve NUMBER TIER")
            logging.error(f"Approval error: {e}")

    async def _show_pending_approvals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List pending approvals for admins"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        with Session() as session:
            pending = session.query(ReservedNumber).filter(
                ReservedNumber.photo_id.isnot(None)
            ).order_by(ReservedNumber.reserved_at).all()
            
            if not pending:
                await update.message.reply_text("No pending approvals")
                return
                
            message = "🔄 Pending Approvals:\n\n"
            for item in pending:
                user = session.query(User).get(item.user_id)
                username = f"@{user.username}" if user.username else f"user {user.id}"
                
                message += (
                    f"#{item.number} ({item.tier} Birr)\n"
                    f"User: {username}\n"
                    f"Approve: /approve {item.number} {item.tier}\n\n"
                )
            
            await update.message.reply_text(message)

    async def _approve_all_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Bulk approve all pending payments"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        with Session() as session:
            pending = session.query(ReservedNumber).filter(
                ReservedNumber.photo_id.isnot(None)
            ).all()
            
            count = 0
            for item in pending:
                # Record ticket
                ticket = Ticket(
                    user_id=item.user_id,
                    number=item.number,
                    tier=item.tier,
                    is_approved=True
                )
                session.add(ticket)
                
                # Update prize pool (50% of ticket price)
                settings = session.query(LotterySettings).filter_by(tier=item.tier).first()
                settings.sold_tickets += 1
                settings.prize_pool += item.tier * 0.5
                
                # Check if tier is sold out
                if settings.sold_tickets >= settings.total_tickets:
                    await self._conduct_draw(session, item.tier)
                
                # Notify user
                user = session.query(User).get(item.user_id)
                await self.application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"🎉 Payment Approved!\n\nTicket #{item.number} confirmed!"
                )
                
                count += 1
            
            # Cleanup reservations
            session.query(ReservedNumber).filter(
                ReservedNumber.photo_id.isnot(None)
            ).delete()
            
            session.commit()
            
            await update.message.reply_text(f"Approved {count} tickets")

    # ============= DRAW SYSTEM =============
    async def _conduct_draw(self, session, tier: int):
        """Automatically conduct draw when tier sells out"""
        # Get all unredeemed tickets for this tier
        last_draw = session.query(func.max(LotteryDraw.drawn_at)).filter_by(tier=tier).scalar()
        query = session.query(Ticket).filter_by(tier=tier)
        
        if last_draw:
            query = query.filter(Ticket.purchased_at > last_draw)
            
        tickets = query.all()
        
        if not tickets:
            logging.warning(f"No tickets found for tier {tier} draw")
            return
        
        # Random selection
        winner = random.choice(tickets)
        
        # Get current prize pool
        settings = session.query(LotterySettings).filter_by(tier=tier).first()
        prize = settings.prize_pool
        
        # Record draw
        draw = LotteryDraw(
            winning_number=winner.number,
            tier=tier
        )
        session.add(draw)
        session.flush()  # To get the draw ID
        
        # Record winner
        winner_entry = Winner(
            draw_id=draw.id,
            user_id=winner.user_id,
            number=winner.number,
            tier=tier,
            prize=prize
        )
        session.add(winner_entry)
        
        # Reset tier counters
        settings.sold_tickets = 0
        settings.prize_pool = 0
        
        session.commit()
        
        # Notify admins
        for admin_id in ADMIN_IDS:
            await self.application.bot.send_message(
                chat_id=admin_id,
                text=f"🎰 Automatic Draw Complete (Tier {tier} Birr)\n\n"
                             f"Winner: User {winner.user_id}\n"
                             f"Number: #{winner.number}\n"
                             f"Prize: {prize:.2f} Birr\n\n"
                             f"Use /announce_{tier} to publish"
            )

    async def _manual_draw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin-triggered manual draw"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        try:
            tier = int(context.args[0])
            with Session() as session:
                await self._conduct_draw(session, tier)
            await update.message.reply_text(f"Manual draw conducted for Tier {tier}")
        except Exception as e:
            await update.message.reply_text("Usage: /draw TIER")
            logging.error(f"Manual draw error: {e}")

    async def _announce_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE, tier: int):
        """Publish winners to channel"""
        if update.effective_user.id not in ADMIN_IDS:
            return

        with Session() as session:
            winner = session.query(Winner).join(LotteryDraw).filter(
                LotteryDraw.tier == tier,
                LotteryDraw.status == 'pending'
            ).order_by(LotteryDraw.drawn_at.desc()).first()
            
            if not winner:
                await update.message.reply_text(f"No pending winners for Tier {tier}")
                return
            
            # Mark as announced
            winner.draw.status = 'announced'
            session.commit()
            
            # Get user info
            user = session.query(User).get(winner.user_id)
            username = f"@{user.username}" if user.username else f"user {user.id}"
            
            message = (
                f"🏆 **Tier {tier} Birr Winner** 🏆\n\n"
                f"🎫 Winning Number: #{winner.number}\n"
                f"💰 Prize Amount: {winner.prize:.2f} Birr\n"
                f"👤 Winner: {username}\n\n"
                f"Contact @admin to claim your prize!"
            )
            
            # Announce to channel
            if CHANNEL_ID:
                await self.application.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=message,
                    parse_mode='Markdown'
                )
            
            await update.message.reply_text(f"Tier {tier} results announced!")

    # ============= USER COMMANDS =============
    async def _show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show ticket sales progress per tier"""
        with Session() as session:
            tiers = session.query(LotterySettings).filter_by(is_active=True).all()
            
            message = "📊 Ticket Sales Progress\n\n"
            for tier in tiers:
                remaining = tier.total_tickets - tier.sold_tickets
                message += (
                    f"Tier {tier.tier} Birr:\n"
                    f"• Sold: {tier.sold_tickets}/{tier.total_tickets}\n"
                    f"• Remaining: {remaining}\n"
                    f"• Current Prize: {tier.prize_pool:.2f} Birr\n\n"
                )
            
            await update.message.reply_text(message)

    async def _show_past_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display last 5 winners"""
        with Session() as session:
            winners = session.query(Winner, LotteryDraw).join(LotteryDraw).filter(
                LotteryDraw.status == 'announced'
            ).order_by(LotteryDraw.drawn_at.desc()).limit(5).all()
            
            if not winners:
                await update.message.reply_text("No past winners yet")
                return
                
            message = "🏆 Past Winners (Last 5)\n\n"
            for winner, draw in winners:
                user = session.query(User).get(winner.user_id)
                username = f"@{user.username}" if user.username else f"user {user.id}"
                
                message += (
                    f"Tier {winner.tier} Birr:\n"
                    f"• Number: #{winner.number}\n"
                    f"• Winner: {username}\n"
                    f"• Prize: {winner.prize:.2f} Birr\n"
                    f"• Date: {draw.drawn_at.strftime('%Y-%m-%d')}\n\n"
                )
            
            await update.message.reply_text(message)

    async def _show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's purchased tickets"""
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
            if not user:
                await update.message.reply_text("You need to /start first!")
                return
                
            tickets = session.query(Ticket).filter_by(user_id=user.id).all()
            
            if not tickets:
                await update.message.reply_text("You don't have any tickets yet!")
                return
                
            message = "🎫 Your Tickets:\n\n"
            for ticket in tickets:
                message += (
                    f"#{ticket.number} ({ticket.tier} Birr)\n"
                    f"Purchased: {ticket.purchased_at.strftime('%Y-%m-%d %H:%M')}\n"
                    f"Status: {'Approved' if ticket.is_approved else 'Pending'}\n\n"
                )
            
            await update.message.reply_text(message)

    async def _cancel_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel current purchase"""
        if 'number' in context.user_data and 'tier' in context.user_data:
            with Session() as session:
                session.query(ReservedNumber).filter_by(
                    number=context.user_data['number'],
                    tier=context.user_data['tier']
                ).delete()
                session.commit()
        
        await update.message.reply_text("❌ Purchase cancelled")
        return ConversationHandler.END

    async def run(self):
        """Start the bot's polling mechanism"""
        await self.application.run_polling()

# --- Main Application Start Point for Gunicorn ---
# This function will be called by Gunicorn
def run():
    """Initializes and runs the bot alongside the Flask app."""
    # Configure logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    # Initialize database
    try:
        init_db()
    except Exception as e:
        logging.critical(f"Failed to initialize database: {e}")
        # In a production environment, you might want to raise here
        # or exit, as the app won't function without a DB.
        # For a WSGI server, raising an exception will cause it to fail startup.
        # For now, let's just log and continue, but this needs careful consideration.
        pass # Allow Flask app to start even if DB init fails, so health check can show ERROR
    
    # Start Telegram bot in a separate thread (it needs an asyncio loop)
    # Gunicorn is not async, so we run the bot in a dedicated thread.
    telegram_bot_instance = LotteryBot()
    # Create a new event loop for the bot's thread
    bot_loop = asyncio.new_event_loop()
    
    def start_bot_loop():
        asyncio.set_event_loop(bot_loop)
        bot_loop.run_until_complete(telegram_bot_instance.run())

    bot_thread = Thread(target=start_bot_loop)
    bot_thread.start()
    
    logging.info("Telegram bot started in background thread.")
    
    return app # Gunicorn expects the WSGI callable to be returned

# If you still want to run it locally with `python main.py`, keep the __name__ == '__main__' block
if __name__ == '__main__':
    # This block is only for local development/testing
    # In a production environment with Gunicorn, this block is not executed directly.
    # The 'run' function above will be the entry point for Gunicorn.
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    try:
        init_db()
    except Exception as e:
        logging.critical(f"Failed to initialize database: {e}")
        exit(1)
        
    # Start Flask health check in main thread for local run
    flask_thread = Thread(target=lambda: app.run(host='0.0.0.0', port=5000))
    flask_thread.start()
    logging.info("Flask health check running on port 5000 (local mode)")
    
    # Start bot in a separate thread for local run
    bot = LotteryBot()
    async def start_bot_async():
        await bot.run()
    
    bot_thread = Thread(target=lambda: asyncio.run(start_bot_async()))
    bot_thread.start()
    
    flask_thread.join() # Keep main thread alive for Flask in local dev
    bot_thread.join()
