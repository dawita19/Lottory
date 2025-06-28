# web.py
import os
import logging
from flask import Flask, jsonify
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import func
from datetime import datetime
import pytz

# --- Configure Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration & Environment Variables ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

# --- Database Engine ---
engine = create_engine(DATABASE_URL)

# --- Flask Web Application ---
run = Flask(__name__)

@run.route('/')
def home():
    """A simple home page endpoint for the Flask application."""
    logger.info("Accessing / (home) endpoint.")
    return "Lottery Bot Web Service is running. Check /health for detailed status."

@run.route('/health')
def health_check():
    """
    Health check endpoint for the Flask application.
    Checks database connectivity and bot maintenance mode.
    """
    try:
        with engine.connect() as connection:
            connection.execute(func.now())
        
        status = "MAINTENANCE" if MAINTENANCE else "OK"
        status_code = 503 if MAINTENANCE else 200
        
        logger.info(f"Health check successful. Status: {status}, DB: connected.")
        return jsonify(
            status=status,
            database="connected",
            maintenance_mode=MAINTENANCE,
            timestamp=datetime.now(pytz.utc).isoformat()
        ), status_code
    except Exception as e:
        logger.error(f"Health check database connection failed: {e}")
        return jsonify(
            status="ERROR",
            database="disconnected",
            maintenance_mode=MAINTENANCE,
            error=str(e),
            timestamp=datetime.now(pytz.utc).isoformat()
        ), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask web service locally on http://0.0.0.0:{port}")
    run.run(host='0.0.0.0', port=port, debug=False)
