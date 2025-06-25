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
