"""Generuje hash Argon2 hasła do wpisania w .env jako APP_PASSWORD_HASH."""

import getpass
import sys

from argon2 import PasswordHasher

MIN_LENGTH = 12


def main() -> None:
    password = getpass.getpass("Nowe hasło: ")
    if len(password) < MIN_LENGTH:
        sys.exit(f"Hasło musi mieć co najmniej {MIN_LENGTH} znaków.")
    if getpass.getpass("Powtórz hasło: ") != password:
        sys.exit("Hasła się różnią.")
    print("\nWklej tę linię do pliku .env (razem z pojedynczymi cudzysłowami):\n")
    print(f"APP_PASSWORD_HASH='{PasswordHasher().hash(password)}'")


if __name__ == "__main__":
    main()
