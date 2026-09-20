from fastapi import FastAPI, Request, Depends, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3
import os
import qrcode
import io
import random
import string
import subprocess

from .database import get_db

app = FastAPI(title="Cinematic Break")

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
    cursor.execute("SELECT * FROM sessions ORDER BY start_time DESC")
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

    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "sessions": active_sessions, 
            "media_items": media_items
        }
    )

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
    if os.path.exists(MEDIA_DIR):
        for root, dirs, files in os.walk(MEDIA_DIR):
            for f in files:
                if not f.startswith("."):
                    title = os.path.splitext(f)[0].replace(".", " ").replace("_", " ").title()
                    if title == movie_title:
                        duration = get_video_duration(os.path.join(root, f))
                        break

    join_code = ''.join(random.choices(string.digits, k=8))
    cursor.execute(
        "INSERT INTO sessions (join_code, session_name, theater_number, movie_title, duration_seconds) VALUES (?, ?, ?, ?, ?)",
        (join_code, session_name, theater_number, movie_title, duration)
    )
    db.commit()
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/sessions/{session_id}/qr")
async def get_session_qr(session_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT join_code FROM sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    if not row:
        return Response(status_code=404)
        
    join_code = row['join_code']
    img = qrcode.make(join_code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

@app.get("/theater/{theater_number}")
async def view_theater(request: Request, theater_number: str, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM sessions WHERE theater_number = ?", (theater_number,))
    session = cursor.fetchone()
    return templates.TemplateResponse("theater.html", {"request": request, "session": session, "theater_number": theater_number})
