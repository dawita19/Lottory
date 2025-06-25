# Lottery Bot Deployment

## Requirements
- Python 3.8+
- Render account
- Telegram bot token

## Setup
1. Clone repository
2. Create `.env` file from `.env.example`
3. Set environment variables:
   ```bash
   echo "BOT_TOKEN=your_token" > .env
   echo "ADMIN_IDS=123456789" >> .env
   ```

## Deployment
1. Connect GitHub repo to Render
2. Set environment variables in Render dashboard
3. Deploy!

## Commands
| Command | Description |
|---------|-------------|
| `/buy` | Start ticket purchase |
| `/progress` | Check ticket sales |
| `/winners` | View past winners |
| `/draw` (admin) | Manual draw |
# 🎰 Telegram Lottery Bot

Automated ticket sales and tier-based lottery draws

## 🚀 Deployment

### Prerequisites
- Python 3.8+
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Render account

### Setup
1. Clone repo:
   ```bash
   git clone https://github.com/yourrepo/lottery-bot.git
   cd lottery-bot
   ```

2. Configure environment:
   ```bash
   cp .env.example .env
   nano .env  # Fill in your credentials
   ```

3. Initialize database:
   ```bash
   python -c "from main import init_db; init_db()"
   ```

### Render Deployment
1. Connect your GitHub repository
2. Set environment variables in dashboard
3. Deploy!

## 🎯 Features
- Automatic draws per ticket tier (100/200/300)
- Real-time sales tracking (`/progress`)
- Winner announcements
- Admin dashboard

## 🔐 Admin Commands
| Command | Description |
|---------|-------------|
| `/approve` | Confirm payments |
| `/draw` | Manual draw trigger |
| `/announce_100` | Publish 100-tier results |
