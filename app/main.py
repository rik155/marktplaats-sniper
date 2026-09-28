import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / os.getenv("DATABASE_PATH", "sniper.db")
SCAN_INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
API_BASE = os.getenv("MARKTPLAATS_API_BASE", "https://api.marktplaats.nl").rstrip("/")
PUBLIC_BASE = "https://www.marktplaats.nl"
ACCESS_TOKEN = os.getenv("MARKTPLAATS_ACCESS_TOKEN", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS snipers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            query TEXT NOT NULL,
            min_price INTEGER,
            max_price INTEGER,
            postcode TEXT,
            distance_km INTEGER,
            exclude_words TEXT DEFAULT '',
            only_with_images INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_ads (
            sniper_id INTEGER NOT NULL,
            ad_id TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            PRIMARY KEY (sniper_id, ad_id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sniper_id INTEGER,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


def log_event(sniper_id, level, message):
    with db() as conn:
        conn.execute(
            "INSERT INTO events(sniper_id,level,message,created_at) VALUES(?,?,?,?)",
            (sniper_id, level, message[:600], datetime.now(timezone.utc).isoformat()),
        )


def extract_results(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "advertisements", "searchResults"):
        if isinstance(payload.get(key), list):
            return payload[key]
    embedded = payload.get("_embedded")
    if isinstance(embedded, dict):
        for value in embedded.values():
            if isinstance(value, list):
                return value
    return []


def normalize_ad(raw):
    ad_id = str(
        raw.get("itemId")
        or raw.get("id")
        or raw.get("advertisementId")
        or raw.get("mp:advertisementId")
        or ""
    )
    title = str(raw.get("title") or raw.get("description") or "Marktplaats advertentie").strip()
    price = raw.get("price")
    if price is None and isinstance(raw.get("priceInfo"), dict):
        cents = raw["priceInfo"].get("priceCents")
        price = cents / 100 if isinstance(cents, (int, float)) else None
    if isinstance(price, dict):
        price = price.get("amount") or price.get("cents")
        if isinstance(price, (int, float)) and price > 10000:
            price = price / 100

    location = raw.get("location") or raw.get("sellerLocation") or ""
    if isinstance(location, dict):
        location = (
            location.get("cityName")
            or location.get("city")
            or location.get("displayName")
            or ""
        )

    url = raw.get("url") or raw.get("vipUrl") or raw.get("link") or ""
    if isinstance(url, dict):
        url = url.get("href") or ""
    if url:
        url = urljoin(PUBLIC_BASE, url)
    elif ad_id:
        url = f"https://www.marktplaats.nl/q/{quote_plus(title)}/"

    return {
        "id": ad_id,
        "title": title,
        "price": price,
        "location": location,
        "url": url,
        "has_images": bool(raw.get("imageUrls") or raw.get("pictures") or raw.get("images")),
        "distance_meters": (
            raw.get("location", {}).get("distanceMeters")
            if isinstance(raw.get("location"), dict)
            else None
        ),
    }


async def search_official_api(sniper):
    params = {"query": sniper["query"], "offset": 0, "limit": 30}
    if sniper["only_with_images"]:
        params["withImages"] = "true"
    if sniper["min_price"] is not None:
        params["filters.price.from"] = sniper["min_price"]
    if sniper["max_price"] is not None:
        params["filters.price.to"] = sniper["max_price"]
    if sniper["postcode"]:
        params["filters.postCode"] = sniper["postcode"].replace(" ", "").upper()
    if sniper["distance_km"]:
        params["filters.distance"] = sniper["distance_km"] * 1000

    headers = {"Accept": "application/json", "Authorization": f"Bearer {ACCESS_TOKEN}"}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(f"{API_BASE}/v2/search", params=params, headers=headers)
        response.raise_for_status()
        return [normalize_ad(x) for x in extract_results(response.json())]


def extract_public_listings(html):
    marker = '<script id="__NEXT_DATA__"'
    start = html.find(marker)
    if start < 0:
        raise ValueError("Marktplaats-resultaten konden niet worden gelezen")
    start = html.find(">", start) + 1
    end = html.find("</script>", start)
    if start == 0 or end < 0:
        raise ValueError("Marktplaats-resultaten zijn onvolledig")

    payload = json.loads(html[start:end])
    return (
        payload.get("props", {})
        .get("pageProps", {})
        .get("searchRequestAndResponse", {})
        .get("listings", [])
    )


async def search_public_page(sniper):
    params = {"sortBy": "SORT_INDEX", "sortOrder": "DECREASING"}
    if sniper["min_price"] is not None:
        params["Pricefrom"] = sniper["min_price"]
    if sniper["max_price"] is not None:
        params["Priceto"] = sniper["max_price"]
    if sniper["postcode"]:
        params["postcode"] = sniper["postcode"].replace(" ", "").upper()
    if sniper["distance_km"]:
        params["distanceMeters"] = sniper["distance_km"] * 1000

    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "nl-NL,nl;q=0.9",
        "User-Agent": "Mozilla/5.0 (compatible; MarktplaatsSniper/1.0; persoonlijk gebruik)",
    }
    url = f"{PUBLIC_BASE}/q/{quote_plus(sniper['query'])}/"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        response = await client.get(url, params=params, headers=headers)
        if response.status_code == 429:
            raise RuntimeError("Marktplaats vraagt om rustiger te scannen; probeer later opnieuw")
        response.raise_for_status()
        ads = [normalize_ad(x) for x in extract_public_listings(response.text)]

    filtered = []
    for ad in ads:
        price = ad["price"]
        if sniper["only_with_images"] and not ad["has_images"]:
            continue
        if sniper["min_price"] is not None and isinstance(price, (int, float)) and price < sniper["min_price"]:
            continue
        if sniper["max_price"] is not None and isinstance(price, (int, float)) and price > sniper["max_price"]:
            continue
        distance = ad["distance_meters"]
        if sniper["distance_km"] and isinstance(distance, (int, float)) and distance >= 0:
            if distance > sniper["distance_km"] * 1000:
                continue
        filtered.append(ad)
    return filtered


async def search_marktplaats(sniper):
    if ACCESS_TOKEN:
        try:
            return await search_official_api(sniper)
        except (httpx.HTTPError, ValueError):
            log_event(sniper["id"], "info", "API niet beschikbaar; openbare zoekpagina gebruikt")
    return await search_public_page(sniper)


async def send_telegram(ad, sniper):
    if not BOT_TOKEN or not CHAT_ID:
        return False

    price = ad["price"]
    if isinstance(price, (int, float)):
        price_text = f"€{price:,.0f}".replace(",", ".")
    else:
        price_text = "Prijs onbekend"

    text = (
        "🔥 Nieuwe Marktplaats-match\n\n"
        f"🎯 {sniper['name']}\n"
        f"📦 {ad['title']}\n"
        f"💰 {price_text}\n"
        f"📍 {ad['location'] or 'Locatie onbekend'}\n\n"
        f"🔗 {ad['url']}"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        response.raise_for_status()

    return True


async def scan_sniper(sniper):
    try:
        ads = await search_marktplaats(sniper)
        excluded = [
            x.strip().lower()
            for x in (sniper["exclude_words"] or "").split(",")
            if x.strip()
        ]

        fresh = []
        with db() as conn:
            for ad in ads:
                if not ad["id"]:
                    continue
                if any(word in ad["title"].lower() for word in excluded):
                    continue

                exists = conn.execute(
                    "SELECT 1 FROM seen_ads WHERE sniper_id=? AND ad_id=?",
                    (sniper["id"], ad["id"]),
                ).fetchone()

                if exists:
                    continue

                conn.execute(
                    "INSERT INTO seen_ads(sniper_id,ad_id,first_seen) VALUES(?,?,?)",
                    (sniper["id"], ad["id"], datetime.now(timezone.utc).isoformat()),
                )
                fresh.append(ad)

        for ad in fresh[:10]:
            sent = await send_telegram(ad, sniper)
            log_event(
                sniper["id"],
                "match",
                f"{ad['title']} - Telegram: {'verstuurd' if sent else 'niet ingesteld'}",
            )

        if fresh:
            log_event(sniper["id"], "info", f"{len(fresh)} nieuwe advertentie(s) gevonden")

    except Exception as exc:
        log_event(sniper["id"], "error", f"Scanfout: {type(exc).__name__}: {exc}")


async def scan_all():
    with db() as conn:
        snipers = conn.execute(
            "SELECT * FROM snipers WHERE enabled=1 ORDER BY id"
        ).fetchall()

    for sniper in snipers:
        await scan_sniper(sniper)


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(
        scan_all,
        "interval",
        seconds=SCAN_INTERVAL,
        max_instances=1,
        coalesce=True,
        id="scanner",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Marktplaats Sniper", lifespan=lifespan)


def page(snipers, events):
    rows = "".join(
        f"""
        <tr>
          <td>{s['name']}</td>
          <td>{s['query']}</td>
          <td>{'€' + str(s['max_price']) if s['max_price'] is not None else '-'}</td>
          <td>{s['postcode'] or '-'}</td>
          <td>{str(s['distance_km']) + ' km' if s['distance_km'] else '-'}</td>
          <td><span class="badge {'on' if s['enabled'] else 'off'}">{'Actief' if s['enabled'] else 'Uit'}</span></td>
          <td class="actions">
            <form method="post" action="/snipers/{s['id']}/scan"><button>Scan nu</button></form>
            <form method="post" action="/snipers/{s['id']}/toggle"><button>{'Pauze' if s['enabled'] else 'Start'}</button></form>
            <form method="post" action="/snipers/{s['id']}/delete" onsubmit="return confirm('Sniper verwijderen?')"><button class="danger">Verwijder</button></form>
          </td>
        </tr>
        """
        for s in snipers
    )

    activity = "".join(
        f"<li><b>{e['level'].upper()}</b> · {e['sniper_name'] or 'Systeem'} · {e['message']}</li>"
        for e in events
    ) or "<li>Nog geen activiteit.</li>"

    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marktplaats Sniper</title>
<style>
body{{font-family:Arial,sans-serif;background:#f5f7fb;margin:0;color:#172033}}
.wrap{{max-width:1200px;margin:0 auto;padding:28px}}
h1{{margin:0 0 6px}}
.sub{{color:#65708a;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.card{{background:white;border-radius:18px;padding:20px;box-shadow:0 8px 30px rgba(0,0,0,.06)}}
label{{display:block;font-size:13px;font-weight:700;margin:12px 0 6px}}
input{{width:100%;box-sizing:border-box;padding:12px;border:1px solid #d9dfeb;border-radius:10px}}
button{{border:0;background:#172033;color:white;padding:10px 13px;border-radius:9px;cursor:pointer}}
.danger{{background:#c43d3d}}
.badge{{padding:6px 9px;border-radius:999px;font-size:12px;font-weight:700}}
.on{{background:#d9f7e7;color:#137a46}} .off{{background:#eceff5;color:#667085}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:12px 8px;border-bottom:1px solid #edf0f5;text-align:left;font-size:14px}}
.actions{{display:flex;gap:6px;flex-wrap:wrap}}
.actions form{{margin:0}}
ul{{padding-left:20px}}
@media(max-width:850px){{.grid{{grid-template-columns:1fr}} table{{display:block;overflow-x:auto}}}}
</style>
</head>
<body>
<div class="wrap">
  <h1>🎯 Marktplaats Sniper</h1>
  <div class="sub">Automatisch nieuwe advertenties zoeken en melden via Telegram.</div>

  <div class="grid">
    <div class="card">
      <h2>Nieuwe sniper</h2>
      <form method="post" action="/snipers">
        <label>Naam</label><input name="name" placeholder="Bijv. Makita koopjes" required>
        <label>Zoekwoord</label><input name="query" placeholder="Bijv. Makita DHP484" required>
        <label>Minimumprijs</label><input name="min_price" type="number" min="0">
        <label>Maximumprijs</label><input name="max_price" type="number" min="0">
        <label>Postcode</label><input name="postcode" placeholder="6541AA">
        <label>Afstand (km)</label><input name="distance_km" type="number" min="1">
        <label>Uitsluitwoorden, komma-gescheiden</label><input name="exclude_words" placeholder="defect, gezocht, onderdelen">
        <label><input type="checkbox" name="only_with_images" value="true" checked style="width:auto"> Alleen met foto</label>
        <br><button type="submit">Sniper toevoegen</button>
      </form>
    </div>

    <div class="card">
      <h2>Status</h2>
      <p>Scaninterval: <b>{SCAN_INTERVAL} seconden</b></p>
      <p>Telegram: <b>{'ingesteld' if BOT_TOKEN and CHAT_ID else 'nog niet ingesteld'}</b></p>
      <p>Gegevensbron: <b>{'officiële API met openbare zoekpagina als reserve' if ACCESS_TOKEN else 'openbare Marktplaats-zoekpagina'}</b></p>
      <p>Voor zoeken is geen Marktplaats-token nodig.</p>
    </div>
  </div>

  <div class="card" style="margin-top:20px">
    <h2>Snipers</h2>
    <table>
      <thead><tr><th>Naam</th><th>Zoekopdracht</th><th>Max</th><th>Postcode</th><th>Afstand</th><th>Status</th><th>Acties</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="7">Nog geen snipers toegevoegd.</td></tr>'}</tbody>
    </table>
  </div>

  <div class="card" style="margin-top:20px">
    <h2>Activiteit</h2>
    <ul>{activity}</ul>
  </div>
</div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    with db() as conn:
        snipers = conn.execute("SELECT * FROM snipers ORDER BY id DESC").fetchall()
        events = conn.execute(
            "SELECT e.*, s.name sniper_name FROM events e LEFT JOIN snipers s ON s.id=e.sniper_id ORDER BY e.id DESC LIMIT 30"
        ).fetchall()
    return HTMLResponse(page(snipers, events))


@app.post("/snipers")
def add_sniper(
    name: str = Form(...),
    query: str = Form(...),
    min_price: str = Form(""),
    max_price: str = Form(""),
    postcode: str = Form(""),
    distance_km: str = Form(""),
    exclude_words: str = Form(""),
    only_with_images: bool = Form(False),
):
    with db() as conn:
        conn.execute(
            """INSERT INTO snipers(
                name,query,min_price,max_price,postcode,distance_km,
                exclude_words,only_with_images,enabled,created_at
            ) VALUES(?,?,?,?,?,?,?,?,1,?)""",
            (
                name.strip(),
                query.strip(),
                int(min_price) if min_price else None,
                int(max_price) if max_price else None,
                postcode.strip(),
                int(distance_km) if distance_km else None,
                exclude_words.strip(),
                1 if only_with_images else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return RedirectResponse("/", status_code=303)


@app.post("/snipers/{sniper_id}/toggle")
def toggle(sniper_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT enabled FROM snipers WHERE id=?", (sniper_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE snipers SET enabled=? WHERE id=?",
                (0 if row["enabled"] else 1, sniper_id),
            )
    return RedirectResponse("/", status_code=303)


@app.post("/snipers/{sniper_id}/delete")
def delete(sniper_id: int):
    with db() as conn:
        conn.execute("DELETE FROM seen_ads WHERE sniper_id=?", (sniper_id,))
        conn.execute("DELETE FROM events WHERE sniper_id=?", (sniper_id,))
        conn.execute("DELETE FROM snipers WHERE id=?", (sniper_id,))
    return RedirectResponse("/", status_code=303)


@app.post("/snipers/{sniper_id}/scan")
async def scan_now(sniper_id: int):
    with db() as conn:
        sniper = conn.execute("SELECT * FROM snipers WHERE id=?", (sniper_id,)).fetchone()
    if sniper:
        await scan_sniper(sniper)
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return {
        "ok": True,
        "scan_interval_seconds": SCAN_INTERVAL,
        "telegram_configured": bool(BOT_TOKEN and CHAT_ID),
    }
