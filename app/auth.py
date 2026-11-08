"""Logowanie jednym hasłem, sesje w ciasteczku i blokada wielu nieudanych prób."""

import asyncio
import hashlib
import ipaddress
import logging
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

from config import settings
from db import Database

log = logging.getLogger("auth")

COOKIE_NAME = "ytp_session"
# Po tylu sekundach bez błędów licznik prób i poziom blokady wracają do zera.
FAIL_DECAY = 24 * 3600

_hasher = PasswordHasher()


class LoginLocked(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify_password(password: str) -> bool:
    try:
        return _hasher.verify(settings.password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


class Auth:
    def __init__(self, db: Database):
        self.db = db
        # Próby logowania są obsługiwane po kolei. Dzięki temu równoległa seria
        # zapytań nie prześlizgnie się, zanim licznik zdąży naliczyć blokadę.
        self._login_lock = asyncio.Lock()

    # --- adres klienta -------------------------------------------------------

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else ""
        if self._is_trusted_proxy(peer):
            forwarded = request.headers.get("x-forwarded-for", "")
            # Ostatni wpis dopisał nasz Caddy; wcześniejsze mógł podać sam klient.
            candidate = forwarded.split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return peer

    @staticmethod
    def _is_trusted_proxy(peer: str) -> bool:
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(address in net for net in settings.trusted_proxy_nets)

    # --- logowanie -----------------------------------------------------------

    async def login(self, password: str, ip: str, user_agent: str | None) -> str | None:
        """Zwraca token sesji, None przy złym haśle albo rzuca LoginLocked."""
        async with self._login_lock:
            now = int(time.time())
            remaining = self._lock_remaining(ip, now)
            if remaining:
                log.warning("login_blocked ip=%s retry_after=%d", ip, remaining)
                raise LoginLocked(remaining)

            if not await asyncio.to_thread(_verify_password, password):
                log.warning("login_failed ip=%s", ip)
                retry_after = self._register_failure(ip, now)
                if retry_after:
                    raise LoginLocked(retry_after)
                return None

            self.db.execute("DELETE FROM login_ip WHERE ip = ?", (ip,))
            log.info("login_ok ip=%s", ip)
            return self._create_session(ip, user_agent, now)

    def _lock_remaining(self, ip: str, now: int) -> int:
        rows = self.db.query("SELECT locked_until FROM login_ip WHERE ip = ?", (ip,))
        ip_until = rows[0]["locked_until"] if rows else 0
        rows = self.db.query("SELECT value FROM meta WHERE key = 'global_locked_until'")
        global_until = int(rows[0]["value"]) if rows else 0
        return max(0, ip_until - now, global_until - now)

    def _register_failure(self, ip: str, now: int) -> int:
        """Nalicza błąd; zwraca czas blokady w sekundach (0 = brak blokady)."""
        steps = settings.login_lock_steps
        locked_until = 0
        global_until = 0

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT fails, lock_level, locked_until, last_fail FROM login_ip WHERE ip = ?", (ip,)
            ).fetchone()
            fails, level = 0, 0
            if row and now - max(row["last_fail"], row["locked_until"]) <= FAIL_DECAY:
                fails, level = row["fails"], row["lock_level"]

            fails += 1
            if fails >= settings.login_max_fails:
                locked_until = now + steps[min(level, len(steps) - 1)]
                level += 1
                fails = 0

            conn.execute(
                """
                INSERT INTO login_ip (ip, fails, lock_level, locked_until, last_fail)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET
                    fails = excluded.fails,
                    lock_level = excluded.lock_level,
                    locked_until = excluded.locked_until,
                    last_fail = excluded.last_fail
                """,
                (ip, fails, level, locked_until, now),
            )

            conn.execute("INSERT INTO login_fail_log (ts) VALUES (?)", (now,))
            conn.execute("DELETE FROM login_fail_log WHERE ts < ?", (now - settings.global_window,))
            recent = conn.execute("SELECT COUNT(*) FROM login_fail_log").fetchone()[0]
            if recent >= settings.global_max_fails:
                global_until = now + settings.global_lock
                conn.execute(
                    """
                    INSERT INTO meta (key, value) VALUES ('global_locked_until', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(global_until),),
                )
                conn.execute("DELETE FROM login_fail_log")

        if locked_until:
            log.warning("login_locked ip=%s seconds=%d level=%d", ip, locked_until - now, level)
        if global_until:
            log.warning("login_global_lock seconds=%d", global_until - now)
        until = max(locked_until, global_until)
        return until - now if until else 0

    # --- sesje ---------------------------------------------------------------

    def _create_session(self, ip: str, user_agent: str | None, now: int) -> str:
        token = secrets.token_urlsafe(32)
        expires = now + settings.session_days * 86400
        self.db.execute(
            "INSERT INTO sessions (token_hash, created, expires, ip, user_agent) VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), now, expires, ip, (user_agent or "")[:300]),
        )
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        return token

    def session_valid(self, token: str | None) -> bool:
        if not token or len(token) > 100:
            return False
        rows = self.db.query("SELECT expires FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        return bool(rows) and rows[0]["expires"] > time.time()

    def logout(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
