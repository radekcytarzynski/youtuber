"""Dostęp do YouTube przez yt-dlp: wyszukiwanie, kanały, miniatury i strumień audio."""

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import yt_dlp
from fastapi.responses import FileResponse, Response, StreamingResponse

log = logging.getLogger("youtube")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_RE = re.compile(r"^(UC[A-Za-z0-9_-]{22}|@[A-Za-z0-9._-]{3,100})$")
WATCH_URL = "https://www.youtube.com/watch?v="

# Pobieranie z googlevideo w kawałkach (jak robi to yt-dlp), bo jedno długie
# zapytanie o cały plik YouTube potrafi mocno spowalniać.
UPSTREAM_CHUNK = 10 * 1024 * 1024
READ_SIZE = 64 * 1024
THUMB_KEEP = 5000

MIME_TYPES = {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm", "opus": "audio/ogg"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class YouTubeError(Exception):
    status_code = 502


class InvalidInput(YouTubeError):
    status_code = 400


class _TTLCache:
    def __init__(self, ttl: float, maxsize: int):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: dict = {}

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        if item[0] < time.monotonic():
            self._data.pop(key, None)
            return None
        return item[1]

    def set(self, key, value, ttl: float | None = None) -> None:
        if key not in self._data and len(self._data) >= self.maxsize:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + (ttl or self.ttl), value)

    def pop(self, key) -> None:
        self._data.pop(key, None)


_http: httpx.AsyncClient | None = None
_thumb_dir: Path | None = None
_ytdlp_cache_dir: Path | None = None
# yt-dlp jest synchroniczny i dość ciężki; ograniczamy liczbę równoczesnych wywołań.
_ytdlp_slots = asyncio.Semaphore(3)
_inflight: dict[str, asyncio.Future] = {}

_audio_cache = _TTLCache(ttl=3600, maxsize=300)
_search_cache = _TTLCache(ttl=600, maxsize=200)
_channel_cache = _TTLCache(ttl=600, maxsize=200)


# --- cykl życia -----------------------------------------------------------------


async def startup(data_dir: Path) -> None:
    global _http, _thumb_dir, _ytdlp_cache_dir
    _thumb_dir = data_dir / "thumbs"
    _ytdlp_cache_dir = data_dir / "yt-dlp-cache"
    _thumb_dir.mkdir(parents=True, exist_ok=True)
    _ytdlp_cache_dir.mkdir(parents=True, exist_ok=True)
    _prune_thumbs()
    _http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=True)


async def shutdown() -> None:
    if _http is not None:
        await _http.aclose()


def _prune_thumbs() -> None:
    files = sorted(_thumb_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[THUMB_KEEP:]:
        path.unlink(missing_ok=True)


# --- walidacja i rozpoznawanie linków -------------------------------------------


def check_video_id(video_id: str) -> None:
    if not VIDEO_ID_RE.match(video_id):
        raise InvalidInput("Nieprawidłowy identyfikator filmu.")


def parse_youtube_url(text: str) -> tuple[str, str] | None:
    """Rozpoznaje wklejony link: ('video', id) albo ('channel', id/@handle)."""
    text = text.strip()
    # Sam uchwyt (@nazwa) albo identyfikator kanału (UC...) bez linku.
    if (text.startswith("@") or text.startswith("UC")) and CHANNEL_RE.match(text):
        return ("channel", text)
    if not re.match(r"^(https?://)?([\w-]+\.)*(youtube\.com|youtu\.be)/", text, re.IGNORECASE):
        return None
    if not text.lower().startswith("http"):
        text = "https://" + text
    url = urlparse(text)
    host = (url.hostname or "").lower()
    parts = [p for p in url.path.split("/") if p]

    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif url.path == "/watch":
        candidate = parse_qs(url.query).get("v", [""])[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
        candidate = parts[1]
    elif len(parts) >= 2 and parts[0] == "channel" and CHANNEL_RE.match(parts[1]):
        return ("channel", parts[1])
    elif parts and parts[0].startswith("@") and CHANNEL_RE.match(parts[0]):
        return ("channel", parts[0])
    else:
        return None

    return ("video", candidate) if VIDEO_ID_RE.match(candidate) else None


# --- yt-dlp ---------------------------------------------------------------------


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": str(_ytdlp_cache_dir),
        "socket_timeout": 20,
        "extractor_retries": 2,
    }


def _friendly_error(exc: Exception) -> str:
    message = _ANSI_RE.sub("", str(exc)).removeprefix("ERROR: ").strip()
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return "YouTube zablokował zapytanie (weryfikacja anty-bot). Spróbuj ponownie za chwilę."
    if "private video" in lowered:
        return "To jest film prywatny."
    if "age" in lowered and ("confirm" in lowered or "restricted" in lowered):
        return "Film ma ograniczenie wiekowe i wymaga zalogowania na YouTube."
    if "video unavailable" in lowered or "this video is not available" in lowered:
        return "Film jest niedostępny."
    return "Błąd YouTube: " + message[:300]


async def _run(func, *args):
    async with _ytdlp_slots:
        try:
            return await asyncio.to_thread(func, *args)
        except yt_dlp.utils.YoutubeDLError as exc:
            log.warning("yt-dlp error: %s", _ANSI_RE.sub("", str(exc)))
            raise YouTubeError(_friendly_error(exc)) from None


async def _dedupe(key: str, factory):
    """Jedno wywołanie yt-dlp dla wielu równoczesnych zapytań o to samo."""
    future = _inflight.get(key)
    if future is None:
        future = asyncio.ensure_future(factory())
        _inflight[key] = future
        future.add_done_callback(lambda _: _inflight.pop(key, None))
    return await asyncio.shield(future)


def _entry(data: dict) -> dict | None:
    video_id = data.get("id")
    if not video_id or not VIDEO_ID_RE.match(video_id):
        return None
    duration = data.get("duration")
    return {
        "id": video_id,
        "title": data.get("title") or "(bez tytułu)",
        "channel": data.get("channel") or data.get("uploader"),
        "channel_id": data.get("channel_id"),
        "duration": int(duration) if duration else None,
        "views": data.get("view_count"),
        "live": data.get("live_status") == "is_live",
    }


def _search_sync(query: str, limit: int) -> list[dict]:
    opts = {**_base_opts(), "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [e for e in map(_entry, info.get("entries") or []) if e]


def _channel_sync(channel: str, page: int, size: int) -> dict:
    base = f"https://www.youtube.com/channel/{channel}" if channel.startswith("UC") else f"https://www.youtube.com/{channel}"
    first = (page - 1) * size + 1
    # Jeden film więcej niż strona, żeby wiedzieć, czy jest następna.
    opts = {**_base_opts(), "extract_flat": "in_playlist", "playlist_items": f"{first}-{first + size}"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(base + "/videos", download=False)

    name = info.get("channel") or info.get("uploader") or info.get("title")
    channel_id = info.get("channel_id") or (channel if channel.startswith("UC") else None)
    items = []
    for raw in info.get("entries") or []:
        data = dict(raw)
        data["channel"] = data.get("channel") or name
        data["channel_id"] = data.get("channel_id") or channel_id
        entry = _entry(data)
        if entry:
            items.append(entry)
    return {
        "channel": {"id": channel_id or channel, "title": name},
        "items": items[:size],
        "has_more": len(items) > size,
    }


def _url_param(url: str, name: str) -> int | None:
    value = parse_qs(urlparse(url).query).get(name, [None])[0]
    return int(value) if value and value.isdigit() else None


def _audio_sync(video_id: str) -> dict:
    opts = {**_base_opts(), "format": "bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(WATCH_URL + video_id, download=False)

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise YouTubeError("Transmisje na żywo i zaplanowane premiery nie są obsługiwane.")
    url = info.get("url")
    if not url:
        raise YouTubeError("Nie znaleziono strumienia audio dla tego filmu.")
    return {
        "url": url,
        "headers": dict(info.get("http_headers") or {}),
        "ext": info.get("ext"),
        "filesize": info.get("filesize") or _url_param(url, "clen"),
        "expire": _url_param(url, "expire"),
        "entry": _entry(info),
    }


# --- operacje dla API -----------------------------------------------------------


async def search(query: str, limit: int) -> dict:
    target = parse_youtube_url(query)
    if target and target[0] == "channel":
        return {"channel": target[1]}
    if target and target[0] == "video":
        audio = await get_audio(target[1])
        return {"items": [audio["entry"]]}

    key = f"{limit}:{query.lower()}"
    items = _search_cache.get(key)
    if items is None:
        items = await _dedupe("search:" + key, lambda: _run(_search_sync, query, limit))
        _search_cache.set(key, items)
    return {"items": items}


async def channel(channel_id: str, page: int, size: int) -> dict:
    if not CHANNEL_RE.match(channel_id):
        raise InvalidInput("Nieprawidłowy identyfikator kanału.")
    key = f"{channel_id}:{page}:{size}"
    result = _channel_cache.get(key)
    if result is None:
        result = await _dedupe("channel:" + key, lambda: _run(_channel_sync, channel_id, page, size))
        _channel_cache.set(key, result)
    return result


async def get_audio(video_id: str, refresh: bool = False) -> dict:
    check_video_id(video_id)
    if refresh:
        _audio_cache.pop(video_id)
    else:
        cached = _audio_cache.get(video_id)
        if cached:
            return cached

    async def load():
        audio = await _run(_audio_sync, video_id)
        ttl = 3600
        if audio["expire"]:
            # Link googlevideo wygasa; odświeżamy go z zapasem.
            ttl = max(60, min(ttl, audio["expire"] - time.time() - 600))
        _audio_cache.set(video_id, audio, ttl)
        return audio

    return await _dedupe("audio:" + video_id, load)


# --- miniatury ------------------------------------------------------------------


async def thumbnail(video_id: str) -> FileResponse:
    check_video_id(video_id)
    path = _thumb_dir / f"{video_id}.jpg"
    if not path.exists():
        try:
            resp = await _http.get(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")
        except httpx.HTTPError:
            raise YouTubeError("Nie udało się pobrać miniatury.") from None
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            raise YouTubeError("Brak miniatury.")
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


# --- strumień audio -------------------------------------------------------------


class _RangeNotSatisfiable(Exception):
    pass


def _parse_range(header: str | None, total: int) -> tuple[int, int]:
    if not header:
        return 0, total - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or (not match[1] and not match[2]):
        raise _RangeNotSatisfiable
    if not match[1]:
        suffix = int(match[2])
        if suffix == 0:
            raise _RangeNotSatisfiable
        start, end = max(0, total - suffix), total - 1
    else:
        start = int(match[1])
        end = min(int(match[2]), total - 1) if match[2] else total - 1
    if start >= total or start > end:
        raise _RangeNotSatisfiable
    return start, end


async def _upstream(audio: dict, start: int, end: int) -> httpx.Response:
    headers = {**audio["headers"], "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
    request = _http.build_request("GET", audio["url"], headers=headers)
    return await _http.send(request, stream=True)


async def _ensure_size(audio: dict) -> int | None:
    """Rozmiar pliku; None oznacza, że link wygasł."""
    if audio.get("filesize"):
        return audio["filesize"]
    resp = await _upstream(audio, 0, 0)
    await resp.aclose()
    if resp.status_code in (403, 404, 410):
        return None
    match = re.search(r"/(\d+)$", resp.headers.get("content-range", ""))
    if resp.status_code != 206 or not match:
        raise YouTubeError(f"Nie można ustalić rozmiaru strumienia (HTTP {resp.status_code}).")
    audio["filesize"] = int(match[1])
    return audio["filesize"]


async def audio_response(video_id: str, range_header: str | None) -> Response:
    check_video_id(video_id)
    try:
        for attempt in range(2):
            audio = await get_audio(video_id, refresh=attempt > 0)
            total = await _ensure_size(audio)
            if total is None:
                continue
            try:
                start, end = _parse_range(range_header, total)
            except _RangeNotSatisfiable:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            first = await _upstream(audio, start, min(end, start + UPSTREAM_CHUNK - 1))
            if first.status_code in (403, 404, 410):
                await first.aclose()
                continue
            if first.status_code != 206:
                await first.aclose()
                raise YouTubeError(f"YouTube zwrócił błąd HTTP {first.status_code}.")
            break
        else:
            raise YouTubeError("Link do strumienia wygasł i nie udało się go odświeżyć.")
    except httpx.HTTPError as exc:
        raise YouTubeError(f"Błąd połączenia z YouTube: {exc.__class__.__name__}") from None

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, no-store",
    }
    status = 200
    if range_header:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    media_type = MIME_TYPES.get(audio.get("ext") or "", "application/octet-stream")
    body = _stream_body(video_id, audio, start, end, first)
    return StreamingResponse(body, status_code=status, media_type=media_type, headers=headers)


async def _stream_body(video_id: str, audio: dict, pos: int, end: int, resp: httpx.Response):
    refreshed = False
    try:
        while True:
            received = 0
            async for chunk in resp.aiter_raw(READ_SIZE):
                chunk = chunk[: end - pos + 1]
                pos += len(chunk)
                received += len(chunk)
                yield chunk
                if pos > end:
                    return
            await resp.aclose()
            if received == 0:
                log.warning("pusta odpowiedź googlevideo dla %s na pozycji %d", video_id, pos)
                return

            resp = await _upstream(audio, pos, min(end, pos + UPSTREAM_CHUNK - 1))
            if resp.status_code in (403, 410) and not refreshed:
                # Link wygasł w trakcie słuchania: pobieramy nowy, jeśli to ten sam plik.
                await resp.aclose()
                refreshed = True
                fresh = await get_audio(video_id, refresh=True)
                if fresh.get("filesize") != audio.get("filesize"):
                    log.warning("odświeżony strumień %s ma inny rozmiar, przerywam", video_id)
                    return
                audio = fresh
                resp = await _upstream(audio, pos, min(end, pos + UPSTREAM_CHUNK - 1))
            if resp.status_code != 206:
                log.warning("googlevideo HTTP %d dla %s na pozycji %d", resp.status_code, video_id, pos)
                return
    except (httpx.HTTPError, YouTubeError) as exc:
        log.warning("przerwany strumień %s: %s", video_id, exc)
    finally:
        await resp.aclose()
