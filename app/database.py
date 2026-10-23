import sqlite3
import os
import shutil

# Save database inside /config when in Docker, or local ./config folder for persistence
CONFIG_DIR = "/config" if os.path.exists("/config") else os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_PATH = os.path.join(CONFIG_DIR, "cinematicbreak.db")

# If an existing database is found in the project root but not in config, migrate it to config
OLD_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cinematicbreak.db")
if os.path.abspath(DB_PATH) != os.path.abspath(OLD_DB_PATH) and not os.path.exists(DB_PATH) and os.path.exists(OLD_DB_PATH):
    try:
        shutil.copy2(OLD_DB_PATH, DB_PATH)
    except Exception:
        pass

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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            session_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Safely try to add columns to an existing table
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN is_paused INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN accumulated_seconds INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN last_updated_time TIMESTAMP")
    except sqlite3.OperationalError:
        pass

    cursor.execute("UPDATE sessions SET last_updated_time = COALESCE(start_time, CURRENT_TIMESTAMP) WHERE last_updated_time IS NULL")
    cursor.execute("UPDATE sessions SET accumulated_seconds = 0 WHERE accumulated_seconds IS NULL")
    cursor.execute("UPDATE sessions SET is_paused = 0 WHERE is_paused IS NULL")

    try:
        cursor.execute("ALTER TABLE tickets ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE tickets ADD COLUMN first_active TIMESTAMP")
        cursor.execute("UPDATE tickets SET first_active = CURRENT_TIMESTAMP WHERE first_active IS NULL AND is_used = 1")
    except sqlite3.OperationalError:
        pass

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('session_timeout_minutes', '10')")

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
