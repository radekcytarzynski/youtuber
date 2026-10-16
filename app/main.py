"""YT Audio: prywatna przejściówka do słuchania YouTube przez własny serwer."""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import youtube
from auth import COOKIE_NAME, Auth, LoginLocked
from config import settings
from db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(settings.data_dir / "app.db")
    app.state.auth = Auth(app.state.db)
    await youtube.startup(settings.data_dir)
    try:
        yield
    finally:
        await youtube.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def get_auth(request: Request) -> Auth:
    return request.app.state.auth


def require_user(request: Request) -> None:
    if not get_auth(request).session_valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Wymagane logowanie.")


@app.exception_handler(youtube.YouTubeError)
async def youtube_error(request: Request, exc: youtube.YouTubeError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)


def _human_duration(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{round(minutes / 60)} godz."


# --- logowanie ------------------------------------------------------------------


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    auth = get_auth(request)
    try:
        token = await auth.login(body.password, auth.client_ip(request), request.headers.get("user-agent"))
    except LoginLocked as exc:
        return JSONResponse(
            {
                "detail": f"Za dużo nieudanych prób. Spróbuj ponownie za {_human_duration(exc.retry_after)}.",
                "retry_after": exc.retry_after,
            },
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
    if token is None:
        raise HTTPException(status_code=401, detail="Nieprawidłowe hasło.")

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    get_auth(request).logout(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(require_user)])
async def me():
    return {"ok": True}


# --- YouTube --------------------------------------------------------------------


@app.get("/api/search", dependencies=[Depends(require_user)])
async def search(q: str = Query(min_length=1, max_length=200)):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Puste zapytanie.")
    return await youtube.search(query, settings.search_limit)


@app.get("/api/channel/{channel_id}", dependencies=[Depends(require_user)])
async def channel(channel_id: str, page: int = Query(1, ge=1, le=50)):
    return await youtube.channel(channel_id, page, settings.channel_page_size)


# --- ulubione kanały ------------------------------------------------------------


class FavoriteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _favorites(db: Database) -> dict:
    rows = db.query("SELECT channel_id, title FROM favorites ORDER BY title COLLATE NOCASE")
    return {"items": [{"id": row["channel_id"], "title": row["title"]} for row in rows]}


def _check_channel_id(channel_id: str) -> None:
    if not youtube.CHANNEL_RE.match(channel_id):
        raise HTTPException(status_code=400, detail="Nieprawidłowy identyfikator kanału.")


@app.get("/api/favorites", dependencies=[Depends(require_user)])
async def favorites(request: Request):
    return _favorites(request.app.state.db)


@app.put("/api/favorites/{channel_id}", dependencies=[Depends(require_user)])
async def add_favorite(channel_id: str, body: FavoriteIn, request: Request):
    _check_channel_id(channel_id)
    db = request.app.state.db
    db.execute(
        """
        INSERT INTO favorites (channel_id, title, added) VALUES (?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET title = excluded.title
        """,
        (channel_id, body.title.strip(), int(time.time())),
    )
    return _favorites(db)


@app.delete("/api/favorites/{channel_id}", dependencies=[Depends(require_user)])
async def remove_favorite(channel_id: str, request: Request):
    _check_channel_id(channel_id)
    db = request.app.state.db
    db.execute("DELETE FROM favorites WHERE channel_id = ?", (channel_id,))
    return _favorites(db)


@app.get("/api/audio/{video_id}", dependencies=[Depends(require_user)])
async def audio(video_id: str, request: Request):
    return await youtube.audio_response(video_id, request.headers.get("range"))


@app.get("/api/thumb/{video_id}", dependencies=[Depends(require_user)])
async def thumb(video_id: str):
    return await youtube.thumbnail(video_id)


# Frontend (pliki statyczne nie zawierają żadnych danych; dane są tylko w /api/*).
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
