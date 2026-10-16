"""Konfiguracja aplikacji wczytywana ze zmiennych środowiskowych (.env)."""

import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _fail(message: str) -> None:
    sys.exit(f"Błąd konfiguracji: {message}")


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        _fail(f"{name} musi być liczbą całkowitą (jest: {raw!r})")
    if value < minimum:
        _fail(f"{name} musi być >= {minimum}")
    return value


def _int_list(name: str, default: str) -> tuple[int, ...]:
    raw = os.environ.get(name, default)
    try:
        values = tuple(int(part) for part in raw.split(",") if part.strip())
    except ValueError:
        _fail(f"{name} musi być listą liczb oddzielonych przecinkami")
    if not values or any(v < 1 for v in values):
        _fail(f"{name} musi zawierać co najmniej jedną dodatnią liczbę")
    return values


def _networks(name: str, default: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get(name, default)
    try:
        return tuple(ipaddress.ip_network(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        _fail(f"{name}: {exc}")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    password_hash: str
    session_days: int
    cookie_secure: bool
    trusted_proxy_nets: tuple
    login_max_fails: int
    login_lock_steps: tuple[int, ...]
    global_max_fails: int
    global_window: int
    global_lock: int
    search_limit: int
    channel_page_size: int
    data_dir: Path


def load() -> Settings:
    password_hash = os.environ.get("APP_PASSWORD_HASH", "").strip().strip("'\"")
    if not password_hash.startswith("$argon2"):
        _fail(
            "APP_PASSWORD_HASH jest pusty albo nie jest hashem Argon2. "
            "Wygeneruj go: docker compose run --rm app python hash_password.py"
        )

    return Settings(
        password_hash=password_hash,
        session_days=_int("SESSION_DAYS", 30),
        cookie_secure=_bool("COOKIE_SECURE", True),
        trusted_proxy_nets=_networks("TRUSTED_PROXY_NETS", "172.31.250.0/24"),
        login_max_fails=_int("LOGIN_MAX_FAILS", 5),
        login_lock_steps=_int_list("LOGIN_LOCK_STEPS", "900,3600,21600,86400"),
        global_max_fails=_int("LOGIN_GLOBAL_MAX_FAILS", 30),
        global_window=_int("LOGIN_GLOBAL_WINDOW", 600),
        global_lock=_int("LOGIN_GLOBAL_LOCK", 900),
        search_limit=min(_int("SEARCH_LIMIT", 24), 50),
        channel_page_size=min(_int("CHANNEL_PAGE_SIZE", 30), 100),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
    )


settings = load()
