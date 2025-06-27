# web.py
import os
import logging
from flask import Flask, jsonify
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import func # IMPORTANT: Import func for cross-database health check
from datetime import datetime # IMPORTANT: Import datetime for timestamp in health check
import pytz # IMPORTANT: Import pytz for timezone-aware timestamps

# --- Configure Logging ---
# Sets up basic logging for the web service.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__) # Use a named logger for better log tracing

# --- Configuration & Environment Variables ---
# Retrieves the database URL from environment variables, defaulting to a local SQLite file.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lottery_bot.db")
# Retrieves the maintenance mode status. This flag is primarily controlled by the bot.
MAINTENANCE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

# --- Database Engine ---
# Creates a SQLAlchemy engine to establish a connection to the database.
# This engine is used by the health check endpoint to verify database connectivity.
engine = create_engine(DATABASE_URL)

# --- Flask Web Application ---
# Initializes the Flask web application.
# It's named 'run' to explicitly match the Gunicorn startup command `web:run`.
run = Flask(__name__)

@run.route('/')
def home():
    """
    A simple home page endpoint for the Flask application.
    When accessed, it provides a basic confirmation message that the web service is operational.
    """
    logger.info("Accessing / (home) endpoint.")
    return "Lottery Bot Web Service is running. Check /health for detailed status."

@run.route('/health')
def health_check():
    """
    Health check endpoint for the Flask application.
    This endpoint is crucial for monitoring the service's health in a production environment.
    It performs two key checks:
    1.  **Database Connectivity**: Attempts to execute a simple query to ensure the database is reachable and responsive.
    2.  **Maintenance Mode Status**: Reports the current state of the bot's maintenance flag.
    Returns a JSON response indicating the service's status and relevant details.
    """
    try:
        # Attempt to connect to the database and execute a lightweight query.
        # `connection.execute(func.now())` is a common way to test database connectivity
        # that is compatible across various database systems (PostgreSQL, SQLite, etc.).
        with engine.connect() as connection:
            connection.execute(func.now()) # Tests connectivity
        
        # Determine the overall service status based on the global MAINTENANCE flag.
        # If MAINTENANCE is true, the service is considered unavailable (503).
        status = "MAINTENANCE" if MAINTENANCE else "OK"
        # Set the appropriate HTTP status code based on the service's condition.
        status_code = 503 if MAINTENANCE else 200 # 503 Service Unavailable during maintenance
        
        logger.info(f"Health check successful. Status: {status}, DB: connected.")
        # Return a JSON response with the service status, database status,
        # maintenance mode, and a timestamp for when the check was performed.
        return jsonify(
            status=status,
            database="connected",
            maintenance_mode=MAINTENANCE,
            timestamp=datetime.now(pytz.utc).isoformat() # Provide timezone-aware ISO format timestamp
        ), status_code
    except Exception as e:
        # If any error occurs during the health check (e.g., database connection failure),
        # log the error and return an "ERROR" status with a 500 HTTP code.
        logger.error(f"Health check database connection failed: {e}")
        return jsonify(
            status="ERROR",
            database="disconnected",
            maintenance_mode=MAINTENANCE,
            error=str(e),
            timestamp=datetime.now(pytz.utc).isoformat()
        ), 500 # 500 Internal Server Error indicates a critical issue

# This conditional block ensures that the Flask application is run directly
# only when the script is executed as the main program (e.g., during local development).
# In a production environment with Gunicorn, Gunicorn directly imports the `run` object,
# so this block will not be executed.
if __name__ == "__main__":
    # Retrieve the port from environment variables, defaulting to 5000 for local testing.
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask web service locally on http://0.0.0.0:{port}")
    # Run the Flask application, listening on all available network interfaces.
    run.run(host='0.0.0.0', port=port, debug=False) # debug=False for safer local testing

