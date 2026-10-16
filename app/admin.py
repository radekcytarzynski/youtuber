"""Narzędzia administracyjne. Uruchamiaj w działającym kontenerze:

    docker compose exec app python admin.py status       # blokady i sesje
    docker compose exec app python admin.py unlock       # zdejmij wszystkie blokady logowania
    docker compose exec app python admin.py logout-all   # unieważnij wszystkie sesje
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from db import Database


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def status(db: Database) -> None:
    now = int(time.time())
    rows = db.query("SELECT value FROM meta WHERE key = 'global_locked_until'")
    global_until = int(rows[0]["value"]) if rows else 0
    print("Blokada globalna:", f"do {_fmt(global_until)}" if global_until > now else "brak")

    print("\nAdresy z nieudanymi próbami:")
    rows = db.query("SELECT * FROM login_ip ORDER BY last_fail DESC")
    if not rows:
        print("  brak")
    for row in rows:
        lock = f"zablokowany do {_fmt(row['locked_until'])}" if row["locked_until"] > now else "bez blokady"
        print(f"  {row['ip']:<40} błędy: {row['fails']}  poziom: {row['lock_level']}  {lock}  ostatni błąd: {_fmt(row['last_fail'])}")

    print("\nAktywne sesje:")
    rows = db.query("SELECT * FROM sessions WHERE expires > ? ORDER BY created DESC", (now,))
    if not rows:
        print("  brak")
    for row in rows:
        print(f"  od {_fmt(row['created'])}  do {_fmt(row['expires'])}  {row['ip']}  {row['user_agent'][:60]}")


def unlock(db: Database) -> None:
    db.execute("DELETE FROM login_ip")
    db.execute("DELETE FROM login_fail_log")
    db.execute("DELETE FROM meta WHERE key = 'global_locked_until'")
    print("Zdjęto wszystkie blokady logowania.")


def logout_all(db: Database) -> None:
    count = db.execute("DELETE FROM sessions")
    print(f"Unieważniono sesje: {count}.")


COMMANDS = {"status": status, "unlock": unlock, "logout-all": logout_all}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    db = Database(Path(os.environ.get("DATA_DIR", "/data")) / "app.db")
    COMMANDS[sys.argv[1]](db)


if __name__ == "__main__":
    main()
