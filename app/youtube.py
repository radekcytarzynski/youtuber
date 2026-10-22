"""Dostęp do YouTube przez yt-dlp: wyszukiwanie, kanały, miniatury, strumień audio i wideo."""

import asyncio
import logging
import re
import secrets
import shutil
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

AUDIO_MIME = {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm", "opus": "audio/ogg"}
VIDEO_MIME = {"mp4": "video/mp4", "webm": "video/webm"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _merged_format(height: int) -> str:
    video = f"bestvideo[height<={height}][protocol=https]"
    m4a = "bestaudio[ext=m4a][protocol=https]"
    # Najpierw H.264 (odtworzy go każda przeglądarka), potem dowolny kodek obrazu.
    return f"{video}[vcodec^=avc1]+{m4a}/{video}+{m4a}/{video}+bestaudio[protocol=https]"


# "audio" to pojedynczy plik (przewijanie przez nagłówek Range). Wideo to osobne
# strumienie obrazu i dźwięku łączone w locie przez ffmpeg; YouTube nie udostępnia
# już plików z obrazem i dźwiękiem razem (format 18).
FORMATS = {
    "audio": "bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]",
    "360": _merged_format(360),
    "720": _merged_format(720),
    "1080": _merged_format(1080),
}
MERGED = ("360", "720", "1080")

# ffmpeg pobiera strumienie przez tę samą aplikację (endpoint /internal), żeby
# korzystać z pobierania w kawałkach i odświeżania wygasłych linków.
INTERNAL_BASE = "http://127.0.0.1:8000"
INTERNAL_SECRET = secrets.token_urlsafe(24)
MAX_FFMPEG = 3
RW_TIMEOUT = "30000000"  # mikrosekundy


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
_ffmpeg_running = 0

_stream_cache = _TTLCache(ttl=3600, maxsize=300)
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


def _stream_info(fmt: dict) -> dict:
    url = fmt.get("url")
    if not url:
        raise YouTubeError("Nie znaleziono strumienia dla tego filmu.")
    return {
        "url": url,
        "headers": dict(fmt.get("http_headers") or {}),
        "ext": fmt.get("ext"),
        "filesize": fmt.get("filesize") or _url_param(url, "clen"),
        "expire": _url_param(url, "expire"),
    }


def _streams_sync(video_id: str, quality: str) -> dict:
    opts = {**_base_opts(), "format": FORMATS[quality]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(WATCH_URL + video_id, download=False)

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise YouTubeError("Transmisje na żywo i zaplanowane premiery nie są obsługiwane.")
    # Przy łączonych formatach (obraz+dźwięk) yt-dlp zwraca je w requested_formats.
    formats = info.get("requested_formats") or [info]
    return {"streams": [_stream_info(fmt) for fmt in formats], "entry": _entry(info)}


# --- operacje dla API -----------------------------------------------------------


async def search(query: str, limit: int) -> dict:
    target = parse_youtube_url(query)
    if target and target[0] == "channel":
        return {"channel": target[1]}
    if target and target[0] == "video":
        data = await get_streams(target[1], "audio")
        return {"items": [data["entry"]]}

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


def check_quality(quality: str) -> None:
    if quality not in FORMATS:
        raise InvalidInput("Nieprawidłowa jakość.")


async def get_streams(video_id: str, quality: str, refresh: bool = False) -> dict:
    """Linki googlevideo dla jakości: {"streams": [...], "entry": {...}}."""
    check_video_id(video_id)
    check_quality(quality)
    key = f"{quality}:{video_id}"
    if refresh:
        _stream_cache.pop(key)
    else:
        cached = _stream_cache.get(key)
        if cached:
            return cached

    async def load():
        data = await _run(_streams_sync, video_id, quality)
        ttl = 3600
        expires = [s["expire"] for s in data["streams"] if s["expire"]]
        if expires:
            # Link googlevideo wygasa; odświeżamy go z zapasem.
            ttl = max(60, min(ttl, min(expires) - time.time() - 600))
        _stream_cache.set(key, data, ttl)
        return data

    return await _dedupe("streams:" + key, load)


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


# --- strumień z googlevideo -----------------------------------------------------


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


async def _upstream(stream: dict, start: int, end: int) -> httpx.Response:
    headers = {**stream["headers"], "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
    request = _http.build_request("GET", stream["url"], headers=headers)
    return await _http.send(request, stream=True)


async def _ensure_size(stream: dict) -> int | None:
    """Rozmiar pliku; None oznacza, że link wygasł."""
    if stream.get("filesize"):
        return stream["filesize"]
    resp = await _upstream(stream, 0, 0)
    await resp.aclose()
    if resp.status_code in (403, 404, 410):
        return None
    match = re.search(r"/(\d+)$", resp.headers.get("content-range", ""))
    if resp.status_code != 206 or not match:
        raise YouTubeError(f"Nie można ustalić rozmiaru strumienia (HTTP {resp.status_code}).")
    stream["filesize"] = int(match[1])
    return stream["filesize"]


async def _get_stream(video_id: str, quality: str, index: int, refresh: bool = False) -> dict:
    streams = (await get_streams(video_id, quality, refresh))["streams"]
    if not 0 <= index < len(streams):
        raise InvalidInput("Nieprawidłowy numer strumienia.")
    return streams[index]


async def stream_response(video_id: str, quality: str, index: int, range_header: str | None) -> Response:
    """Pojedynczy plik z googlevideo z obsługą Range (audio albo obraz/dźwięk wideo dla ffmpeg)."""
    check_video_id(video_id)
    check_quality(quality)
    try:
        for attempt in range(2):
            stream = await _get_stream(video_id, quality, index, refresh=attempt > 0)
            total = await _ensure_size(stream)
            if total is None:
                continue
            try:
                start, end = _parse_range(range_header, total)
            except _RangeNotSatisfiable:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            first = await _upstream(stream, start, min(end, start + UPSTREAM_CHUNK - 1))
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
    mime_types = AUDIO_MIME if quality == "audio" else VIDEO_MIME
    media_type = mime_types.get(stream.get("ext") or "", "application/octet-stream")
    body = _stream_body(video_id, quality, index, stream, start, end, first)
    return StreamingResponse(body, status_code=status, media_type=media_type, headers=headers)


async def _stream_body(video_id: str, quality: str, index: int, stream: dict, pos: int, end: int, resp: httpx.Response):
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

            resp = await _upstream(stream, pos, min(end, pos + UPSTREAM_CHUNK - 1))
            if resp.status_code in (403, 410) and not refreshed:
                # Link wygasł w trakcie słuchania: pobieramy nowy, jeśli to ten sam plik.
                await resp.aclose()
                refreshed = True
                fresh = await _get_stream(video_id, quality, index, refresh=True)
                if fresh.get("filesize") != stream.get("filesize"):
                    log.warning("odświeżony strumień %s ma inny rozmiar, przerywam", video_id)
                    return
                stream = fresh
                resp = await _upstream(stream, pos, min(end, pos + UPSTREAM_CHUNK - 1))
            if resp.status_code != 206:
                log.warning("googlevideo HTTP %d dla %s na pozycji %d", resp.status_code, video_id, pos)
                return
    except (httpx.HTTPError, YouTubeError) as exc:
        log.warning("przerwany strumień %s: %s", video_id, exc)
    finally:
        await resp.aclose()


# --- wideo: łączenie obrazu i dźwięku przez ffmpeg ------------------------------


async def merged_response(video_id: str, quality: str, start: int) -> StreamingResponse:
    """Fragmentowany MP4 od sekundy `start`; bez Range, przewijanie = nowe zapytanie z innym `start`."""
    check_video_id(video_id)
    if quality not in MERGED:
        raise InvalidInput("Nieprawidłowa jakość.")
    if not shutil.which("ffmpeg"):
        raise YouTubeError("Brak ffmpeg na serwerze, wideo jest niedostępne.")
    if _ffmpeg_running >= MAX_FFMPEG:
        raise YouTubeError("Za dużo równoczesnych strumieni wideo. Spróbuj za chwilę.")
    # Błędy YouTube (film prywatny, anty-bot) zgłaszamy jako JSON, zanim ruszy strumień.
    data = await get_streams(video_id, quality)
    if len(data["streams"]) != 2:
        raise YouTubeError("YouTube nie zwrócił osobnych strumieni obrazu i dźwięku.")

    video_url, audio_url = (f"{INTERNAL_BASE}/internal/{INTERNAL_SECRET}/{quality}/{i}/{video_id}" for i in range(2))
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start:
        # Obraz (bez przekodowania) może zacząć się tylko od klatki kluczowej, zwykle kilka
        # sekund przed `start`. Fragmentowany MP4 zeruje czas każdej ścieżki osobno, więc
        # dźwięk musi zacząć się dokładnie tam, gdzie obraz, inaczej się rozjadą.
        audio_start = await _keyframe_dts(video_url, start)
        if audio_start is None:
            log.warning("nie ustalono klatki kluczowej dla %s od %d s, możliwy rozjazd", video_id, start)
            audio_start = str(start)
        args += ["-copyts", "-ss", str(start), "-rw_timeout", RW_TIMEOUT, "-i", video_url]
        args += ["-ss", audio_start, "-rw_timeout", RW_TIMEOUT, "-i", audio_url]
        args += ["-avoid_negative_ts", "disabled"]
    else:
        args += ["-rw_timeout", RW_TIMEOUT, "-i", video_url, "-rw_timeout", RW_TIMEOUT, "-i", audio_url]
    args += [
        # Dźwięk kodujemy na nowo, bo tylko wtedy ffmpeg przewija go z dokładnością do ramki.
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]
    return StreamingResponse(
        _ffmpeg_body(video_id, args), media_type="video/mp4", headers={"Cache-Control": "private, no-store"}
    )


async def _keyframe_dts(video_url: str, start: int) -> str | None:
    """Czas (DTS) klatki kluczowej, od której ffmpeg zacznie obraz przy `-ss start`."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-rw_timeout", RW_TIMEOUT,
        "-select_streams", "v:0", "-read_intervals", f"{start}%+#1",
        "-show_entries", "packet=dts_time", "-of", "csv=p=0", video_url,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    values = out.decode().split()
    if values and re.fullmatch(r"\d+(\.\d+)?", values[0]):
        return values[0]
    return None


async def _ffmpeg_body(video_id: str, args: list[str]):
    global _ffmpeg_running
    _ffmpeg_running += 1
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr = asyncio.ensure_future(proc.stderr.read())
        while chunk := await proc.stdout.read(READ_SIZE):
            yield chunk
        if await proc.wait():
            message = (await stderr).decode(errors="replace").strip()
            log.warning("ffmpeg %s zakończył się kodem %d: %s", video_id, proc.returncode, message[-500:])
    finally:
        _ffmpeg_running -= 1
        # Klient się rozłączył (przewinięcie, zmiana filmu): zatrzymujemy ffmpeg.
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
