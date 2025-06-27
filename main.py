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
            # In Render, if you're using a managed PostgreSQL, it handles backups.
            # If you're using SQLite, ensure the path is writable or use a persistent disk.
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
            # These handlers must be at the highest group (lowest number) to run first.
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
                # Display only a subset if many are available to keep message concise
                display_numbers = available[:15]
                remaining_count = len(available) - len(display_numbers)
                
                message += f"{label}: {', '.join(map(str, display_numbers))}"
                if remaining_count > 0:
                    message += f" (and {remaining_count} more)"
                message += "\n\n"
            
            await update.message.reply_text(message)

        # ============= PURCHASE FLOW =============
        async def _start_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            """Start ticket purchase process"""
            if MAINTENANCE:
                await update.message.reply_text("🚧 Bot is under maintenance. Please try again later.")
                return ConversationHandler.END
                
            clean_expired_reservations()
            
            # Prepare inline keyboard for tier selection
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

        async def _select_tier(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            """Handle tier selection from inline keyboard"""
            query = update.callback_query
            await query.answer() # Acknowledge the button press

            tier = int(query.data.split('_')[1]) # Extract tier from callback_data
            context.user_data['tier'] = tier
            available = self._get_available_numbers(tier)
            
            if not available:
                await query.edit_message_text(f"❌ No numbers available for {tier} Birr tier. Please choose another tier or try later.")
                return ConversationHandler.END # End conversation if no numbers
                
            # Create number selection keyboard
            buttons = [
                InlineKeyboardButton(str(num), callback_data=f"num_{num}")
                for num in available[:20]  # Show first 20 numbers, or fewer if not 20 available
            ]
            # Arrange buttons in rows of 5 for better display
            keyboard = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"🔢 Available Numbers for {tier} Birr:\n\n"
                "Select your preferred number:",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return SELECT_NUMBER # Transition to next state

        async def _select_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            """Reserve selected number from inline keyboard"""
            query = update.callback_query
            await query.answer() # Acknowledge the button press

            number = int(query.data.split('_')[1]) # Extract number from callback_data
            tier = context.user_data['tier']
            user_id = query.from_user.id
            
            if not self._is_number_available(number, tier):
                await query.edit_message_text("❌ Number already taken or no longer available! Please choose another number or tier.")
                # Optionally, refresh the available numbers list or end conversation
                return SELECT_NUMBER # Stay in the same state to allow re-selection
            
            # Reserve number
            with Session() as session:
                user = session.query(User).filter_by(telegram_id=user_id).first()
                if not user:
                    await query.edit_message_text("❌ User not found. Please /start again.")
                    return ConversationHandler.END
                    
                # Check for existing reservation by this user for this tier (to prevent multiple reservations)
                existing_reservation = session.query(ReservedNumber).filter_by(
                    user_id=user.id,
                    tier=tier
                ).first()
                if existing_reservation:
                    # If they have an existing reservation for this tier, update it
                    existing_reservation.number = number
                    existing_reservation.reserved_at = datetime.utcnow()
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
            
            context.user_data['number'] = number
            
            await query.edit_message_text(
                f"✅ <b>Number #{number} Reserved for {tier} Birr</b>\n\n"
                f"Send payment of {tier} Birr to:\n"
                "<code>CBE: 1000295626473</code>\n\n"
                "Then upload your payment receipt photo to this chat.",
                parse_mode='HTML'
            )
            return PAYMENT_PROOF

        async def _receive_payment_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            """Handle payment proof upload"""
            user_id = update.effective_user.id
            photo_id = update.message.photo[-1].file_id
            number = context.user_data.get('number')
            tier = context.user_data.get('tier')
            
            if not number or not tier:
                await update.message.reply_text("❌ An error occurred with your reservation details. Please start the purchase process again with /buy.")
                return ConversationHandler.END

            # Store payment proof
            with Session() as session:
                reservation = session.query(ReservedNumber).filter_by(
                    user_id=session.query(User).filter_by(telegram_id=user_id).first().id,
                    number=number, # Filter by chosen number
                    tier=tier
                ).first()
                
                if not reservation:
                    await update.message.reply_text("❌ Your reservation expired or was not found. Please start over with /buy.")
                    return ConversationHandler.END
                    
                reservation.photo_id = photo_id
                session.commit()
                
                # Notify all admins
                for admin_id in ADMIN_IDS:
                    try:
                        await self.application.bot.send_photo(
                            chat_id=admin_id,
                            photo=photo_id,
                            caption=(f"🔄 Payment Proof\n\nUser: @{update.effective_user.username or user_id}\n"
                                       f"Number: #{number}\nTier: {tier} Birr\n\n"
                                       f"Approve: /approve {number} {tier}")
                        )
                    except Exception as e:
                        logging.error(f"Failed to send payment proof to admin {admin_id}: {e}")
            
            await update.message.reply_text(
                "📨 <b>Payment Received!</b>\n\n"
                "Your payment proof has been submitted. An admin will verify your payment shortly.",
                parse_mode='HTML'
            )
            return ConversationHandler.END

        async def _approve_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Admin approval of payment"""
            if update.effective_user.id not in ADMIN_IDS:
                return
                
            try:
                if len(context.args) < 2:
                    await update.message.reply_text("Usage: /approve NUMBER TIER")
                    return
                    
                number = int(context.args[0])
                tier = int(context.args[1])
                
                with Session() as session:
                    # Get reservation info
                    # Find by number and tier. The user_id is on the reservation, not directly passed here
                    reservation = session.query(ReservedNumber).filter_by(
                        number=number,
                        tier=tier
                    ).first()
                    
                    if not reservation:
                        await update.message.reply_text(f"❌ No pending reservation found for number #{number} tier {tier}.")
                        return
                    
                    # Check if ticket already exists (double check)
                    existing_ticket = session.query(Ticket).filter_by(
                        user_id=reservation.user_id,
                        number=number,
                        tier=tier,
                        is_approved=True
                    ).first()

                    if existing_ticket:
                        await update.message.reply_text(f"⚠️ Ticket #{number} (Tier {tier}) for user {reservation.user_id} is already approved.")
                        session.delete(reservation) # Clean up the reservation if it somehow persists
                        session.commit()
                        return

                    # Record ticket
                    ticket = Ticket(
                        user_id=reservation.user_id,
                        number=number,
                        tier=tier,
                        purchased_at=reservation.reserved_at, # Use reservation time for consistency
                        is_approved=True
                    )
                    session.add(ticket)
                    
                    # Update prize pool (50% of ticket price) and sold tickets count
                    settings = session.query(LotterySettings).filter_by(tier=tier).first()
                    if settings: # Ensure settings exist
                        settings.sold_tickets += 1
                        settings.prize_pool += tier * 0.5
                    else:
                        logging.warning(f"LotterySettings for tier {tier} not found. Prize pool not updated.")
                    
                    # Check if tier is sold out and conduct draw
                    if settings and settings.sold_tickets >= settings.total_tickets:
                        await self._conduct_draw(session, tier)
                    
                    # Cleanup reservation
                    session.delete(reservation)
                    session.commit()
                    
                    # Notify user
                    user = session.query(User).get(ticket.user_id)
                    if user:
                        await self.application.bot.send_message(
                            chat_id=user.telegram_id,
                            text=f"🎉 <b>Payment Approved!</b>\n\nYour ticket <b>#{number}</b> for {tier} Birr is now confirmed and entered into the draw! Good luck!",
                            parse_mode='HTML'
                        )
                    else:
                        logging.error(f"User {ticket.user_id} not found to notify after approval.")
                    
                    await update.message.reply_text(f"✅ Approved ticket #{number} (Tier {tier}).")

            except Exception as e:
                await update.message.reply_text(f"❌ Error approving payment: {e}. Usage: /approve NUMBER TIER")
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
                    username = f"@{user.username}" if user.username else f"user ID: {user.telegram_id}"
                    
                    message += (
                        f"Ticket: #{item.number} ({item.tier} Birr)\n"
                        f"User: {username}\n"
                        f"Reserved At: {item.reserved_at.strftime('%Y-%m-%d %H:%M')}\n"
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
                    try:
                        # Check for existing ticket to prevent duplicates
                        existing_ticket = session.query(Ticket).filter_by(
                            user_id=item.user_id,
                            number=item.number,
                            tier=item.tier,
                            is_approved=True
                        ).first()

                        if existing_ticket:
                            logging.info(f"Skipping approval for ticket #{item.number} (Tier {item.tier}) for user {item.user_id} - already approved.")
                            session.delete(item) # Clean up the reservation
                            continue # Move to the next item

                        # Record ticket
                        ticket = Ticket(
                            user_id=item.user_id,
                            number=item.number,
                            tier=item.tier,
                            purchased_at=item.reserved_at, # Use reservation time
                            is_approved=True
                        )
                        session.add(ticket)
                        
                        # Update prize pool (50% of ticket price)
                        settings = session.query(LotterySettings).filter_by(tier=item.tier).first()
                        if settings:
                            settings.sold_tickets += 1
                            settings.prize_pool += item.tier * 0.5
                        
                        # Check if tier is sold out
                        if settings and settings.sold_tickets >= settings.total_tickets:
                            await self._conduct_draw(session, item.tier)
                        
                        # Notify user
                        user = session.query(User).get(item.user_id)
                        if user:
                            await self.application.bot.send_message(
                                chat_id=user.telegram_id,
                                text=f"🎉 Payment Approved!\n\nYour ticket #{item.number} for {item.tier} Birr is now confirmed!"
                            )
                        
                        session.delete(item) # Delete reservation after processing
                        count += 1
                    except Exception as e:
                        logging.error(f"Error processing pending approval for ticket {item.number} (Tier {item.tier}): {e}")
                        # Don't re-raise, try to process other items
                
                session.commit() # Commit all changes at once
                
                await update.message.reply_text(f"Approved {count} tickets.")

        # ============= DRAW SYSTEM =============
        async def _conduct_draw(self, session, tier: int):
            """Automatically conduct draw when tier sells out"""
            # Get all approved tickets for this tier since the last draw
            # Ensures that tickets already included in a past draw are not re-drawn
            last_draw = session.query(LotteryDraw).filter_by(tier=tier, status='announced').order_by(LotteryDraw.drawn_at.desc()).first()
            
            query = session.query(Ticket).filter_by(tier=tier, is_approved=True)
            if last_draw:
                query = query.filter(Ticket.purchased_at > last_draw.drawn_at)
                
            tickets = query.all()
            
            if not tickets:
                logging.warning(f"No *new* approved tickets found for tier {tier} draw since last announcement.")
                # If no new tickets, prevent drawing and reset sold_tickets/prize_pool if they are already maxed
                settings = session.query(LotterySettings).filter_by(tier=tier).first()
                if settings and settings.sold_tickets >= settings.total_tickets:
                     # Reset sold_tickets and prize_pool if full but no draw happened
                    settings.sold_tickets = 0
                    settings.prize_pool = 0
                    session.commit()
                    await self.application.bot.send_message(
                        chat_id=ADMIN_IDS[0], # Send to first admin for notification
                        text=f"⚠️ Tier {tier} sold out, but no new unique tickets found for draw. Counters reset."
                    )
                return
            
            # Random selection of the winning ticket
            winner_ticket = random.choice(tickets)
            
            # Get current prize pool
            settings = session.query(LotterySettings).filter_by(tier=tier).first()
            prize = settings.prize_pool if settings else 0 # Default to 0 if settings not found
            
            # Record draw
            draw = LotteryDraw(
                winning_number=winner_ticket.number,
                tier=tier,
                status='pending' # Set to pending, to be announced manually by admin
            )
            session.add(draw)
            session.flush()  # To get the draw ID before commit
            
            # Record winner
            winner_entry = Winner(
                draw_id=draw.id,
                user_id=winner_ticket.user_id,
                number=winner_ticket.number,
                tier=tier,
                prize=prize
            )
            session.add(winner_entry)
            
            # Reset tier counters immediately after draw decision
            if settings:
                settings.sold_tickets = 0
                settings.prize_pool = 0
            
            session.commit()
            
            # Notify admins about the draw, prompt for announcement
            for admin_id in ADMIN_IDS:
                try:
                    await self.application.bot.send_message(
                        chat_id=admin_id,
                        text=(f"🎰 Automatic Draw Complete (Tier {tier} Birr)\n\n"
                                    f"Winning Number: #{winner_ticket.number}\n"
                                    f"Winner User ID: {winner_ticket.user_id}\n"
                                    f"Prize: {prize:.2f} Birr\n\n"
                                    f"<b>Please use /announce_{tier} to publish this winner to the channel.</b>"),
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id} about draw: {e}")

        async def _manual_draw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Admin-triggered manual draw for a specific tier."""
            if update.effective_user.id not in ADMIN_IDS:
                return
                
            try:
                if not context.args or not context.args[0].isdigit():
                    await update.message.reply_text("Usage: /draw <TIER> (e.g., /draw 100)")
                    return

                tier = int(context.args[0])
                if tier not in [100, 200, 300]:
                    await update.message.reply_text("Invalid tier. Please use 100, 200, or 300.")
                    return

                await update.message.reply_text(f"Attempting to conduct manual draw for Tier {tier}...")
                with Session() as session:
                    await self._conduct_draw(session, tier)
                await update.message.reply_text(f"Manual draw process initiated for Tier {tier}. Check admin notifications for results and announcement command.")
            except Exception as e:
                await update.message.reply_text(f"Error during manual draw: {e}")
                logging.error(f"Manual draw error: {e}")

        async def _announce_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE, tier: int):
            """Publish winners to channel"""
            if update.effective_user.id not in ADMIN_IDS:
                return

            with Session() as session:
                # Find the most recent draw for this tier that is still 'pending'
                winner_entry = session.query(Winner).join(LotteryDraw).filter(
                    LotteryDraw.tier == tier,
                    LotteryDraw.status == 'pending'
                ).order_by(LotteryDraw.drawn_at.desc()).first()
                
                if not winner_entry:
                    await update.message.reply_text(f"No pending winners to announce for Tier {tier}.")
                    # Optionally, show the last announced winner if no pending
                    last_announced_winner = session.query(Winner).join(LotteryDraw).filter(
                        LotteryDraw.tier == tier,
                        LotteryDraw.status == 'announced'
                    ).order_by(LotteryDraw.drawn_at.desc()).first()
                    if last_announced_winner:
                        await update.message.reply_text(f"Last announced winner for Tier {tier} was #{last_announced_winner.number}.")
                    return
                
                # Mark the draw as 'announced'
                winner_entry.draw.status = 'announced'
                session.commit()
                
                # Get user info for the winner
                user = session.query(User).get(winner_entry.user_id)
                username = f"@{user.username}" if user and user.username else f"User ID: {winner_entry.user_id}"
                
                message = (
                    f"🏆 **Tier {tier} Birr Winner** 🏆\n\n"
                    f"🎫 Winning Number: `{winner_entry.number}`\n" # Use backticks for monospace
                    f"💰 Prize Amount: `{winner_entry.prize:.2f} Birr`\n"
                    f"👤 Winner: {username}\n\n"
                    f"Contact @YourAdminHandle (replace with actual admin handle) to claim your prize!"
                )
                
                # Announce to channel
                if CHANNEL_ID:
                    try:
                        await self.application.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=message,
                            parse_mode='Markdown' # Use Markdown for bold and monospace
                        )
                    except Exception as e:
                        logging.error(f"Failed to announce winner to channel {CHANNEL_ID}: {e}")
                        await update.message.reply_text(f"❌ Failed to announce to channel: {e}")
                else:
                    await update.message.reply_text("⚠️ CHANNEL_ID not configured. Winner not announced to channel.")
                
                await update.message.reply_text(f"✅ Tier {tier} results announced successfully!")

        # ============= USER COMMANDS =============
        async def _show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Show ticket sales progress per tier"""
            with Session() as session:
                tiers = session.query(LotterySettings).filter_by(is_active=True).all()
                
                message = "📊 Ticket Sales Progress\n\n"
                if not tiers:
                    message += "No active lottery tiers found."
                else:
                    for tier in tiers:
                        remaining = tier.total_tickets - tier.sold_tickets
                        message += (
                            f"<b>Tier {tier.tier} Birr:</b>\n"
                            f"• Sold: {tier.sold_tickets}/{tier.total_tickets}\n"
                            f"• Remaining: {remaining}\n"
                            f"• Current Prize Pool: {tier.prize_pool:.2f} Birr\n\n"
                        )
                
                await update.message.reply_text(message, parse_mode='HTML')

        async def _show_past_winners(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Display last 5 announced winners across all tiers"""
            with Session() as session:
                winners = session.query(Winner, LotteryDraw).join(LotteryDraw).filter(
                    LotteryDraw.status == 'announced'
                ).order_by(LotteryDraw.drawn_at.desc()).limit(5).all()
                
                if not winners:
                    await update.message.reply_text("No past winners yet. Be the first!")
                    return
                    
                message = "🏆 Past Winners (Last 5)\n\n"
                for winner_entry, draw in winners:
                    user = session.query(User).get(winner_entry.user_id)
                    username = f"@{user.username}" if user and user.username else f"User ID: {winner_entry.user_id}"
                    
                    message += (
                        f"<b>Tier {winner_entry.tier} Birr:</b>\n"
                        f"• Winning Number: #{winner_entry.number}\n"
                        f"• Winner: {username}\n"
                        f"• Prize: {winner_entry.prize:.2f} Birr\n"
                        f"• Draw Date: {draw.drawn_at.strftime('%Y-%m-%d')}\n\n"
                    )
                
                await update.message.reply_text(message, parse_mode='HTML')

        async def _show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Show user's purchased tickets"""
            with Session() as session:
                user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
                if not user:
                    await update.message.reply_text("You need to /start first to register your account!")
                    return
                    
                tickets = session.query(Ticket).filter_by(user_id=user.id).order_by(Ticket.purchased_at.desc()).all()
                
                if not tickets:
                    await update.message.reply_text("You don't have any tickets yet! Use /buy to get one.")
                    return
                    
                message = "🎫 <b>Your Tickets:</b>\n\n"
                for ticket in tickets:
                    status_text = "Approved ✅" if ticket.is_approved else "Pending Verification ⏳"
                    message += (
                        f"• Ticket #{ticket.number} ({ticket.tier} Birr)\n"
                        f"  Purchased: {ticket.purchased_at.strftime('%Y-%m-%d %H:%M')}\n"
                        f"  Status: {status_text}\n\n"
                    )
                
                await update.message.reply_text(message, parse_mode='HTML')

        async def _cancel_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            """Cancel current purchase conversation and delete reservation if exists."""
            if 'number' in context.user_data and 'tier' in context.user_data and update.effective_user:
                user_telegram_id = update.effective_user.id
                number_reserved = context.user_data['number']
                tier_reserved = context.user_data['tier']
                
                with Session() as session:
                    user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
                    if user:
                        # Find and delete the specific reservation for this user, number, and tier
                        reservation_to_delete = session.query(ReservedNumber).filter_by(
                            user_id=user.id,
                            number=number_reserved,
                            tier=tier_reserved
                        ).first()
                        if reservation_to_delete:
                            session.delete(reservation_to_delete)
                            session.commit()
                            logging.info(f"Reservation for user {user_telegram_id}, number {number_reserved}, tier {tier_reserved} cancelled.")
                        else:
                            logging.info(f"No active reservation found for user {user_telegram_id} during cancellation (possibly expired or already processed).")
            
            await update.message.reply_text("❌ Purchase cancelled. You can start a new purchase with /buy.")
            # Clear user_data for the conversation to ensure a clean slate
            context.user_data.clear() 
            return ConversationHandler.END

        async def run_polling_bot(self):
            """Starts the bot's polling mechanism."""
            logging.info("Starting Telegram bot polling...")
            await self.application.run_polling(drop_pending_updates=True)


    # --- Global instance of the bot for internal use (e.g., scheduled tasks) ---
    # This will be initialized only once when the `run` function is called by Gunicorn
    telegram_bot_instance: Optional[LotteryBot] = None

    # --- Main Application Start Point for Gunicorn ---
    # This function will be called by Gunicorn to start the WSGI application (Flask)
    # and also launch the Telegram bot in a background thread.
    def run(environ, start_response): # This function takes the standard WSGI arguments
        """
        Initializes the database, starts the Telegram bot in a background thread,
        and returns the Flask application for Gunicorn to serve.
        """
        # Configure logging for the Gunicorn process
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )
        
        logging.info("Starting Lottery Bot application...")

        # Initialize database
        try:
            init_db()
        except Exception as e:
            logging.critical(f"Failed to initialize database during startup: {e}")
            # In a production environment, if the database is critical,
            # you might want to raise here or exit. For now, we log and
            # allow the Flask app to start (it will report DB disconnected).
            pass 
        
        # Start Telegram bot in a separate thread
        # Gunicorn is synchronous, so we need a dedicated thread for the asyncio-based bot polling.
        global telegram_bot_instance
        if telegram_bot_instance is None: # Ensure bot is only initialized once per Gunicorn worker
            telegram_bot_instance = LotteryBot()

            def start_bot_async_loop():
                # Create a new event loop for this thread, as the main thread (Gunicorn's)
                # will have its own loop or none that we can use directly for asyncio.
                bot_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(bot_loop)
                bot_loop.run_until_complete(telegram_bot_instance.run_polling_bot())

            bot_thread = Thread(target=start_bot_async_loop, daemon=True) # daemon=True allows thread to exit with main app
            bot_thread.start()
            
            logging.info("Telegram bot background thread started.")
        else:
            logging.info("Telegram bot already initialized for this worker.")
        
        # Gunicorn expects a WSGI callable, which is Flask's `app` instance.
        # We pass the arguments received by `run` directly to Flask's `app`.
        return app(environ, start_response)


    # --- Local Development/Testing Entry Point ---
    # This block is only executed when you run the script directly (e.g., `python main.py`).
    # It provides a way to run both Flask and the bot locally without Gunicorn.
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
        # This simulates Gunicorn binding to the Flask app.
        flask_thread = Thread(target=lambda: app.run(host='0.0.0.0', port=5000))
        flask_thread.start()
        logging.info("Flask health check running on port 5000 (local dev mode)")
        
        # Start bot in a separate thread for local development
        local_bot_instance = LotteryBot()
        async def start_local_bot_async():
            await local_bot_instance.run_polling_bot()
        
        bot_thread = Thread(target=lambda: asyncio.run(start_local_bot_async()))
        bot_thread.start()
        logging.info("Telegram bot polling started in background (local dev mode)")
        
        # Keep the main thread alive for Flask in local dev.
        # The bot thread will also run in the background.
        flask_thread.join()
        bot_thread.join() # This line will make main thread wait for bot, which is often not desired in prod.
                          # For local dev it's okay, but in prod Gunicorn manages process lifecycle.
    
