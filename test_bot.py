"""Tests rapides (sans réseau) : python test_bot.py   (ou pytest)."""
import argparse
import json
import tempfile
from pathlib import Path

import bot


def label(text):
    r = bot.classify(text)
    return None if r is None else r[0]


def test_classify_positives():
    assert label("Portière arrière gauche BMW E12 520") == 3
    assert label("BMW E12 Tür hinten links") == 3
    assert label("Porte AR G BMW e12 gauche") == 3
    assert label("Porte BMW E12 AR") == 2
    assert label("Porte gauche BMW E12") == 1
    assert label("Porte BMW E12") == 0
    assert label("Portes avant et arrière BMW E12") == 2  # contient « arrière » : on garde


def test_classify_negatives():
    assert label("Porte avant gauche BMW E12") is None
    assert label("Porte arrière gauche BMW E28") is None
    assert label("Porte arrière BMW E21") is None
    assert label("Maquette BMW E12 1/18 porte") is None
    assert label("Cherche portière BMW E12") is None
    assert label("Jante BMW E12") is None
    assert label("Portière arrière gauche Peugeot 504") is None


def test_classify_e12_unconfirmed():
    score, lab = bot.classify("Porte arrière gauche BMW 520")
    assert score == 3 and "E12 non confirmé" in lab


def test_price_parsing():
    assert bot.price_from_text("Porte E12 525 180 € Lyon")[1] == 180.0  # le modèle 525 n'est pas dans le prix
    assert bot.price_from_text("Porte 1 250 € Paris")[1] == 1250.0
    assert bot.price_from_text("Porte 45,50 €")[1] == 45.5
    assert bot.price_from_text("Porte sans prix")[1] is None
    assert bot.fmt_price("45.00") == "45 €" and bot.fmt_price("45.5") == "45,50 €"
    assert bot.fmt_price("30", "GBP") == "30 GBP"


def test_scrub_masks_secrets():
    s = bot.scrub("404 for url: https://api.telegram.org/bot123456:ABC-def_1/sendMessage?pass=hunter2&x=1")
    assert "ABC-def" not in s and "hunter2" not in s


def test_parse_ebay_item():
    item = {
        "itemId": "v1|123|0", "title": "Tür hinten links BMW E12",
        "price": {"value": "85.00", "currency": "EUR"},
        "itemLocation": {"city": "Hamburg", "postalCode": "20095", "country": "DE"},
        "itemWebUrl": "https://www.ebay.de/itm/123",
    }
    l = bot.parse_ebay_item(item, "EBAY_DE")
    assert (l.price, l.location, l.url) == ("85 €", "Hamburg 20095 (DE)", "https://www.ebay.de/itm/123")
    assert l.source == "eBay DE" and l.uid == "ebay:v1|123|0"


def test_parse_lbc_ad():
    nested = {"props": {"pageProps": {"searchData": {"ads": [{
        "list_id": 2900000001, "subject": "Portière arrière gauche BMW E12", "price": [120],
        "location": {"city_label": "Nancy 54000", "department_name": "Meurthe-et-Moselle"},
        "url": "https://www.leboncoin.fr/ad/pieces_detachees/2900000001",
    }]}}}}
    ads = bot.find_ads(nested)
    l = bot.parse_lbc_ad(ads[0])
    assert l.price == "120 €" and "Nancy" in l.location and "Meurthe" in l.location
    assert l.uid == "lbc:2900000001" and l.url.endswith("2900000001")


GENERIC_HTML = """
<html><body>
<nav><a href="/">Accueil</a><a href="/pieces">Pièces</a></nav>
<div class="list">
  <div class="card"><a href="/annonce/101"><img src="x.jpg"></a>
    <h3><a href="/annonce/101">Portière arrière gauche BMW E12 525</a></h3>
    <span>180 €</span><span>Lyon (69)</span></div>
  <div class="card"><a href="/annonce/102">Phare BMW E12</a><span>90 €</span><span>67000 Strasbourg</span></div>
  <div class="card"><a href="/annonce/103">Porte avant BMW E12</a><span>120 €</span></div>
</div>
<a href="https://pub.example.com/ad">Pub externe porte BMW E12 arrière gauche</a>
</body></html>
"""


def test_parse_generic():
    listings = bot.parse_generic("lesanciennes", "https://www.example.fr/annonces", GENERIC_HTML)
    matched = [l for l in listings if bot.classify(l.match_text or l.title)]
    assert [l.url for l in matched] == ["https://www.example.fr/annonce/101"]
    l = matched[0]
    assert l.price_num == 180.0 and "69" in l.location and "BMW E12" in l.title
    assert not any("pub.example.com" in x.url for x in listings)  # liens externes ignorés


def test_end_to_end_state_and_dedup():
    def make(uid):
        return bot.Listing(uid=uid, source="fake", title="Porte arrière gauche BMW E12", price="100 €",
                           location="Paris", url=f"https://x/{uid}", price_num=100.0)

    batch = [make("a")]
    sent = []
    with tempfile.TemporaryDirectory() as tmp:
        bot.STATE_FILE = Path(tmp) / "seen.json"
        bot.build_sources = lambda selected: [("fake", lambda: list(batch))]
        bot.active_channels = lambda: [("fake", None)]
        bot.notify = lambda rich, plain, dry_run=False: sent.append(plain) or True
        args = argparse.Namespace(dry_run=False, sources=None, notify_existing=False)

        assert bot.run(args) == 0                     # 1re exécution : récapitulatif uniquement
        assert len(sent) == 1 and "initialisé" in sent[0]
        batch.append(make("b"))
        assert bot.run(args) == 0                     # nouvelle annonce : 1 notification
        assert len(sent) == 2 and "https://x/b" in sent[1]
        assert bot.run(args) == 0                     # rien de neuf : silence
        assert len(sent) == 2
        state = json.loads(bot.STATE_FILE.read_text())
        assert set(state["seen"]) == {"a", "b"} and state["bootstrapped"] == ["fake"]


def test_source_failure_alert():
    sent = []
    with tempfile.TemporaryDirectory() as tmp:
        bot.STATE_FILE = Path(tmp) / "seen.json"

        def boom():
            raise bot.BlockedError("HTTP 403 sur www.leboncoin.fr (blocage anti-bot)")

        bot.build_sources = lambda selected: [("leboncoin", boom)]
        bot.active_channels = lambda: [("fake", None)]
        bot.notify = lambda rich, plain, dry_run=False: sent.append(plain) or True
        args = argparse.Namespace(dry_run=False, sources=None, notify_existing=False)
        for _ in range(bot.FAIL_ALERT_AFTER + 2):
            bot.run(args)
        assert len(sent) == 1 and "leboncoin" in sent[0]  # alerte envoyée une seule fois


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"OK  {name}")
    print(f"\n{len(tests)} tests réussis")
