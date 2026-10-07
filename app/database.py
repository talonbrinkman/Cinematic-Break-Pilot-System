import sqlite3
import os

DB_PATH = "cinematicbreak.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            join_code TEXT UNIQUE,
            session_name TEXT,
            theater_number TEXT,
            movie_title TEXT NOT NULL,
            duration_seconds INTEGER DEFAULT 0,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_paused INTEGER DEFAULT 0,
            accumulated_seconds INTEGER DEFAULT 0,
            last_updated_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            ticket_number TEXT,
            is_used INTEGER DEFAULT 0,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Safely try to add columns to an existing table
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN is_paused INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE sessions ADD COLUMN accumulated_seconds INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE sessions ADD COLUMN last_updated_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass # Columns already exist

    try:
        cursor.execute("ALTER TABLE tickets ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

# Initialize DB on import
init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Enables accessing columns by name
    try:
        yield conn
    finally:
        conn.close()
