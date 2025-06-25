# 🎰 Telegram Lottery Bot

Automated ticket sales with tier-based lottery draws (100/200/300 Birr)

## 🌟 Key Features
- **Automatic Draws** - Triggers when tickets sell out per tier
- **Real-Time Tracking** - `/progress` shows current sales
- **Winner Management** - Automatic announcements
- **Admin Dashboard** - Full control over draws

## 🛠️ Prerequisites
- Python 3.8+
- [Render](https://render.com) account
- Telegram bot token from [@BotFather](https://t.me/BotFather)

## 🚀 Quick Deployment

### 1. Local Setup
```bash
git clone https://github.com/yourrepo/lottery-bot.git
cd lottery-bot
cp .env.example .env  # Update with your credentials
python -c "from main import init_db; init_db()"
