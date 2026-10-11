#!/usr/bin/env python3
"""
Alerte annonces : portière arrière gauche BMW E12.

Surveille eBay (API officielle), LeBonCoin (optionnel, depuis une IP résidentielle)
et n'importe quel site d'annonces via une URL de recherche (sources.json),
filtre les annonces pertinentes et envoie une notification (Telegram, SMS Free
Mobile, SMS/WhatsApp Twilio) pour chaque NOUVELLE annonce.

Usage rapide :
    python bot.py --test            # envoie un message de test
    python bot.py --dry-run --debug # lit les sources, affiche, n'envoie rien
    python bot.py                   # exécution normale (à lancer via cron / GitHub Actions)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:  # optionnel : empreinte TLS de navigateur (utile surtout pour leboncoin, en local)
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("e12")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    """Charge un fichier .env minimal (sans dépendance). Les vraies variables d'environnement priment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(BASE_DIR / ".env")

STATE_FILE = Path(os.getenv("STATE_FILE", str(BASE_DIR / "seen.json")))
SOURCES = [s.strip() for s in os.getenv("SOURCES", "ebay,html").split(",") if s.strip()]
FAIL_ALERT_AFTER = int(os.getenv("FAIL_ALERT_AFTER", "6"))  # alerte si une source échoue N fois de suite
SEEN_TTL_DAYS = 180

# Requêtes de recherche. Une recherche large + le filtre local ci-dessous = on ne rate rien.
EBAY_MARKETPLACES = [m.strip() for m in os.getenv("EBAY_MARKETPLACES", "EBAY_FR,EBAY_DE").split(",") if m.strip()]
EBAY_QUERIES = {
    "EBAY_FR": ["BMW E12 portière arrière", "BMW E12 porte arrière gauche", "BMW 520 525 E12 porte"],
    "EBAY_DE": ["BMW E12 Tür hinten links", "BMW 5er E12 Tür hinten", "BMW E12 Hintertür"],
}
EBAY_DELIVERY_COUNTRY = os.getenv("EBAY_DELIVERY_COUNTRY", "FR")  # ne garde que ce qui livre dans ce pays
LBC_QUERIES = ["BMW E12 portière", "BMW E12 porte arrière", "BMW 520 porte arrière gauche"]


# --------------------------------------------------------------------------- #
# Filtre : est-ce bien une portière de BMW E12 ?
# --------------------------------------------------------------------------- #
E12_RE = re.compile(r"(?<![a-z0-9])e[\s\-]?12(?![a-z0-9])")
OTHER_E_RE = re.compile(r"(?<![a-z0-9])e[\s\-]?(\d{2})(?![a-z0-9])")
BMW_MODEL_RE = re.compile(r"(?<![\d])(?:518|520|525|528|530|533|535)(?:i|a)?(?!\d)|\bm535")
DOOR_RE = re.compile(
    r"(?<![a-z])(?:porti[eè]res?|portes?|doors?|\w*t[üu]r|\w*tuer|\w*t[üu]ren|tur)(?![a-z])"
)
REAR_RE = re.compile(r"(?<![a-z])(?:arri[eè]re|arr|ar|rear|hinten|hinter\w*|rr|rh)(?![a-z])")
LEFT_RE = re.compile(r"(?<![a-z])(?:gauche|left|links|lh|lhs|ag)(?![a-z])")
FRONT_RE = re.compile(r"(?<![a-z])(?:avant|av|front|vorne|vorn|vorder\w*)(?![a-z])")
NEGATIVE_RE = re.compile(
    r"miniature|maquette|modellauto|modell\b|\b1[/:]\d{2}\b|jouet|\bwanted\b|\brecherche\b|\bcherche\b|\bsuche\b|\bachat\b"
)


def classify(text: str) -> tuple[int, str] | None:
    """Retourne (score, étiquette) si l'annonce est pertinente, sinon None.

    Volontairement tolérant : une annonce « porte BMW E12 » sans précision de côté est
    gardée (score 0), pour ne rien rater. Seules les portières explicitement AVANT sont écartées.
    """
    t = text.lower()
    if NEGATIVE_RE.search(t) or not DOOR_RE.search(t):
        return None

    has_e12 = bool(E12_RE.search(t))
    if not has_e12:
        other_models = {m for m in OTHER_E_RE.findall(t) if m != "12"}
        if other_models:  # ex. E28, E34 : autre génération
            return None
        if not ("bmw" in t and BMW_MODEL_RE.search(t)):
            return None

    rear, left, front = (bool(r.search(t)) for r in (REAR_RE, LEFT_RE, FRONT_RE))
    if front and not rear:
        return None

    if rear and left:
        score, label = 3, "✅ arrière gauche"
    elif rear:
        score, label = 2, "🟡 arrière (côté à vérifier)"
    elif left:
        score, label = 1, "🟠 gauche (avant/arrière à vérifier)"
    else:
        score, label = 0, "⚪ porte (position non précisée)"
    if not has_e12:
        label += " · E12 non confirmé"
    return score, label


# --------------------------------------------------------------------------- #
# Modèle de données et utilitaires
# --------------------------------------------------------------------------- #
@dataclass
class Listing:
    uid: str
    source: str
    title: str
    price: str
    location: str
    url: str
    match_text: str = ""
    price_num: float | None = None


class SourceSkipped(Exception):
    """Source non configurée : ignorée sans compter comme un échec."""


class BlockedError(Exception):
    """Le site a répondu par un blocage (403/429/captcha)."""


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_price(value, currency: str = "EUR") -> str:
    v = to_float(value)
    if v is None:
        return "prix n/c"
    s = f"{v:.2f}".replace(".", ",")
    if s.endswith(",00"):
        s = s[:-3]
    return f"{s} €" if currency == "EUR" else f"{s} {currency}"


def scrub(message: str) -> str:
    """Masque tokens et mots de passe dans les messages d'erreur (logs publics GitHub, etc.)."""
    message = re.sub(r"bot\d+:[\w-]+", "bot***", message)
    return re.sub(r"(?i)(pass|token|key|secret)=[^&\s]+", r"\1=***", message)


# --------------------------------------------------------------------------- #
# HTTP « propre » : User-Agent réaliste, timeouts, pauses aléatoires, 1 retry
# --------------------------------------------------------------------------- #
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def polite_sleep(low: float = 1.5, high: float = 4.0) -> None:
    time.sleep(random.uniform(low, high))


def http_get(url: str, params: dict | None = None, impersonate: bool = False):
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            if impersonate and cffi_requests is not None:
                r = cffi_requests.get(
                    url, params=params, impersonate="chrome", timeout=30,
                    headers={"Accept-Language": HEADERS["Accept-Language"]},
                )
            else:
                r = SESSION.get(url, params=params, timeout=30)
            if r.status_code in (403, 429):
                raise BlockedError(f"HTTP {r.status_code} sur {urlparse(url).netloc} (blocage anti-bot)")
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except BlockedError:
            raise  # inutile d'insister : on ne martèle pas un site qui nous bloque
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(3 + attempt * 5)
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Source 1 : eBay (API officielle Browse, gratuite)
# --------------------------------------------------------------------------- #
def ebay_token() -> str:
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def parse_ebay_item(item: dict, marketplace: str) -> Listing:
    price = item.get("price") or {}
    loc = item.get("itemLocation") or {}
    place = " ".join(x for x in (loc.get("city"), loc.get("postalCode")) if x) or "n/c"
    if loc.get("country"):
        place += f" ({loc['country']})"
    return Listing(
        uid=f"ebay:{item['itemId']}",
        source=f"eBay {marketplace.replace('EBAY_', '')}",
        title=item.get("title", ""),
        price=fmt_price(price.get("value"), price.get("currency", "EUR")),
        location=place,
        url=item.get("itemWebUrl", ""),
        price_num=to_float(price.get("value")),
    )


def fetch_ebay() -> list[Listing]:
    if not (os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET")):
        raise SourceSkipped("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET absents")
    token = ebay_token()
    out: dict[str, Listing] = {}
    for marketplace in EBAY_MARKETPLACES:
        for query in EBAY_QUERIES.get(marketplace, EBAY_QUERIES["EBAY_FR"]):
            r = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                params={
                    "q": query,
                    "sort": "newlyListed",
                    "limit": 50,
                    "filter": f"deliveryCountry:{EBAY_DELIVERY_COUNTRY}",
                },
                headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace},
                timeout=30,
            )
            r.raise_for_status()
            for item in r.json().get("itemSummaries", []):
                listing = parse_ebay_item(item, marketplace)
                out[listing.uid] = listing
            polite_sleep(0.5, 1.5)
    return list(out.values())


# --------------------------------------------------------------------------- #
# Source 2 : LeBonCoin (pas d'API publique ; à lancer depuis une IP résidentielle)
# --------------------------------------------------------------------------- #
def find_ads(obj):
    """Cherche récursivement la liste d'annonces dans le JSON __NEXT_DATA__."""
    if isinstance(obj, dict):
        ads = obj.get("ads")
        if isinstance(ads, list) and ads and isinstance(ads[0], dict) and "subject" in ads[0]:
            return ads
        for value in obj.values():
            found = find_ads(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_ads(value)
            if found:
                return found
    return None


def parse_lbc_ad(ad: dict) -> Listing:
    loc = ad.get("location") or {}
    city = loc.get("city_label") or loc.get("city") or ""
    dept = loc.get("department_name") or ""
    place = f"{city} – {dept}" if city and dept and dept not in city else (city or dept or "n/c")

    raw_price = ad.get("price")
    if isinstance(raw_price, list) and raw_price:
        value = raw_price[0]
    elif isinstance(raw_price, (int, float)):
        value = raw_price
    elif ad.get("price_cents") is not None:
        value = ad["price_cents"] / 100
    else:
        value = None

    url = ad.get("url") or f"https://www.leboncoin.fr/ad/pieces_detachees/{ad.get('list_id')}"
    body = ad.get("body") or ""
    return Listing(
        uid=f"lbc:{ad.get('list_id') or url}",
        source="LeBonCoin",
        title=ad.get("subject", ""),
        price=fmt_price(value),
        location=place,
        url=url,
        match_text=f"{ad.get('subject', '')} {body}",
        price_num=to_float(value),
    )


def fetch_leboncoin() -> list[Listing]:
    out: dict[str, Listing] = {}
    for i, query in enumerate(LBC_QUERIES):
        if i:
            polite_sleep()
        r = http_get(
            "https://www.leboncoin.fr/recherche",
            params={"text": query, "sort": "time", "order": "desc"},
            impersonate=True,
        )
        tag = BeautifulSoup(r.text, "html.parser").find("script", id="__NEXT_DATA__")
        if tag is None or not tag.string:
            raise BlockedError("page sans __NEXT_DATA__ (page anti-bot / captcha probable)")
        ads = find_ads(json.loads(tag.string)) or []
        for ad in ads:
            listing = parse_lbc_ad(ad)
            out[listing.uid] = listing
    return list(out.values())


# --------------------------------------------------------------------------- #
# Source 3 : n'importe quel site d'annonces (LesAnciennes, etc.) via sources.json
# --------------------------------------------------------------------------- #
# Séparateur de milliers : espace insécable ou point seulement. Une espace normale est ambiguë
# (« E12 525 180 € » ne doit pas devenir 12 525 180 €).
PRICE_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[\u202f\u00a0.]\d{3})+|\d+)(?:[.,]\d{1,2})?\s?(?:€|eur\b)", re.I)
POSTAL_RE = re.compile(r"\b(\d{5})\b(?!\s?(?:€|eur))", re.I)
DEPT_RE = re.compile(r"\((\d{2,3}|2[AB])\)")


def price_from_text(text: str) -> tuple[str, float | None]:
    m = PRICE_RE.search(text)
    if not m:
        return "prix n/c", None
    raw = m.group(0)
    num = re.sub(r"(?i)\s|€|eur", "", raw)
    num = num.replace(".", "").replace(",", ".") if "," in num else re.sub(r"\.(?=\d{3}(?:\.|$))", "", num)
    return raw.strip(), to_float(num)


def location_from_text(text: str) -> str:
    m = POSTAL_RE.search(text)
    if m:
        return f"CP {m.group(1)}"
    m = DEPT_RE.search(text)
    if m:
        return f"dép. {m.group(1)}"
    return "n/c (voir l'annonce)"


def parse_generic(site: str, page_url: str, page_html: str) -> list[Listing]:
    """Extraction « best effort » indépendante du site : on regroupe chaque lien avec le texte de sa carte."""
    soup = BeautifulSoup(page_html, "html.parser")
    host = urlparse(page_url).netloc.removeprefix("www.")
    cards: dict[str, dict] = {}

    def abs_url(href: str) -> str:
        return urljoin(page_url, href).split("#")[0]

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = abs_url(href)
        if urlparse(url).netloc.removeprefix("www.") != host or url.rstrip("/") == page_url.rstrip("/"):
            continue
        card = a
        for _ in range(5):  # on remonte jusqu'au conteneur de l'annonce
            parent = card.parent
            if parent is None or parent.name in ("body", "html", "main"):
                break
            if len({abs_url(x["href"]) for x in parent.find_all("a", href=True)}) > 2:
                break
            card = parent
        text = card.get_text(" ", strip=True)
        if not text or len(text) > 800:
            continue
        entry = cards.setdefault(url, {"text": "", "title": ""})
        if len(text) > len(entry["text"]):
            entry["text"] = text
        anchor_text = (a.get("title") or a.get_text(" ", strip=True) or "")[:160]
        if len(anchor_text) > len(entry["title"]):
            entry["title"] = anchor_text

    out = []
    for url, entry in cards.items():
        price, price_num = price_from_text(entry["text"])
        out.append(
            Listing(
                uid=f"{site}:{url}",
                source=site,
                title=entry["title"] or entry["text"][:120],
                price=price,
                location=location_from_text(entry["text"]),
                url=url,
                match_text=entry["text"],
                price_num=price_num,
            )
        )
    return out


def load_html_sources() -> dict[str, list[str]]:
    path = BASE_DIR / "sources.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        name: [u for u in urls if isinstance(u, str) and u.startswith("http") and "REMPLACER" not in u]
        for name, urls in data.items()
        if not name.startswith("_") and isinstance(urls, list)
    }


def fetch_html(site: str, urls: list[str]) -> list[Listing]:
    out: dict[str, Listing] = {}
    for i, url in enumerate(urls):
        if i:
            polite_sleep()
        r = http_get(url)
        for listing in parse_generic(site, url, r.text):
            out[listing.uid] = listing
    return list(out.values())


def build_sources(selected: list[str]):
    sources = []
    if "ebay" in selected:
        sources.append(("ebay", fetch_ebay))
    if "leboncoin" in selected:
        sources.append(("leboncoin", fetch_leboncoin))
    if "html" in selected:
        configured = {n: u for n, u in load_html_sources().items() if u}
        if not configured:
            log.info("[html] aucune URL dans sources.json : source ignorée")
        for name, urls in configured.items():
            sources.append((f"html:{name}", lambda n=name, u=urls: fetch_html(n, u)))
    return sources


# --------------------------------------------------------------------------- #
# Notifications : Telegram (recommandé), SMS Free Mobile, SMS / WhatsApp Twilio
# --------------------------------------------------------------------------- #
def send_telegram(rich: str, plain: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": rich, "parse_mode": "HTML"},
        timeout=20,
    )
    r.raise_for_status()


def send_free_sms(rich: str, plain: str) -> None:
    r = requests.get(
        "https://smsapi.free-mobile.fr/sendmsg",
        params={"user": os.environ["FREE_MOBILE_USER"], "pass": os.environ["FREE_MOBILE_PASS"], "msg": plain[:900]},
        timeout=20,
    )
    r.raise_for_status()


def send_twilio(rich: str, plain: str) -> None:
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, os.environ["TWILIO_AUTH_TOKEN"]),
        data={"From": os.environ["TWILIO_FROM"], "To": os.environ["TWILIO_TO"], "Body": plain[:1500]},
        timeout=20,
    )
    r.raise_for_status()


def active_channels():
    env, chans = os.getenv, []
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        chans.append(("telegram", send_telegram))
    if env("FREE_MOBILE_USER") and env("FREE_MOBILE_PASS"):
        chans.append(("free-mobile", send_free_sms))
    if all(env(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO")):
        chans.append(("twilio", send_twilio))
    return chans


def notify(rich: str, plain: str, dry_run: bool = False) -> bool:
    """True si au moins un canal a bien envoyé le message."""
    if dry_run:
        print(plain, end="\n\n")
        return True
    ok = False
    for name, send in active_channels():
        try:
            send(rich, plain)
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.error("Envoi %s échoué : %s", name, scrub(str(exc)))
    return ok


def render(listing: Listing, label: str) -> tuple[str, str]:
    e = html.escape
    rich = (
        f"🚪 <b>{e(listing.title)}</b>\n"
        f"💶 {e(listing.price)}\n"
        f"📍 {e(listing.location)}\n"
        f"{e(label)} · {e(listing.source)}\n"
        f"🔗 {e(listing.url)}"
    )
    plain = f"[E12] {listing.price} | {listing.title[:90]} | {listing.location}\n{listing.url}"
    return rich, plain


# --------------------------------------------------------------------------- #
# État (anti-doublons) et boucle principale
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("seen", {})
    data.setdefault("health", {})
    data.setdefault("bootstrapped", [])
    return data


def save_state(state: dict) -> None:
    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def run(args: argparse.Namespace) -> int:
    if not args.dry_run and not active_channels():
        log.error("Aucun canal de notification configuré (voir .env.example). Utilisez --dry-run pour tester.")
        return 2

    state = load_state()
    seen, health, boot = state["seen"], state["health"], set(state["bootstrapped"])
    now = int(time.time())
    selected = [s.strip() for s in args.sources.split(",")] if args.sources else SOURCES
    new_items: dict[str, tuple[Listing, int, str]] = {}
    digest: dict[str, tuple[Listing, int, str]] = {}

    for name, fetch in build_sources(selected):
        h = health.setdefault(name, {"fails": 0, "alerted": False})
        try:
            listings = fetch()
        except SourceSkipped as exc:
            log.info("[%s] ignorée : %s", name, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            h["fails"] += 1
            log.error("[%s] échec (%d de suite) : %s", name, h["fails"], scrub(str(exc)))
            if h["fails"] >= FAIL_ALERT_AFTER and not h["alerted"]:
                msg = f"⚠️ Bot E12 : la source « {name} » échoue depuis {h['fails']} exécutions ({scrub(str(exc))[:150]})."
                if notify(html.escape(msg), msg, args.dry_run):
                    h["alerted"] = True
            continue
        h.update(fails=0, alerted=False)

        matches: dict[str, tuple[Listing, int, str]] = {}
        for item in listings:
            result = classify(item.match_text or item.title)
            if result is None:
                log.debug("[%s] rejeté : %s", name, item.title)
                continue
            matches[item.uid] = (item, *result)
        log.info("[%s] %d annonces lues, %d pertinentes", name, len(listings), len(matches))

        first_time = name not in boot
        boot.add(name)
        if first_time and not args.notify_existing:
            digest.update(matches)  # première exécution de cette source : on mémorise sans spammer
            for uid in matches:
                seen[uid] = now
            continue
        for uid, entry in matches.items():
            if uid not in seen:
                new_items[uid] = entry

    sent = 0
    ordered = sorted(new_items.values(), key=lambda m: (-m[1], m[0].price_num if m[0].price_num is not None else 1e9))
    for listing, _score, label in ordered:
        rich, plain = render(listing, label)
        if notify(rich, plain, args.dry_run):
            seen[listing.uid] = now
            sent += 1
            time.sleep(0.4)

    if digest:
        top = sorted(digest.values(), key=lambda m: -m[1])[:10]
        lines = [f"🤖 Bot E12 initialisé : {len(digest)} annonce(s) déjà en ligne mémorisée(s)."]
        rich_lines = [html.escape(lines[0])]
        for listing, _score, label in top:
            lines.append(f"- {listing.price} | {listing.title[:70]} | {listing.location} | {listing.url}")
            rich_lines.append(
                f"\n<b>{html.escape(listing.title[:90])}</b>\n💶 {html.escape(listing.price)} · "
                f"📍 {html.escape(listing.location)} · {html.escape(label)}\n🔗 {html.escape(listing.url)}"
            )
        notify("\n".join(rich_lines), "\n".join(lines[:4]), args.dry_run)

    state["bootstrapped"] = sorted(boot)
    if not args.dry_run:
        save_state(state)
    log.info("Terminé : %d nouvelle(s) annonce(s) notifiée(s)", sent)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Alerte annonces portière arrière gauche BMW E12")
    parser.add_argument("--test", action="store_true", help="envoie un message de test et quitte")
    parser.add_argument("--dry-run", action="store_true", help="n'envoie rien et n'écrit pas l'état")
    parser.add_argument("--debug", action="store_true", help="logs détaillés (annonces rejetées, etc.)")
    parser.add_argument("--sources", help="remplace SOURCES, ex. : ebay,leboncoin,html")
    parser.add_argument("--notify-existing", action="store_true",
                        help="à la 1re exécution, notifie aussi les annonces déjà en ligne")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.test:
        chans = active_channels()
        if not chans:
            log.error("Aucun canal configuré.")
            return 2
        ok = notify("✅ Bot E12 : test de notification réussi.", "[E12] Test de notification reussi.")
        log.info("Canaux testés : %s -> %s", ", ".join(n for n, _ in chans), "OK" if ok else "ÉCHEC")
        return 0 if ok else 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
