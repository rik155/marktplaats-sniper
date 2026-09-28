# Marktplaats Sniper

Eigen Marktplaats-monitor met FastAPI, SQLite en Telegram.

## Functies

- meerdere snipers
- minimum- en maximumprijs
- postcode en afstand
- uitsluitwoorden
- alleen advertenties met foto
- automatisch scannen
- handmatig scannen
- dubbele meldingen voorkomen
- Telegram-meldingen
- activiteitsoverzicht
- Docker-ready

## Lokaal starten

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Op Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open daarna:

`http://localhost:8080`

## Telegram instellen

Zet in `.env`:

```env
TELEGRAM_BOT_TOKEN=jouw_bot_token
TELEGRAM_CHAT_ID=jouw_chat_id
```

## Marktplaats

Zet een geldige API-token in:

```env
MARKTPLAATS_ACCESS_TOKEN=
```

De app gebruikt standaard:

`https://api.marktplaats.nl/v2/search`

## Docker

```bash
docker build -t marktplaats-sniper .
docker run -d \
  --name marktplaats-sniper \
  --restart unless-stopped \
  -p 8080:8080 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  marktplaats-sniper
```

## Veiligheid

De echte `.env` staat in `.gitignore`. Zet nooit Telegram-tokens of andere sleutels rechtstreeks in GitHub.
