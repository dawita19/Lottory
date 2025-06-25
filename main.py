import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackContext,
    MessageHandler, Filters, ConversationHandler, TypeHandler
)

# ====================== CONFIGURATION ======================
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS").split(",")]
CHANNEL_ID = os.getenv("CHANNEL_ID")
DB_FILE = "/data/lottery_bot.db"  # Persistent storage path for Render

# Conversation states
SELECT_TIER, SELECT_NUMBER, PAYMENT_PROOF = range(3)

# ====================== DATABASE SETUP ======================
def init_db():
    os.makedirs("/data", exist_ok=True)  # Ensure data directory exists
    
    conn = sqlite3.connect(DB_FILE)
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
        purchase_time TEXT DEFAULT CURRENT_TIMESTAMP)
    """)
    
    # Add indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reserved ON reserved_numbers(number, tier)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets ON tickets(number, tier)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tickets ON tickets(user_id)")
    
    conn.commit()
    conn.close()

init_db()

# ====================== BOT IMPLEMENTATION ======================
class LotteryBot:
    def __init__(self):
        self.updater = Updater(BOT_TOKEN, use_context=True)
        self._setup_handlers()
        self.user_activity = {}

    def _db_execute(self, query, params=()):
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()

    def _db_fetch(self, query, params=()):
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def _clean_expired_reservations(self):
        """Remove reservations older than 24 hours"""
        expiry_time = (datetime.now() - timedelta(hours=24)).isoformat()
        self._db_execute(
            "DELETE FROM reserved_numbers WHERE reserved_at < ?",
            (expiry_time,)
        )

    def _check_spam(self, update: Update, context: CallbackContext):
        """Anti-spam protection"""
        user_id = update.effective_user.id
        now = datetime.now().timestamp()
        
        if user_id in self.user_activity:
            if now - self.user_activity[user_id] < 2:  # 2 seconds cooldown
                update.message.reply_text("⚠️ Please wait before sending another request")
                return True
                
        self.user_activity[user_id] = now
        return False

    def _setup_handlers(self):
        dp = self.updater.dispatcher
        
        # Anti-spam handler
        dp.add_handler(TypeHandler(Update, self._check_spam), group=-1)
        
        # Ticket purchase flow
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
        dp.add_handler(CommandHandler("approve", self._approve_payment))
        dp.add_handler(CommandHandler("pending", self._show_pending_approvals))
        dp.add_handler(CommandHandler("approve_all", self._approve_all_pending))
        dp.add_handler(CommandHandler("numbers", self._available_numbers))
        dp.add_handler(CommandHandler("mytickets", self._show_user_tickets))

    # ============= PURCHASE FLOW =============
    def _start_purchase(self, update: Update, context: CallbackContext):
        """Start ticket purchase process"""
        self._clean_expired_reservations()
        update.message.reply_text(
            "🎟️ <b>Select Ticket Tier</b>\n\n"
            "1. 100 Birr\n"
            "2. 200 Birr\n"
            "3. 300 Birr\n\n"
            "Reply with the amount (100, 200, or 300)",
            parse_mode='HTML'
        )
        return SELECT_TIER

    def _select_tier(self, update: Update, context: CallbackContext):
        """Store selected tier and request number"""
        tier = int(update.message.text)
        context.user_data['tier'] = tier
        
        available = self._get_available_numbers(tier)
        buttons = [
            InlineKeyboardButton(str(num), callback_data=f"num_{num}")
            for num in available[:20]  # Show first 20 for space
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
        
        self._db_execute(
            "INSERT INTO reserved_numbers VALUES (?, ?, ?, ?, NULL)",
            (number, tier, user_id, datetime.now().isoformat())
        )
        
        context.user_data['number'] = number
        
        update.message.reply_text(
            f"✅ <b>Number #{number} Reserved</b>\n\n"
            f"Send payment of {tier} Birr to:\n"
            "<code>CBE: 1000XXXXXX</code>\n\n"
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
        
        self._db_execute(
            "UPDATE reserved_numbers SET photo_id = ? WHERE number = ? AND tier = ?",
            (photo_id, number, tier)
        )
        
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

    # ============= ADMIN COMMANDS =============
    def _approve_payment(self, update: Update, context: CallbackContext):
        """Admin approval of payment"""
        if update.effective_user.id not in ADMIN_IDS:
            return
            
        try:
            number = int(context.args[0])
            tier = int(context.args[1])
            
            res = self._db_fetch(
                "SELECT user_id, photo_id FROM reserved_numbers WHERE number = ? AND tier = ?",
                (number, tier)
            )[0]
            
            self._db_execute(
                "INSERT INTO tickets (user_id, number, tier) VALUES (?, ?, ?)",
                (res['user_id'], number, tier)
            )
            
            self._db_execute(
                "DELETE FROM reserved_numbers WHERE number = ? AND tier = ?",
                (number, tier)
            )
            
            self.updater.bot.send_message(
                chat_id=res['user_id'],
                text=f"🎉 <b>Payment Approved!</b>\n\nTicket #{number} confirmed!"
            )
            
            update.message.reply_text(f"✅ Approved ticket #{number}")

        except Exception as e:
            update.message.reply_text("Usage: /approve NUMBER TIER")
            logging.error(f"Approval error: {e}")

    def _show_pending_approvals(self, update: Update, context: CallbackContext):
        """List all pending approvals"""
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
        """Approve all pending payments"""
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

    # ============= USER COMMANDS =============
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

    def _available_numbers(self, update: Update, context: CallbackContext):
        """Show available numbers for all tiers"""
        tiers = {100: "💵 100 Birr", 200: "💰 200 Birr", 300: "💎 300 Birr"}
        message = "🔢 Available Numbers:\n\n"
        
        for tier, label in tiers.items():
            available = self._get_available_numbers(tier)
            message += f"{label}: {', '.join(map(str, available[:15]))}\n\n"
        
        update.message.reply_text(message)

    # ============= HELPER METHODS =============
    def _get_available_numbers(self, tier: int) -> List[int]:
        """Get available numbers for a tier"""
        reserved = {r['number'] for r in self._db_fetch(
            "SELECT number FROM reserved_numbers WHERE tier = ?", (tier,))}
        
        confirmed = {c['number'] for c in self._db_fetch(
            "SELECT number FROM tickets WHERE tier = ?", (tier,))}
        
        return sorted(set(range(1, 101)) - reserved - confirmed)

    def _is_number_available(self, number: int, tier: int) -> bool:
        """Check number availability"""
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
        """Start the bot"""
        logging.info("Starting lottery bot...")
        self.updater.start_polling()
        self.updater.idle()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    bot = LotteryBot()
    bot.run()
