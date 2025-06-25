import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta
from threading import Thread
from typing import List, Dict, Optional

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackContext,
    MessageHandler, Filters, ConversationHandler, TypeHandler
)

# ====================== CONFIGURATION ======================
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]
CHANNEL_ID = os.getenv("CHANNEL_ID")
DB_FILE = "/data/lottery_bot.db"
BACKUP_DIR = "/data/backups"
MAINTENANCE = False  # Maintenance mode flag

# Conversation states
SELECT_TIER, SELECT_NUMBER, PAYMENT_PROOF = range(3)

# ====================== DATABASE SETUP ======================
def init_db():
    """Initialize database with all required tables"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reserved_numbers (
            number INTEGER,
            tier INTEGER,
            user_id INTEGER,
            reserved_at TEXT,
            photo_id TEXT,
            PRIMARY KEY (number, tier)
        )""")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            number INTEGER,
            tier INTEGER,
            purchase_time TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS draws (
            draw_id INTEGER PRIMARY KEY AUTOINCREMENT,
            winning_number INTEGER,
            tier INTEGER,
            draw_time TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending'
        )""")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS winners (
            draw_id INTEGER,
            user_id INTEGER,
            number INTEGER,
            tier INTEGER,
            prize REAL,
            FOREIGN KEY (draw_id) REFERENCES draws(draw_id)
        )""")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS lottery_settings (
            tier INTEGER PRIMARY KEY,
            total_tickets INTEGER DEFAULT 100,
            sold_tickets INTEGER DEFAULT 0,
            prize_pool REAL DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        )""")

        # Initialize ticket tiers
        for tier in [100, 200, 300]:
            cursor.execute(
                "INSERT OR IGNORE INTO lottery_settings (tier) VALUES (?)", 
                (tier,)
            )
        conn.commit()

init_db()

# ====================== BOT IMPLEMENTATION ======================
class LotteryBot:
    def __init__(self):
        self._validate_env()
        self.updater = Updater(BOT_TOKEN, use_context=True)
        self.user_activity = {}
        self._setup_handlers()
        self._init_schedulers()

    def _validate_env(self):
        """Verify required environment variables"""
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable required")
        if not ADMIN_IDS:
            logging.warning("No ADMIN_IDS configured - admin commands disabled")

    def _db_execute(self, query, params=(), fetch_lastrowid=False):
        """Safe database execution with error handling"""
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                if fetch_lastrowid:
                    return cursor.lastrowid
        except sqlite3.Error as e:
            logging.error(f"Database error: {e}")
            raise

    def _db_fetch(self, query, params=()):
        """Safe database fetch with row dicts"""
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def _init_schedulers(self):
        """Initialize scheduled tasks"""
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(self._backup_db, 'interval', hours=6)
        scheduler.add_job(self._clean_expired_reservations, 'interval', hours=1)
        scheduler.start()

    def _backup_db(self):
        """Create timestamped database backup"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            backup_path = f"{BACKUP_DIR}/backup_{timestamp}.db"
            with sqlite3.connect(DB_FILE) as src:
                with sqlite3.connect(backup_path) as dst:
                    src.backup(dst)
            logging.info(f"Database backed up to {backup_path}")
            self._clean_old_backups()
        except Exception as e:
            logging.error(f"Backup failed: {str(e)}")

    def _clean_old_backups(self, keep_last=5):
        """Rotate backup files"""
        try:
            backups = sorted(os.listdir(BACKUP_DIR))
            for old_backup in backups[:-keep_last]:
                os.remove(f"{BACKUP_DIR}/{old_backup}")
        except Exception as e:
            logging.error(f"Backup cleanup failed: {str(e)}")

    def _clean_expired_reservations(self):
        """Remove reservations older than 24 hours"""
        expiry_time = (datetime.now() - timedelta(hours=24)).isoformat()
        self._db_execute(
            "DELETE FROM reserved_numbers WHERE reserved_at < ?",
            (expiry_time,)
        )

    def _check_spam(self, update: Update, context: CallbackContext):
        """Anti-spam protection with 2-second cooldown"""
        user_id = update.effective_user.id
        now = datetime.now().timestamp()
        if user_id in self.user_activity and now - self.user_activity[user_id] < 2:
            update.message.reply_text("⚠️ Please wait before sending another request")
            return True
        self.user_activity[user_id] = now
        return False

    def _check_maintenance(self, update: Update, context: CallbackContext):
        """Check if bot is in maintenance mode"""
        if MAINTENANCE and update.effective_user.id not in ADMIN_IDS:
            update.message.reply_text("🔧 The bot is currently under maintenance. Please try again later.")
            return True
        return False

    def _setup_handlers(self):
        """Configure all bot command handlers"""
        dp = self.updater.dispatcher
        
        # Anti-spam and maintenance checks
        dp.add_handler(TypeHandler(Update, self._check_spam), group=-1)
        dp.add_handler(TypeHandler(Update, self._check_maintenance), group=-1)
        
        # Purchase flow
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('buy', self._start_purchase)],
            states={
                SELECT_TIER: [MessageHandler(Filters.regex('^(100|200|300)$'), self._select_tier)],
                SELECT_NUMBER: [MessageHandler(Filters.regex('^[1-9][0-9]?$|^100$'), self._select_number)],
                PAYMENT_PROOF: [MessageHandler(Filters.photo, self._receive_payment_proof)]
            },
            fallbacks=[CommandHandler('cancel', self._cancel_purchase)]
        )
        dp.add_handler(conv_handler)
        
        # Admin commands
        dp.add_handler(CommandHandler("approve", self._approve_payment))
        dp.add_handler(CommandHandler("pending", self._show_pending_approvals))
        dp.add_handler(CommandHandler("approve_all", self._approve_all_pending))
        dp.add_handler(CommandHandler("draw", self._manual_draw))
        dp.add_handler(CommandHandler("announce_100", lambda u,c: self._announce_winners(u,c,100)))
        dp.add_handler(CommandHandler("announce_200", lambda u,c: self._announce_winners(u,c,200)))
        dp.add_handler(CommandHandler("announce_300", lambda u,c: self._announce_winners(u,c,300)))
        
        # Maintenance mode commands (admin only)
        dp.add_handler(CommandHandler("maintenance_on", self._enable_maintenance))
        dp.add_handler(CommandHandler("maintenance_off", self._disable_maintenance))
        
        # User commands
        dp.add_handler(CommandHandler("numbers", self._available_numbers))
        dp.add_handler(CommandHandler("mytickets", self._show_user_tickets))
        dp.add_handler(CommandHandler("progress", self._show_progress))
        dp.add_handler(CommandHandler("winners", self._show_past_winners))

    # ============= MAINTENANCE MODE HANDLERS =============
    def _enable_maintenance(self, update: Update, context: CallbackContext):
        """Enable maintenance mode (admin only)"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        global MAINTENANCE
        MAINTENANCE = True
        update.message.reply_text("🛠 Maintenance mode ENABLED")

    def _disable_maintenance(self, update: Update, context: CallbackContext):
        """Disable maintenance mode (admin only)"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        global MAINTENANCE
        MAINTENANCE = False
        update.message.reply_text("✅ Maintenance mode DISABLED")

    # ============= PURCHASE FLOW =============
    def _start_purchase(self, update: Update, context: CallbackContext):
        """Start ticket purchase process"""
        self._clean_expired_reservations()
        update.message.reply_text(
            "🎟️ <b>Select Ticket Tier</b>\n\n"
            "1. 100 Birr\n2. 200 Birr\n3. 300 Birr\n\n"
            "Reply with the amount (100, 200, or 300)",
            parse_mode='HTML'
        )
        return SELECT_TIER

    def _select_tier(self, update: Update, context: CallbackContext):
        """Handle tier selection"""
        tier = int(update.message.text)
        context.user_data['tier'] = tier
        available = self._get_available_numbers(tier)
        
        # Create number selection keyboard
        buttons = [
            InlineKeyboardButton(str(num), callback_data=f"num_{num}")
            for num in available[:20]  # Show first 20 numbers
        ]
        
        update.message.reply_text(
            f"🔢 Available Numbers for {tier} Birr:",
            reply_markup=InlineKeyboardMarkup([buttons[i:i+5] for i in range(0, len(buttons), 5)])
        )
        return SELECT_NUMBER

    def _select_number(self, update: Update, context: CallbackContext):
        """Reserve selected number"""
        number = int(update.message.text)
        tier = context.user_data['tier']
        user_id = update.effective_user.id
        
        if not self._is_number_available(number, tier):
            update.message.reply_text("❌ Number already taken! Choose another:")
            return SELECT_NUMBER
        
        # Reserve number
        self._db_execute(
            "INSERT INTO reserved_numbers VALUES (?, ?, ?, ?, NULL)",
            (number, tier, user_id, datetime.now().isoformat())
        )
        context.user_data['number'] = number
        
        update.message.reply_text(
            f"✅ <b>Number #{number} Reserved</b>\n\n"
            f"Send payment of {tier} Birr to:\n"
            "<code>CBE: 1000295626473</code>\n\n"
            "Then upload receipt photo",
            parse_mode='HTML'
        )
        return PAYMENT_PROOF

    def _receive_payment_proof(self, update: Update, context: CallbackContext):
        """Handle payment proof upload"""
        user_id = update.effective_user.id
        photo_id = update.message.photo[-1].file_id
        number = context.user_data['number']
        tier = context.user_data['tier']
        
        # Store payment proof
        self._db_execute(
            "UPDATE reserved_numbers SET photo_id = ? WHERE number = ? AND tier = ?",
            (photo_id, number, tier)
        )
        
        # Notify all admins
        for admin_id in ADMIN_IDS:
            self.updater.bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=f"🔄 Payment Proof\n\nUser: @{update.effective_user.username}\n"
                       f"Number: #{number}\nTier: {tier} Birr\n\n"
                       f"Approve: /approve {number} {tier}"
            )
        
        update.message.reply_text(
            "📨 <b>Payment Received!</b>\n\n"
            "Admin will verify your payment shortly",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    def _approve_payment(self, update: Update, context: CallbackContext):
        """Admin approval of payment"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        try:
            number = int(context.args[0])
            tier = int(context.args[1])
            
            # Get reservation info
            res = self._db_fetch(
                "SELECT user_id FROM reserved_numbers WHERE number = ? AND tier = ?",
                (number, tier)
            )[0]
            
            # Record ticket
            self._db_execute(
                "INSERT INTO tickets (user_id, number, tier) VALUES (?, ?, ?)",
                (res['user_id'], number, tier)
            )
            
            # Update prize pool (50% of ticket price)
            self._db_execute("""
            UPDATE lottery_settings 
            SET sold_tickets = sold_tickets + 1,
                prize_pool = prize_pool + ?
            WHERE tier = ?
            """, (tier * 0.5, tier))
            
            # Check if tier is sold out
            tier_status = self._db_fetch("""
            SELECT total_tickets, sold_tickets 
            FROM lottery_settings 
            WHERE tier = ?
            """, (tier,))[0]
            
            if tier_status['sold_tickets'] >= tier_status['total_tickets']:
                self._conduct_draw(tier)
            
            # Cleanup reservation
            self._db_execute(
                "DELETE FROM reserved_numbers WHERE number = ? AND tier = ?",
                (number, tier)
            )
            
            # Notify user
            self.updater.bot.send_message(
                chat_id=res['user_id'],
                text=f"🎉 <b>Payment Approved!</b>\n\nTicket #{number} confirmed!"
            )
            
            update.message.reply_text(f"✅ Approved ticket #{number}")

        except Exception as e:
            update.message.reply_text("Usage: /approve NUMBER TIER")
            logging.error(f"Approval error: {e}")

    # ============= DRAW SYSTEM =============
    def _conduct_draw(self, tier: int):
        """Automatically conduct draw when tier sells out"""
        # Get all unredeemed tickets for this tier
        tickets = self._db_fetch("""
        SELECT number, user_id FROM tickets 
        WHERE tier = ? AND purchase_time > (
            SELECT MAX(draw_time) FROM draws WHERE tier = ?
        )
        """, (tier, tier))
        
        if not tickets:
            logging.warning(f"No tickets found for tier {tier} draw")
            return
        
        # Random selection
        winner = random.choice(tickets)
        
        # Get current prize pool
        prize = self._db_fetch("""
        SELECT prize_pool FROM lottery_settings WHERE tier = ?
        """, (tier,))[0]['prize_pool']
        
        # Record draw
        draw_id = self._db_execute("""
        INSERT INTO draws (winning_number, tier, status)
        VALUES (?, ?, 'pending')
        RETURNING draw_id
        """, (winner['number'], tier), fetch_lastrowid=True)
        
        # Record winner
        self._db_execute("""
        INSERT INTO winners VALUES (?, ?, ?, ?, ?)
        """, (draw_id, winner['user_id'], winner['number'], tier, prize))
        
        # Reset tier counters
        self._db_execute("""
        UPDATE lottery_settings 
        SET sold_tickets = 0,
            prize_pool = 0
        WHERE tier = ?
        """, (tier,))
        
        # Notify admins
        for admin_id in ADMIN_IDS:
            self.updater.bot.send_message(
                chat_id=admin_id,
                text=f"🎰 Automatic Draw Complete (Tier {tier} Birr)\n\n"
                     f"Winner: User {winner['user_id']}\n"
                     f"Number: #{winner['number']}\n"
                     f"Prize: {prize:.2f} Birr\n\n"
                     f"Use /announce_{tier} to publish"
            )

    def _manual_draw(self, update: Update, context: CallbackContext):
        """Admin-triggered manual draw"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        try:
            tier = int(context.args[0])
            self._conduct_draw(tier)
            update.message.reply_text(f"Manual draw conducted for Tier {tier}")
        except:
            update.message.reply_text("Usage: /draw TIER")

    def _announce_winners(self, update: Update, context: CallbackContext, tier: int):
        """Publish winners to channel"""
        if update.effective_user.id not in ADMIN_IDS:
            return

        winner = self._db_fetch("""
        SELECT w.user_id, w.number, w.prize
        FROM winners w
        JOIN draws d ON w.draw_id = d.draw_id
        WHERE d.tier = ? AND d.status = 'pending'
        ORDER BY d.draw_time DESC LIMIT 1
        """, (tier,))
        
        if not winner:
            update.message.reply_text(f"No pending winners for Tier {tier}")
            return
        
        winner = winner[0]
        message = (
            f"🏆 **Tier {tier} Birr Winner** 🏆\n\n"
            f"🎫 Winning Number: #{winner['number']}\n"
            f"💰 Prize Amount: {winner['prize']:.2f} Birr\n\n"
            f"Contact @admin to claim your prize!"
        )
        
        # Announce to channel
        self.updater.bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            parse_mode='Markdown'
        )
        
        # Mark as announced
        self._db_execute("""
        UPDATE draws SET status = 'announced'
        WHERE tier = ? AND status = 'pending'
        """, (tier,))
        
        update.message.reply_text(f"Tier {tier} results announced!")

    # ============= USER COMMANDS =============
    def _show_progress(self, update: Update, context: CallbackContext):
        """Show ticket sales progress per tier"""
        tiers = self._db_fetch("""
        SELECT tier, total_tickets, sold_tickets, prize_pool
        FROM lottery_settings
        WHERE is_active = TRUE
        """)
        
        message = "📊 Ticket Sales Progress\n\n"
        for tier in tiers:
            remaining = tier['total_tickets'] - tier['sold_tickets']
            message += (
                f"Tier {tier['tier']} Birr:\n"
                f"• Sold: {tier['sold_tickets']}/{tier['total_tickets']}\n"
                f"• Remaining: {remaining}\n"
                f"• Current Prize: {tier['prize_pool']:.2f} Birr\n\n"
            )
        
        update.message.reply_text(message)

    def _show_past_winners(self, update: Update, context: CallbackContext):
        """Display last 5 winners"""
        winners = self._db_fetch("""
        SELECT d.tier, d.winning_number, d.draw_time, w.prize
        FROM winners w
        JOIN draws d ON w.draw_id = d.draw_id
        WHERE d.status = 'announced'
        ORDER BY d.draw_time DESC
        LIMIT 5
        """)
        
        if not winners:
            update.message.reply_text("No past winners yet")
            return
            
        message = "🏆 Past Winners (Last 5)\n\n"
        for winner in winners:
            message += (
                f"Tier {winner['tier']} Birr:\n"
                f"• Number: #{winner['winning_number']}\n"
                f"• Prize: {winner['prize']:.2f} Birr\n"
                f"• Date: {winner['draw_time'][:10]}\n\n"
            )
        
        update.message.reply_text(message)

    def _show_user_tickets(self, update: Update, context: CallbackContext):
        """Show user's purchased tickets"""
        tickets = self._db_fetch(
            "SELECT number, tier, purchase_time FROM tickets WHERE user_id = ?",
            (update.effective_user.id,)
        )
        
        if not tickets:
            update.message.reply_text("You don't have any tickets yet!")
            return
            
        message = "🎫 Your Tickets:\n\n"
        for ticket in tickets:
            message += (
                f"#{ticket['number']} ({ticket['tier']} Birr)\n"
                f"Purchased: {ticket['purchase_time']}\n\n"
            )
        
        update.message.reply_text(message)

    def _show_pending_approvals(self, update: Update, context: CallbackContext):
        """List pending approvals for admins"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        pending = self._db_fetch("""
            SELECT number, tier, user_id, reserved_at 
            FROM reserved_numbers 
            WHERE photo_id IS NOT NULL
            ORDER BY reserved_at
        """)
        
        if not pending:
            update.message.reply_text("No pending approvals")
            return
            
        message = "🔄 Pending Approvals:\n\n"
        for item in pending:
            message += (
                f"#{item['number']} ({item['tier']} Birr)\n"
                f"User: {item['user_id']}\n"
                f"Approve: /approve {item['number']} {item['tier']}\n\n"
            )
        
        update.message.reply_text(message)

    def _approve_all_pending(self, update: Update, context: CallbackContext):
        """Bulk approve all pending payments"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        pending = self._db_fetch("""
            SELECT number, tier, user_id FROM reserved_numbers 
            WHERE photo_id IS NOT NULL
        """)
        
        for item in pending:
            self._db_execute(
                "INSERT INTO tickets (user_id, number, tier) VALUES (?, ?, ?)",
                (item['user_id'], item['number'], item['tier'])
            )
            
            self.updater.bot.send_message(
                chat_id=item['user_id'],
                text=f"🎉 Payment Approved!\n\nTicket #{item['number']} confirmed!"
            )
        
        self._db_execute("DELETE FROM reserved_numbers WHERE photo_id IS NOT NULL")
        update.message.reply_text(f"Approved {len(pending)} tickets")

    def _available_numbers(self, update: Update, context: CallbackContext):
        """Show available numbers for all tiers"""
        tiers = {100: "💵 100 Birr", 200: "💰 200 Birr", 300: "💎 300 Birr"}
        message = "🔢 Available Numbers:\n\n"
        
        for tier, label in tiers.items():
            available = self._get_available_numbers(tier)
            message += f"{label}: {', '.join(map(str, available[:15]))}\n\n"
        
        update.message.reply_text(message)

    def _get_available_numbers(self, tier: int) -> List[int]:
        """Get available numbers for a tier"""
        reserved = {r['number'] for r in self._db_fetch(
            "SELECT number FROM reserved_numbers WHERE tier = ?", (tier,))}
        
        confirmed = {c['number'] for c in self._db_fetch(
            "SELECT number FROM tickets WHERE tier = ?", (tier,))}
        
        return sorted(set(range(1, 101)) - reserved - confirmed)

    def _is_number_available(self, number: int, tier: int) -> bool:
        """Check if number is available"""
        return number in self._get_available_numbers(tier)

    def _cancel_purchase(self, update: Update, context: CallbackContext):
        """Cancel current purchase"""
        if 'number' in context.user_data and 'tier' in context.user_data:
            self._db_execute(
                "DELETE FROM reserved_numbers WHERE number = ? AND tier = ?",
                (context.user_data['number'], context.user_data['tier'])
            )
        
        update.message.reply_text("❌ Purchase cancelled")
        return ConversationHandler.END

    def run(self):
        """Start the application"""
        app = Flask(__name__)
        
        @app.route('/health')
        def health():
            return "MAINTENANCE" if MAINTENANCE else "OK", (503 if MAINTENANCE else 200)
        
        Thread(target=app.run, kwargs={'host':'0.0.0.0','port':5000}).start()
        logging.info("Starting lottery bot...")
        self.updater.start_polling()
        self.updater.idle()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    try:
        bot = LotteryBot()
        bot.run()
    except Exception as e:
        logging.critical(f"Failed to start bot: {str(e)}")
        raise
