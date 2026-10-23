from fastapi import FastAPI, Request, Depends, Form, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
from typing import Union, Optional
import sqlite3
import os
import qrcode
import io
import random
import string
import subprocess
import shutil
import asyncio
import uuid
import bcrypt

import mimetypes

mimetypes.add_type('application/vnd.apple.mpegurl', '.m3u8')
mimetypes.add_type('video/mp2t', '.ts')

from .database import get_db, DB_PATH

app = FastAPI(title="Cinematic Break")

# ----------------- SECURITY & RATE LIMITING HELPERS -----------------

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip
    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"

def is_request_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.lower() == "https"

rate_limit_records: dict[str, dict] = {}

def check_rate_limit(
    target: Union[Request, str],
    max_attempts: int = 5,
    window_seconds: int = 60,
    error_message: str = "Too many attempts. Please try again later."
):
    key = get_client_ip(target) if isinstance(target, Request) else target
    now = datetime.now(timezone.utc)
    if len(rate_limit_records) > 500:
        for k in list(rate_limit_records.keys()):
            if (now - rate_limit_records[k]["last_attempt"]).total_seconds() > 3600:
                del rate_limit_records[k]

    entry = rate_limit_records.get(key)
    if entry:
        elapsed = (now - entry["last_attempt"]).total_seconds()
        if elapsed > window_seconds:
            del rate_limit_records[key]
        elif entry["count"] >= max_attempts:
            raise HTTPException(status_code=429, detail=error_message)

def record_failed_attempt(target: Union[Request, str]):
    key = get_client_ip(target) if isinstance(target, Request) else target
    now = datetime.now(timezone.utc)
    entry = rate_limit_records.get(key)
    if not entry or (now - entry["last_attempt"]).total_seconds() > 3600:
        rate_limit_records[key] = {"count": 1, "last_attempt": now}
    else:
        entry["count"] += 1
        entry["last_attempt"] = now

def clear_rate_limit(target: Union[Request, str]):
    key = get_client_ip(target) if isinstance(target, Request) else target
    rate_limit_records.pop(key, None)

def admin_exists(db: sqlite3.Connection) -> bool:
    cursor = db.cursor()
    cursor.execute("SELECT 1 FROM admin_users LIMIT 1")
    return cursor.fetchone() is not None

def get_current_admin(request: Request, db: sqlite3.Connection = Depends(get_db)):
    token = request.cookies.get("admin_session")
    if not token:
        return None
    cursor = db.cursor()
    cursor.execute("SELECT id, username, password_hash, session_token FROM admin_users WHERE session_token = ?", (token,))
    return cursor.fetchone()

@app.middleware("http")
async def add_cache_control_header(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith(".m3u8") or request.url.path.endswith(".ts"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

ffmpeg_processes = {}

def cleanup_session_hls(session_id: int):
    if session_id in ffmpeg_processes:
        try:
            ffmpeg_processes[session_id].terminate()
        except Exception:
            pass
        del ffmpeg_processes[session_id]
    hls_dir = os.path.join("app", "static", "hls", str(session_id))
    if os.path.exists(hls_dir):
        shutil.rmtree(hls_dir, ignore_errors=True)

def cleanup_expired_sessions(db: sqlite3.Connection):
    """Auto-clean sessions exceeding movie duration or admin auto-timeout, and expired patron tickets."""
    cursor = db.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'session_timeout_minutes'")
    setting_row = cursor.fetchone()
    timeout_minutes = 10
    if setting_row:
        raw_val = setting_row['value'] if isinstance(setting_row, sqlite3.Row) else setting_row[0]
        if raw_val:
            try:
                timeout_minutes = int(str(raw_val).strip())
            except ValueError:
                timeout_minutes = 10

    timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else 0

    if timeout_seconds > 0:
        cursor.execute("""
            SELECT id FROM sessions 
            WHERE (
                CASE 
                    WHEN is_paused = 1 THEN COALESCE(accumulated_seconds, 0)
                    ELSE COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now')))
                END
            ) >= ? OR (
                duration_seconds > 0 AND (
                    CASE 
                        WHEN is_paused = 1 THEN COALESCE(accumulated_seconds, 0)
                        ELSE COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now')))
                    END
                ) >= duration_seconds
            )
        """, (timeout_seconds,))
    else:
        cursor.execute("""
            SELECT id FROM sessions 
            WHERE duration_seconds > 0 AND (
                CASE 
                    WHEN is_paused = 1 THEN COALESCE(accumulated_seconds, 0)
                    ELSE COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now')))
                END
            ) >= duration_seconds
        """)

    expired_sessions = cursor.fetchall()
    for row in expired_sessions:
        s_id = row['id'] if isinstance(row, sqlite3.Row) else row[0]
        cleanup_session_hls(s_id)
        cursor.execute("DELETE FROM tickets WHERE session_id = ?", (s_id,))
        cursor.execute("DELETE FROM sessions WHERE id = ?", (s_id,))

    # Clean up tickets that exceeded the break timeout
    if timeout_seconds > 0:
        cursor.execute("""
            DELETE FROM tickets 
            WHERE is_used = 1 AND first_active IS NOT NULL AND (strftime('%s', 'now') - strftime('%s', first_active)) >= ?
        """, (timeout_seconds,))

    db.commit()

async def auto_timeout_checker():
    """Background task to periodically clean up sessions exceeding the configured timeout."""
    while True:
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                cleanup_expired_sessions(conn)
            finally:
                conn.close()
        except Exception:
            pass
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_timeout_checker())

def get_base_url(request: Request, db: sqlite3.Connection):
    cursor = db.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'external_dns'")
    row = cursor.fetchone()
    if row and row['value']:
        dns = row['value'].strip()
        if not dns.startswith('http'):
            dns = f"http://{dns}"
        if not dns.endswith('/'):
            dns += '/'
        return dns
    return str(request.base_url)

# Ensure static and templates directories exist for mounting
os.makedirs(os.path.join(os.path.dirname(__file__), "static"), exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(__file__), "templates"), exist_ok=True)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

MEDIA_DIR = "/media" if os.path.exists("/media") else os.path.join(os.path.dirname(os.path.dirname(__file__)), "media")
os.makedirs(MEDIA_DIR, exist_ok=True)

def get_video_duration(file_path: str) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return int(float(result.stdout.strip()))
    except Exception:
        pass
    return 7200

def get_video_codec(file_path: str) -> str:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().lower()
    except Exception:
        pass
    return "unknown"

# ----------------- AUTH & ADMIN MANAGEMENT -----------------

@app.get("/setup", response_class=HTMLResponse)
async def view_setup(request: Request, db: sqlite3.Connection = Depends(get_db)):
    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("setup.html", {"request": request, "error": None})

@app.post("/setup")
async def process_setup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: sqlite3.Connection = Depends(get_db)
):
    ip = get_client_ip(request)
    try:
        check_rate_limit(f"setup_{ip}", max_attempts=5, window_seconds=60)
    except HTTPException:
        return templates.TemplateResponse("setup.html", {"request": request, "error": "Too many setup attempts. Please wait a minute."}, status_code=429)

    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)

    username = username.strip()
    if not username or not password:
        record_failed_attempt(f"setup_{ip}")
        return templates.TemplateResponse("setup.html", {"request": request, "error": "Username and password are required."}, status_code=400)

    if password != confirm_password:
        record_failed_attempt(f"setup_{ip}")
        return templates.TemplateResponse("setup.html", {"request": request, "error": "Passwords do not match. Please try again."}, status_code=400)

    salt = bcrypt.gensalt()
    pw_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    session_token = str(uuid.uuid4())

    cursor = db.cursor()
    cursor.execute("INSERT INTO admin_users (username, password_hash, session_token) VALUES (?, ?, ?)", (username, pw_hash, session_token))
    db.commit()

    clear_rate_limit(f"setup_{ip}")
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=is_request_secure(request)
    )
    return response

@app.get("/login", response_class=HTMLResponse)
async def view_login(
    request: Request,
    error: Optional[str] = None,
    setup_success: Optional[int] = None,
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if not admin_exists(db):
        return RedirectResponse(url="/setup", status_code=303)
    if current_admin:
        return RedirectResponse(url="/", status_code=303)

    error_msg = None
    if error == "1":
        error_msg = "Invalid username or password."
    elif error == "rate_limited":
        error_msg = "Too many failed login attempts. Please wait 1 minute."

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": error_msg,
            "setup_success": bool(setup_success)
        }
    )

@app.post("/login")
async def process_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: sqlite3.Connection = Depends(get_db)
):
    if not admin_exists(db):
        return RedirectResponse(url="/setup", status_code=303)

    ip = get_client_ip(request)
    rate_key = f"login_{ip}"
    try:
        check_rate_limit(rate_key, max_attempts=5, window_seconds=60)
    except HTTPException:
        return RedirectResponse(url="/login?error=rate_limited", status_code=303)

    cursor = db.cursor()
    cursor.execute("SELECT id, username, password_hash FROM admin_users WHERE username = ?", (username.strip(),))
    user = cursor.fetchone()

    if user and bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
        clear_rate_limit(rate_key)
        session_token = str(uuid.uuid4())
        cursor.execute("UPDATE admin_users SET session_token = ? WHERE id = ?", (session_token, user['id']))
        db.commit()

        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key="admin_session",
            value=session_token,
            httponly=True,
            samesite="lax",
            secure=is_request_secure(request)
        )
        return response

    record_failed_attempt(rate_key)
    return RedirectResponse(url="/login?error=1", status_code=303)

@app.api_route("/logout", methods=["GET", "POST"])
async def process_logout(
    request: Request,
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if current_admin:
        cursor = db.cursor()
        cursor.execute("UPDATE admin_users SET session_token = NULL WHERE id = ?", (current_admin['id'],))
        db.commit()

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("admin_session")
    return response

# ----------------- ADMIN DASHBOARD & SETTINGS -----------------

@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if not admin_exists(db):
        return RedirectResponse(url="/setup", status_code=303)
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)

    # Auto-cleanup expired sessions & tickets
    cleanup_expired_sessions(db)

    cursor = db.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'session_timeout_minutes'")
    setting_row = cursor.fetchone()
    timeout_minutes = 10
    if setting_row:
        raw_val = setting_row['value'] if isinstance(setting_row, sqlite3.Row) else setting_row[0]
        if raw_val:
            try:
                timeout_minutes = int(str(raw_val).strip())
            except ValueError:
                timeout_minutes = 10
    timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else 0

    cursor.execute("""
        SELECT *, 
        CASE 
            WHEN is_paused = 1 THEN COALESCE(accumulated_seconds, 0)
            ELSE COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now')))
        END AS elapsed_seconds 
        FROM sessions ORDER BY start_time DESC
    """)
    raw_sessions = cursor.fetchall()
    active_sessions = []
    for s in raw_sessions:
        s_dict = dict(s)
        s_dict['elapsed_seconds'] = int(s_dict.get('elapsed_seconds') or 0)
        if timeout_seconds > 0:
            if s_dict['duration_seconds'] > 0:
                s_dict['duration_seconds'] = min(s_dict['duration_seconds'], timeout_seconds)
            else:
                s_dict['duration_seconds'] = timeout_seconds
        active_sessions.append(s_dict)
    
    # Scan media directory recursively
    media_items = []
    if os.path.exists(MEDIA_DIR):
        for root, dirs, files in os.walk(MEDIA_DIR):
            for f in files:
                if not f.startswith("."):
                    title = os.path.splitext(f)[0].replace(".", " ").replace("_", " ").title()
                    media_items.append({
                        "filename": f,
                        "title": title
                    })

    base_url = get_base_url(request, db)
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "sessions": active_sessions, 
            "media_items": media_items,
            "base_url": base_url,
            "current_admin": current_admin
        }
    )

@app.get("/settings")
async def get_settings(
    request: Request,
    pw_success: Optional[int] = None,
    pw_error: Optional[str] = None,
    settings_success: Optional[int] = None,
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if not admin_exists(db):
        return RedirectResponse(url="/setup", status_code=303)
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)

    cursor = db.cursor()
    cursor.execute("SELECT * FROM settings")
    rows = cursor.fetchall()
    settings = {row['key']: row['value'] for row in rows}

    err_msg = None
    if pw_error == "mismatch":
        err_msg = "New passwords do not match."
    elif pw_error == "invalid_current":
        err_msg = "Current password is incorrect."
    elif pw_error == "rate_limited":
        err_msg = "Too many password change attempts. Please wait 1 minute."

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "pw_success": bool(pw_success),
            "pw_error": err_msg,
            "settings_success": bool(settings_success),
            "current_admin": current_admin
        }
    )

@app.post("/settings")
async def save_settings(
    request: Request,
    external_dns: str = Form(""),
    session_timeout_minutes: str = Form("10"),
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)

    ip = get_client_ip(request)
    rate_key = f"settings_{ip}"
    check_rate_limit(rate_key, max_attempts=15, window_seconds=60, error_message="Too many settings updates. Please try again later.")
    record_failed_attempt(rate_key)

    cursor = db.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('external_dns', ?)", (external_dns.strip(),))

    try:
        t_val = int(session_timeout_minutes.strip())
        if t_val < 0:
            t_val = 0
    except ValueError:
        t_val = 10

    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('session_timeout_minutes', ?)", (str(t_val),))
    db.commit()

    cleanup_expired_sessions(db)
    return RedirectResponse(url="/settings?settings_success=1", status_code=303)

@app.post("/settings/change_password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)

    ip = get_client_ip(request)
    rate_key = f"pw_change_{ip}"
    try:
        check_rate_limit(rate_key, max_attempts=5, window_seconds=60)
    except HTTPException:
        return RedirectResponse(url="/settings?pw_error=rate_limited", status_code=303)

    if not bcrypt.checkpw(current_password.encode('utf-8'), current_admin['password_hash'].encode('utf-8')):
        record_failed_attempt(rate_key)
        return RedirectResponse(url="/settings?pw_error=invalid_current", status_code=303)

    if new_password != confirm_password:
        return RedirectResponse(url="/settings?pw_error=mismatch", status_code=303)

    clear_rate_limit(rate_key)
    salt = bcrypt.gensalt()
    new_hash = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    cursor = db.cursor()
    cursor.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?", (new_hash, current_admin['id']))
    db.commit()

    return RedirectResponse(url="/settings?pw_success=1", status_code=303)

@app.post("/sessions")
async def create_session(
    movie_title: str = Form(...),
    session_name: str = Form(None),
    theater_number: str = Form(None),
    current_admin = Depends(get_current_admin),
    db: sqlite3.Connection = Depends(get_db)
):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    if theater_number:
        cursor.execute("DELETE FROM sessions WHERE theater_number = ?", (theater_number,))
        db.commit()

    duration = 7200
    media_file_path = None
    if os.path.exists(MEDIA_DIR):
        for root, dirs, files in os.walk(MEDIA_DIR):
            for f in files:
                if not f.startswith("."):
                    title = os.path.splitext(f)[0].replace(".", " ").replace("_", " ").title()
                    if title == movie_title:
                        media_file_path = os.path.join(root, f)
                        duration = get_video_duration(media_file_path)
                        break
            if media_file_path:
                break

    join_code = ''.join(random.choices(string.digits, k=8))
    cursor.execute("""
        INSERT INTO sessions (
            join_code, session_name, theater_number, movie_title, duration_seconds,
            start_time, is_paused, accumulated_seconds, last_updated_time
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, 0, CURRENT_TIMESTAMP)
    """, (join_code, session_name, theater_number, movie_title, duration))
    session_id = cursor.lastrowid
    
    for _ in range(100):
        ticket_number = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        cursor.execute("INSERT INTO tickets (session_id, ticket_number) VALUES (?, ?)", (session_id, ticket_number))
        
    db.commit()
    
    if media_file_path:
        cleanup_session_hls(session_id)
        file_ext = os.path.splitext(media_file_path)[1].lower()
        if file_ext != '.mp4':
            hls_dir = os.path.join("app", "static", "hls", str(session_id))
            os.makedirs(hls_dir, exist_ok=True)
            
            # Generate AES-128 key and key_info
            key_path = os.path.join(hls_dir, "key.bin")
            key_info_path = os.path.join(hls_dir, "key_info.txt")
            
            with open(key_path, "wb") as f:
                f.write(os.urandom(16))
            
            with open(key_info_path, "w") as f:
                f.write(f"/sessions/{session_id}/key.bin\n")
                f.write(f"{key_path}\n")
            
            # Detect video codec to determine if direct stream copy is supported
            v_codec = get_video_codec(media_file_path)
            if v_codec == "h264":
                video_args = ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"]
            else:
                # Transcode non-H.264 video (e.g. HEVC/H.265, VP9, AV1) to standard H.264 for universal Hls.js browser playback
                video_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p"]

            log_file = open(os.path.join(hls_dir, "ffmpeg.log"), "w")
            proc = subprocess.Popen([
                "ffmpeg", "-y", "-fflags", "+genpts", "-i", media_file_path,
                *video_args,
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-sn",
                "-avoid_negative_ts", "make_non_negative",
                "-start_number", "0", "-hls_time", "4", "-hls_list_size", "0",
                "-hls_flags", "temp_file",
                "-hls_key_info_file", key_info_path,
                "-f", "hls", os.path.join(hls_dir, "index.m3u8")
            ], stdout=subprocess.DEVNULL, stderr=log_file)
            ffmpeg_processes[session_id] = proc
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    cursor.execute("DELETE FROM tickets WHERE session_id = ?", (session_id,))
    db.commit()
    cleanup_session_hls(session_id)
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/{session_id}/pause")
async def pause_session(session_id: int, current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET accumulated_seconds = COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now'))),
            is_paused = 1,
            last_updated_time = CURRENT_TIMESTAMP
        WHERE id = ? AND is_paused = 0
    """, (session_id,))
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/{session_id}/resume")
async def resume_session(session_id: int, current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET is_paused = 0,
            last_updated_time = CURRENT_TIMESTAMP
        WHERE id = ? AND is_paused = 1
    """, (session_id,))
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/stop_all")
async def stop_all_sessions(current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    cursor.execute("DELETE FROM sessions")
    cursor.execute("DELETE FROM tickets")
    db.commit()
    for sid in list(ffmpeg_processes.keys()):
        cleanup_session_hls(sid)
    shutil.rmtree(os.path.join("app", "static", "hls"), ignore_errors=True)
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/pause_all")
async def pause_all_sessions(current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET accumulated_seconds = COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now'))),
            is_paused = 1,
            last_updated_time = CURRENT_TIMESTAMP
        WHERE is_paused = 0
    """)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/resume_all")
async def resume_all_sessions(current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return RedirectResponse(url="/login", status_code=303)
    cursor = db.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET is_paused = 0,
            last_updated_time = CURRENT_TIMESTAMP
        WHERE is_paused = 1
    """)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/sessions/{session_id}/state")
async def get_session_state(request: Request, session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cleanup_expired_sessions(db)

    cursor = db.cursor()
    cursor.execute("""
        SELECT is_paused, join_code, theater_number,
        CASE 
            WHEN is_paused = 1 THEN COALESCE(accumulated_seconds, 0)
            ELSE COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now')))
        END AS elapsed_seconds 
        FROM sessions WHERE id = ?
    """, (session_id,))
    session = cursor.fetchone()
    if not session:
        return JSONResponse({"detail": "This session has timed out after reaching the maximum allowed duration."}, status_code=404)
        
    join_code = session['join_code']
    ticket_number = request.cookies.get(f"ticket_{join_code}")
    if ticket_number:
        cursor.execute("SELECT id, is_banned, first_active FROM tickets WHERE session_id = ? AND ticket_number = ?", (session_id, ticket_number))
        ticket = cursor.fetchone()
        if not ticket:
            return JSONResponse({"detail": "Your break session has timed out or was ended by the administrator."}, status_code=403)
        if ticket['is_banned']:
            return JSONResponse({"detail": "Ticket banned for watermark tampering."}, status_code=403)
            
        # Check ticket break duration timeout
        cursor.execute("SELECT value FROM settings WHERE key = 'session_timeout_minutes'")
        s_row = cursor.fetchone()
        timeout_minutes = 10
        if s_row:
            raw_val = s_row['value'] if isinstance(s_row, sqlite3.Row) else s_row[0]
            if raw_val:
                try:
                    timeout_minutes = int(str(raw_val).strip())
                except ValueError:
                    timeout_minutes = 10
        timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else 0

        if timeout_seconds > 0 and ticket['first_active']:
            cursor.execute("SELECT (strftime('%s', 'now') - strftime('%s', first_active)) as elapsed FROM tickets WHERE id = ?", (ticket['id'],))
            dur_row = cursor.fetchone()
            if dur_row and dur_row['elapsed'] and dur_row['elapsed'] >= timeout_seconds:
                cursor.execute("DELETE FROM tickets WHERE id = ?", (ticket['id'],))
                db.commit()
                theater_str = f" to Theater {session['theater_number']}" if session['theater_number'] and session['theater_number'] != 'None' else ""
                return JSONResponse({"detail": f"Your break session has timed out ({timeout_minutes} min limit). Please return{theater_str}."}, status_code=403)

        cursor.execute("UPDATE tickets SET last_active = CURRENT_TIMESTAMP WHERE id = ?", (ticket['id'],))
        db.commit()

    return {"is_paused": bool(session["is_paused"]), "elapsed_seconds": session["elapsed_seconds"]}

@app.post("/admin/tickets/{ticket_id}/ban")
async def admin_ban_ticket(ticket_id: int, current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return Response(status_code=401)
    cursor = db.cursor()
    cursor.execute("UPDATE tickets SET is_banned = 1 WHERE id = ?", (ticket_id,))
    db.commit()
    return Response(status_code=200)

@app.post("/admin/tickets/{ticket_id}/end")
async def admin_end_ticket(ticket_id: int, current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return Response(status_code=401)
    cursor = db.cursor()
    cursor.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    db.commit()
    return Response(status_code=200)

@app.post("/sessions/{session_id}/ban")
async def ban_ticket(session_id: int, request: Request, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT join_code FROM sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
        
    join_code = session['join_code']
    ticket_number = request.cookies.get(f"ticket_{join_code}")
    if ticket_number:
        cursor.execute("UPDATE tickets SET is_banned = 1 WHERE session_id = ? AND ticket_number = ?", (session_id, ticket_number))
        db.commit()
    return Response(status_code=200)

@app.get("/sessions/{session_id}/qr")
async def get_session_qr(request: Request, session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT join_code FROM sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
        
    base_url = get_base_url(request, db)
    player_url = f"{base_url}player/{session['join_code']}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(player_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="white", back_color="#111111")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

@app.get("/theater/{theater_number}")
async def view_theater(request: Request, theater_number: str, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM sessions WHERE theater_number = ?", (theater_number,))
    session = cursor.fetchone()
    return templates.TemplateResponse("theater.html", {"request": request, "session": session, "theater_number": theater_number})

@app.get("/sessions/{session_id}/tickets")
async def view_tickets(session_id: int, current_admin = Depends(get_current_admin), db: sqlite3.Connection = Depends(get_db)):
    if not current_admin:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    cursor = db.cursor()
    cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
        
    cursor.execute("""
        SELECT id, ticket_number, is_used, is_banned,
        CASE WHEN is_used = 1 AND strftime('%s', 'now') - strftime('%s', last_active) < 10 THEN 1 ELSE 0 END as currently_active
        FROM tickets 
        WHERE session_id = ?
    """, (session_id,))
    tickets = [{"id": row['id'], "ticket_number": row['ticket_number'], "is_used": row['is_used'], "is_banned": row['is_banned'], "currently_active": row['currently_active']} for row in cursor.fetchall()]
    
    return {"tickets": tickets}

@app.get("/join/{join_code}")
async def join_session_get(request: Request, join_code: str, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM sessions WHERE join_code = ?", (join_code,))
    session = cursor.fetchone()
    if not session:
        return HTMLResponse("<h1>Session not found or has ended.</h1>", status_code=404)
        
    return templates.TemplateResponse("join.html", {"request": request, "session": session, "error": None})

@app.post("/join/{join_code}")
async def join_session_post(request: Request, join_code: str, ticket_number: str = Form(...), db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM sessions WHERE join_code = ?", (join_code,))
    session = cursor.fetchone()
    if not session:
        return HTMLResponse("<h1>Session not found or has ended.</h1>", status_code=404)
        
    session_id = session['id']
    ticket_number = ticket_number.strip().upper()
    
    cursor.execute("""
        SELECT id, is_used, 
        CASE WHEN is_used = 1 AND strftime('%s', 'now') - strftime('%s', last_active) < 10 THEN 1 ELSE 0 END as currently_active
        FROM tickets 
        WHERE session_id = ? AND ticket_number = ?
    """, (session_id, ticket_number))
    ticket = cursor.fetchone()
    
    if not ticket:
        return templates.TemplateResponse("join.html", {"request": request, "session": session, "error": "Invalid ticket number."})
        
    if ticket['currently_active'] == 1:
        return templates.TemplateResponse("join.html", {"request": request, "session": session, "error": "Ticket is currently in use by another device."})
        
    cursor.execute("UPDATE tickets SET is_used = 1, first_active = CURRENT_TIMESTAMP, last_active = CURRENT_TIMESTAMP WHERE id = ?", (ticket['id'],))
    db.commit()
    
    response = RedirectResponse(url=f"/player/{join_code}", status_code=303)
    response.set_cookie(key=f"ticket_{join_code}", value=ticket_number, httponly=True)
    return response

@app.get("/player/{join_code}")
async def view_player(request: Request, join_code: str, db: sqlite3.Connection = Depends(get_db)):
    ticket_number = request.cookies.get(f"ticket_{join_code}")
    if not ticket_number:
        return RedirectResponse(url=f"/join/{join_code}")

    cursor = db.cursor()
    cursor.execute("""
        SELECT *, 
        CASE 
            WHEN is_paused = 1 THEN COALESCE(accumulated_seconds, 0)
            ELSE COALESCE(accumulated_seconds, 0) + (strftime('%s', 'now') - strftime('%s', COALESCE(last_updated_time, start_time, 'now')))
        END AS elapsed_seconds 
        FROM sessions WHERE join_code = ?
    """, (join_code,))
    session = cursor.fetchone()
    if not session:
        return HTMLResponse("<h1>Session not found or has ended.</h1>", status_code=404)
        
    cursor.execute("SELECT is_banned FROM tickets WHERE session_id = ? AND ticket_number = ?", (session['id'], ticket_number))
    ticket = cursor.fetchone()
    if not ticket:
        return RedirectResponse(url=f"/join/{join_code}")
        
    if ticket['is_banned']:
        html_content = """
        <html><head><title>Access Revoked</title><link rel="stylesheet" href="/static/main.css"><script src="https://unpkg.com/lucide@latest"></script></head><body style="background:#000;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;font-family:sans-serif;">
        <div style="text-align:center;"><i data-lucide="shield-alert" style="width:80px;height:80px;color:#e50914;"></i><h2 style="color:#e50914;font-size:2em;margin-top:20px;">Access Revoked</h2><p style="font-size:1.2em;color:#aaa;">Ticket banned for watermark tampering.</p></div>
        <script>lucide.createIcons();</script></body></html>
        """
        return HTMLResponse(content=html_content, status_code=403)
    
    hls_dir = os.path.join("app", "static", "hls", str(session['id']))
    use_hls = os.path.exists(hls_dir)
    
    return templates.TemplateResponse("player.html", {"request": request, "session": session, "use_hls": use_hls, "ticket_number": ticket_number})

@app.get("/sessions/{session_id}/key.bin")
async def get_hls_key(request: Request, session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT join_code FROM sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
        
    join_code = session['join_code']
    ticket_number = request.cookies.get(f"ticket_{join_code}")
    if not ticket_number:
        return Response(status_code=403)
        
    cursor.execute("SELECT is_banned FROM tickets WHERE session_id = ? AND ticket_number = ?", (session_id, ticket_number))
    ticket = cursor.fetchone()
    if not ticket or ticket['is_banned']:
        return Response(status_code=403)
        
    key_path = os.path.join("app", "static", "hls", str(session_id), "key.bin")
    if not os.path.exists(key_path):
        return Response(status_code=404)
        
    return FileResponse(key_path, media_type="application/octet-stream", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

@app.get("/stream/{join_code}")
async def stream_media(request: Request, join_code: str, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT id, movie_title FROM sessions WHERE join_code = ?", (join_code,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
    
    # Check ticket status if ticket cookie exists
    ticket_number = request.cookies.get(f"ticket_{join_code}")
    if ticket_number:
        cursor.execute("SELECT is_banned FROM tickets WHERE session_id = ? AND ticket_number = ?", (session['id'], ticket_number))
        t_row = cursor.fetchone()
        if not t_row or t_row['is_banned']:
            return Response(status_code=403)

    movie_title = session['movie_title']
    
    file_path = None
    if os.path.exists(MEDIA_DIR):
        for root, dirs, files in os.walk(MEDIA_DIR):
            for f in files:
                if not f.startswith("."):
                    title = os.path.splitext(f)[0].replace(".", " ").replace("_", " ").title()
                    if title == movie_title:
                        file_path = os.path.join(root, f)
                        break
            if file_path:
                break
                
    if not file_path or not os.path.exists(file_path):
        return Response(status_code=404)
        
    file_size = os.stat(file_path).st_size
    range_header = request.headers.get("range")
    
    def stream_file(path: str, start: int, end: int):
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            chunk_size = 1024 * 1024 * 2 # 2MB chunks
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    if range_header:
        range_match = range_header.strip().split("=")[-1]
        range_parts = range_match.split("-")
        start = int(range_parts[0]) if range_parts[0] else 0
        end = int(range_parts[1]) if len(range_parts) > 1 and range_parts[1] else file_size - 1
        
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        
        length = end - start + 1
        
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        
        return StreamingResponse(
            stream_file(file_path, start, end),
            status_code=206,
            headers=headers,
            media_type="video/mp4"
        )
    else:
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        }
        return StreamingResponse(
            stream_file(file_path, 0, file_size - 1),
            status_code=200,
            headers=headers,
            media_type="video/mp4"
        )
