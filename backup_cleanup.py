#!/usr/bin/env python3
"""
Database Backup and Maintenance Utility
Automatically manages backup rotation and cleanup
"""

import os
import sqlite3
from datetime import datetime, timedelta

# Configuration
DB_FILE = "/data/lottery_bot.db"
BACKUP_DIR = "/data/backups"
DAYS_TO_KEEP = 7  # Keep backups for 7 days

def create_backup():
    """Create timestamped database backup"""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{BACKUP_DIR}/backup_{timestamp}.db"
        
        with sqlite3.connect(DB_FILE) as src:
            with sqlite3.connect(backup_path) as dst:
                src.backup(dst)
        
        print(f"✓ Backup created: {backup_path}")
        return True
    except Exception as e:
        print(f"✗ Backup failed: {str(e)}")
        return False

def clean_old_backups():
    """Remove backups older than DAYS_TO_KEEP"""
    try:
        cutoff = datetime.now() - timedelta(days=DAYS_TO_KEEP)
        deleted_count = 0
        
        for filename in os.listdir(BACKUP_DIR):
            if filename.startswith("backup_") and filename.endswith(".db"):
                filepath = os.path.join(BACKUP_DIR, filename)
                mod_time = datetime.fromtimestamp(os.path.getmtime(filepath))
                
                if mod_time < cutoff:
                    os.remove(filepath)
                    deleted_count += 1
                    print(f"✓ Removed old backup: {filename}")
        
        print(f"✓ Cleanup complete. Removed {deleted_count} old backups")
        return True
    except Exception as e:
        print(f"✗ Cleanup failed: {str(e)}")
        return False

def verify_backups():
    """Check backup integrity"""
    try:
        backups = [f for f in os.listdir(BACKUP_DIR) 
                 if f.startswith("backup_") and f.endswith(".db")]
        
        if not backups:
            print("ℹ No backups found")
            return False
            
        latest = sorted(backups)[-1]
        test_path = f"{BACKUP_DIR}/verify_temp.db"
        
        # Test restore
        with sqlite3.connect(f"{BACKUP_DIR}/{latest}") as src:
            with sqlite3.connect(test_path) as dst:
                src.backup(dst)
        
        os.remove(test_path)
        print(f"✓ Backup verified: {latest}")
        return True
    except Exception as e:
        print(f"✗ Verification failed: {str(e)}")
        return False

if __name__ == "__main__":
    print("\n=== Lottery Bot Backup Maintenance ===")
    print(f"Database: {DB_FILE}")
    print(f"Backup Directory: {BACKUP_DIR}\n")
    
    create_backup()
    clean_old_backups()
    verify_backups()
    
    print("\nMaintenance completed")
