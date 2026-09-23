from fastapi import FastAPI, Request, Depends, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3
import os
import qrcode
import io
import random
import string
import subprocess
import shutil

import mimetypes

mimetypes.add_type('application/vnd.apple.mpegurl', '.m3u8')
mimetypes.add_type('video/mp2t', '.ts')

from .database import get_db

app = FastAPI(title="Cinematic Break")

ffmpeg_processes = {}

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

# Ensure static and templates directories exist for mounting
os.makedirs(os.path.join(os.path.dirname(__file__), "static"), exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(__file__), "templates"), exist_ok=True)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "media")
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

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: sqlite3.Connection = Depends(get_db)):
    # Fetch active sessions
    cursor = db.cursor()
    
    # Auto-cleanup expired sessions
    cursor.execute("""
        SELECT id FROM sessions 
        WHERE duration_seconds > 0 AND (
            CASE 
                WHEN is_paused = 1 THEN accumulated_seconds 
                ELSE accumulated_seconds + (strftime('%s', 'now') - strftime('%s', last_updated_time))
            END
        ) >= duration_seconds
    """)
    expired_sessions = cursor.fetchall()
    for row in expired_sessions:
        cleanup_session_hls(row['id'])

    cursor.execute("""
        DELETE FROM sessions 
        WHERE duration_seconds > 0 AND (
            CASE 
                WHEN is_paused = 1 THEN accumulated_seconds 
                ELSE accumulated_seconds + (strftime('%s', 'now') - strftime('%s', last_updated_time))
            END
        ) >= duration_seconds
    """)
    db.commit()
    
    cursor.execute("""
        SELECT *, 
        CASE 
            WHEN is_paused = 1 THEN accumulated_seconds 
            ELSE accumulated_seconds + (strftime('%s', 'now') - strftime('%s', last_updated_time))
        END AS elapsed_seconds 
        FROM sessions ORDER BY start_time DESC
    """)
    active_sessions = cursor.fetchall()
    
    # Scan media directory recursively
    media_items = []
    if os.path.exists(MEDIA_DIR):
        for root, dirs, files in os.walk(MEDIA_DIR):
            for f in files:
                if not f.startswith("."):
                    # Simplistic parsing of title from filename
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
            "base_url": base_url
        }
    )

@app.get("/settings")
async def get_settings(request: Request, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM settings")
    rows = cursor.fetchall()
    settings = {row['key']: row['value'] for row in rows}
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})

@app.post("/settings")
async def save_settings(external_dns: str = Form(""), db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('external_dns', ?)", (external_dns,))
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/sessions")
async def create_session(
    movie_title: str = Form(...),
    session_name: str = Form(None),
    theater_number: str = Form(None),
    db: sqlite3.Connection = Depends(get_db)
):
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
    cursor.execute(
        "INSERT INTO sessions (join_code, session_name, theater_number, movie_title, duration_seconds) VALUES (?, ?, ?, ?, ?)",
        (join_code, session_name, theater_number, movie_title, duration)
    )
    session_id = cursor.lastrowid
    db.commit()
    
    if media_file_path:
        file_ext = os.path.splitext(media_file_path)[1].lower()
        if file_ext != '.mp4':
            cleanup_session_hls(session_id)
            hls_dir = os.path.join("app", "static", "hls", str(session_id))
            os.makedirs(hls_dir, exist_ok=True)
            
            log_file = open(os.path.join(hls_dir, "ffmpeg.log"), "w")
            proc = subprocess.Popen([
                "ffmpeg", "-y", "-fflags", "+genpts", "-i", media_file_path,
                "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", 
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-sn",
                "-avoid_negative_ts", "make_non_negative",
                "-start_number", "0", "-hls_time", "4", "-hls_list_size", "0",
                "-hls_flags", "temp_file",
                "-f", "hls", os.path.join(hls_dir, "index.m3u8")
            ], stdout=subprocess.DEVNULL, stderr=log_file)
            ffmpeg_processes[session_id] = proc
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    cleanup_session_hls(session_id)
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/stop_all")
async def stop_all_sessions(db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("DELETE FROM sessions")
    db.commit()
    for sid in list(ffmpeg_processes.keys()):
        cleanup_session_hls(sid)
    shutil.rmtree(os.path.join("app", "static", "hls"), ignore_errors=True)
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/pause_all")
async def pause_all_sessions(db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET accumulated_seconds = accumulated_seconds + (strftime('%s', 'now') - strftime('%s', last_updated_time)),
            is_paused = 1,
            last_updated_time = CURRENT_TIMESTAMP
        WHERE is_paused = 0
    """)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/resume_all")
async def resume_all_sessions(db: sqlite3.Connection = Depends(get_db)):
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
async def get_session_state(session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        SELECT is_paused, 
        CASE 
            WHEN is_paused = 1 THEN accumulated_seconds 
            ELSE accumulated_seconds + (strftime('%s', 'now') - strftime('%s', last_updated_time))
        END AS elapsed_seconds 
        FROM sessions WHERE id = ?
    """, (session_id,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
    return {"is_paused": bool(session["is_paused"]), "elapsed_seconds": session["elapsed_seconds"]}

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

@app.get("/player/{join_code}")
async def view_player(request: Request, join_code: str, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        SELECT *, 
        CASE 
            WHEN is_paused = 1 THEN accumulated_seconds 
            ELSE accumulated_seconds + (strftime('%s', 'now') - strftime('%s', last_updated_time))
        END AS elapsed_seconds 
        FROM sessions WHERE join_code = ?
    """, (join_code,))
    session = cursor.fetchone()
    if not session:
        return HTMLResponse("<h1>Session not found or has ended.</h1>", status_code=404)
    
    hls_dir = os.path.join("app", "static", "hls", str(session['id']))
    use_hls = os.path.exists(hls_dir)
    
    return templates.TemplateResponse("player.html", {"request": request, "session": session, "use_hls": use_hls})

@app.get("/stream/{join_code}")
async def stream_media(request: Request, join_code: str, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT movie_title FROM sessions WHERE join_code = ?", (join_code,))
    session = cursor.fetchone()
    if not session:
        return Response(status_code=404)
    
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
