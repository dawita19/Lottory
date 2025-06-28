🎰 Telegram Lottery BotAutomated ticket sales with tier-based lottery draws (100/200/300 Birr)🌟 Key FeaturesAutomatic Draws - Triggers when tickets sell out per tierReal-Time Tracking - /progress shows current salesWinner Management - Automatic announcementsAdmin Dashboard - Full control over draws🛠️ PrerequisitesPython 3.8+Render accountTelegram bot token from @BotFather🚀 Quick Deployment1. Local Setupgit clone https://github.com/yourrepo/lottery-bot.git
cd lottery-bot
cp .env.example .env # Update with your credentials
python -c "from main import init_db; init_db()"
