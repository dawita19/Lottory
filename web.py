# web.py
import os
import logging
from flask import Flask, jsonify
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

# --- Configure Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration & Environment Variables ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

# --- Database Engine (no models needed here, just connection test) ---
engine = create_engine(DATABASE_URL)

# --- Flask Web Application ---
run = Flask(__name__) # Named 'run' to match Gunicorn command

@run.route('/')
def home():
    """A simple home page for the Flask application."""
    return "Lottery Bot Web Service is running. Check /health for status."

@run.route('/health')
def health_check():
    """
    Health check endpoint for the Flask application.
    Checks database connectivity and bot maintenance mode.
    """
    try:
        # Perform a simple query to check database connection
        with engine.connect() as connection:
            # Use func.now() for a cross-database compatible test query
            connection.execute("SELECT 1")
        
        status = "MAINTENANCE" if MAINTENANCE else "OK"
        status_code = 503 if MAINTENANCE else 200
        
        return jsonify(
            status=status,
            database="connected",
            maintenance_mode=MAINTENANCE
        ), status_code
    except Exception as e:
        logger.error(f"Health check database error: {e}")
        return jsonify(
            status="ERROR",
            database="disconnected",
            error=str(e)
        ), 500

# This `if __name__ == "__main__":` block is typically for local development
# Gunicorn will import `run` directly.
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    run.run(host='0.0.0.0', port=port)
