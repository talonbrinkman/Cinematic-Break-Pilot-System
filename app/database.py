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
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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
