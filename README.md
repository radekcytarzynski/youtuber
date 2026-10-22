# YT Audio

Prywatna przejściówka do słuchania i oglądania YouTube przez własny serwer. Wyszukiwanie, przeglądanie kanałów i odtwarzanie w przeglądarce: samego dźwięku albo wideo w 360p, 720p lub 1080p (wybór w górnym pasku). Cały ruch (lista, miniatury, audio, wideo) idzie przez Twoją domenę, więc przeglądarka nie łączy się z żadnym serwerem Google.

YouTube podaje obraz i dźwięk jako osobne strumienie, więc przy wideo serwer łączy je w locie przez ffmpeg (bez przekodowania, niskie użycie CPU). Taki strumień nie obsługuje przewijania w samym odtwarzaczu, dlatego pod wideo jest osobny suwak pozycji: przesunięcie go uruchamia strumień od nowa od wybranej sekundy (start od najbliższej wcześniejszej klatki kluczowej, czyli zwykle kilka sekund wcześniej).

```
Internet ──TCP 443/80──> [Caddy] ──sieć wewnętrzna──> [app: FastAPI + yt-dlp]
```

- **Caddy**: HTTPS (certyfikat Let's Encrypt), nagłówki bezpieczeństwa.
- **app**: logowanie z blokadą prób, wyszukiwarka, kanały, strumień audio. Nie wystawia żadnego portu na hosta.
- Osobna sieć Dockera i osobne wolumeny: brak dostępu do innych usług na serwerze (Immich, Nextcloud).

## Instalacja

### 1. DNS i router

- W panelu DNS: `yt.example.com` jako CNAME na domenę aktualizowaną przez DDNS (albo rekord A na publiczne IP).
- Na routerze: przekierowanie **TCP 80 i 443** na serwer. Port 80 służy tylko do wydania certyfikatu i przekierowania na HTTPS.

Sprawdzenie, czy DNS już działa:

```bash
dig +short yt.example.com
```

### 2. Pliki na serwerze

Skopiuj cały katalog projektu na serwer, np. do `/opt/yt-proxy`, i wejdź do niego:

```bash
cd /opt/yt-proxy
```

### 3. Konfiguracja

```bash
cp .env.example .env
```

```bash
docker compose build
```

Wygeneruj hash hasła (minimum 12 znaków):

```bash
docker compose run --rm --no-deps app python hash_password.py
```

Wklej wypisaną linię `APP_PASSWORD_HASH='...'` do `.env` w miejsce pustej. **Pojedyncze cudzysłowy są konieczne**, bo hash zawiera znaki `$`.

```bash
chmod 600 .env
```

### 4. Uruchomienie

```bash
docker compose up -d
```

Sprawdź, czy Caddy dostał certyfikat (szukaj `certificate obtained successfully`):

```bash
docker compose logs -f caddy
```

Potem otwórz https://yt.example.com.

## Utrzymanie

### Aktualizacja yt-dlp

YouTube regularnie coś zmienia; gdy odtwarzanie albo wyszukiwanie przestaje działać, najpierw zaktualizuj yt-dlp (przebudowa obrazu pobiera najnowszą wersję):

```bash
docker compose build --pull --no-cache app
```

```bash
docker compose up -d app
```

Warto robić to automatycznie, np. raz w tygodniu z crona (`crontab -e`):

```
0 4 * * 1 cd /opt/yt-proxy && docker compose build --pull --no-cache app && docker compose up -d app
```

### Blokady logowania

Po 5 nieudanych próbach z jednego IP logowanie jest blokowane na 15 min, przy kolejnych seriach na 1 h, 6 h i 24 h. Przy 30 błędach w 10 minut (z dowolnych adresów) logowanie wstrzymuje się dla wszystkich na 15 min. Liczniki są w bazie SQLite, więc restart ich nie zeruje. Wszystkie progi można zmienić w `.env`.

Podgląd blokad i sesji:

```bash
docker compose exec app python admin.py status
```

Jeśli zablokujesz sam siebie:

```bash
docker compose exec app python admin.py unlock
```

Wylogowanie wszystkich urządzeń (np. po zmianie hasła):

```bash
docker compose exec app python admin.py logout-all
```

### Zmiana hasła

Wygeneruj nowy hash (krok 3), podmień go w `.env` i zrestartuj aplikację:

```bash
docker compose up -d app
```

### Logi

```bash
docker compose logs -f app
```

Nieudane logowania mają postać `login_failed ip=...`, a blokady `login_locked ip=...`. Po te wpisy może sięgać fail2ban albo CrowdSec.

## Typowe problemy

- **„YouTube zablokował zapytanie (weryfikacja anty-bot)”**: YouTube uznał zapytania za automatyczne. Zwykle mija samo; pomaga też aktualizacja yt-dlp.
- **Caddy nie dostaje certyfikatu**: sprawdź, czy DNS wskazuje na Twoje IP i czy router przekierowuje port 80.
- **Kolizja podsieci Dockera** (`Pool overlaps with other one`): zmień `172.31.250.0/24` w `docker-compose.yml` i tak samo `TRUSTED_PROXY_NETS` w `.env`.
- **Wideo nie działa w Safari (iPhone, iPad, Mac)**: Safari wymaga, żeby serwer obsługiwał zapytania o fragmenty pliku (Range), a łączony w locie strumień wideo tego nie umożliwia. Tryb „Audio” działa normalnie.
- **„Za dużo równoczesnych strumieni wideo”**: jednocześnie działają najwyżej 3 procesy ffmpeg (`MAX_FFMPEG` w `app/youtube.py`).
- **Ekran logowania po każdym odświeżeniu**: strona musi być otwierana przez `https://`, bo ciasteczko sesji ma flagę `Secure`.
- **Kontener nie rozwiązuje nazw YouTube** (błędy połączenia, `Temporary failure in name resolution`): zwykle winny jest resolver hosta (np. `systemd-resolved` na `127.0.0.53`) albo Pi-hole/AdGuard blokujący `googlevideo.com`. Utwórz obok `docker-compose.yml` plik `docker-compose.override.yml` (Compose wczyta go automatycznie, a Git go ignoruje):

  ```yaml
  services:
    app:
      dns:
        - 1.1.1.1
        - 8.8.8.8
  ```

  Potem `docker compose up -d app`. Nazwy wewnętrzne (np. `app` dla Caddy) nadal działają przez DNS Dockera.
