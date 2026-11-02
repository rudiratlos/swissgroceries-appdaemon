"""Tests für die AppDaemon-App (AppDaemon selbst wird durch eine Attrappe ersetzt)."""
import pathlib
import sys
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "apps" / "aktionen_sensor"))
for _n in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
_api = types.ModuleType("appdaemon.plugins.hass.hassapi")
_api.Hass = object
sys.modules["appdaemon.plugins.hass.hassapi"] = _api

import aktionen_sensor as m  # noqa: E402

NBSP = "\u00a0"


class StubApp(m.AktionenSensor):
    def __init__(self, chain, items, **args):
        self.args = {"chain": chain, "cache_dir": tempfile.mkdtemp(), "notify_service": None, "mcp_gap": 0.0, **args}
        self.items, self.logs, self.states, self.calls = items or [], [], {}, []

    def log(self, message, **kw): self.logs.append(message)
    def run_in(self, cb, delay, **kw): return 1
    def run_every(self, *a, **kw): return 0
    def cancel_timer(self, h): pass
    def listen_state(self, *a, **kw): pass
    def entity_exists(self, e): return False
    def get_state(self, e, attribute=None): return self.items
    def set_state(self, e, state=None, attributes=None): self.states[e] = (state, attributes)
    def call_service(self, service, **kw): self.calls.append((service, kw))


@pytest.fixture
def make_app():
    def factory(chain="denner", promos=None, items=None, **args):
        app = StubApp(chain, items, **args)
        app.initialize()
        app.promos = [m.flatten(p) for p in (promos or [])]
        app.meta = {"updated": "2026-11-01T08:00:00", "ok": True, "error": None}
        return app
    return factory


# --- Matching -------------------------------------------------------------------------------
@pytest.mark.parametrize("wanted,title,expected", [
    ("Bananen", "Naturaplan Bio Fairtrade Bananen ca. 1kg", True),
    ("Banane", "Bananen", True),
    ("Äpfel", "Äpfel Golden", True),
    ("Biozitrone", "Zitronen", True),
    ("Lebkuchen", "Früchtelebkuchen mit Zartbitterschokolade", True),   # Kompositum
    ("Semmeln", "Kaisersemmel", True),
    ("Tomaten", "Datteltomaten Bio", True),
    ("Tomatenmark", "Cherry Tomaten 250g", False),
    ("Milch", "Milchschokolade", False),
    ("Lauch", "Knoblauch", False),
])
def test_matching(make_app, wanted, title, expected):
    assert make_app()._matches(wanted, {"productName": title}) is expected


@pytest.mark.parametrize("raw,core", [
    ("2 Flaschen Wein", "Wein"), ("1 Sack Äpfel · Aber nur Pink Lady", "Äpfel"),
    ("1x Tomatenmark", "Tomatenmark"), ("2 kg Möhren", "Möhren"), ("Basilikum", "Basilikum"),
])
def test_menge_und_notiz_werden_abgetrennt(raw, core):
    assert m.AktionenSensor._core(raw) == core


# --- Wein -----------------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "La Perche Mont-sur-Rolle AOC La Côte", "Chianti Classico DOCG", "Merlot del Ticino DOC",
    "Montepulciano d'Abruzzo Riserva", "Chasselas Fendant",
])
def test_ist_wein(make_app, title):
    assert make_app()._matches("Wein", {"productName": title})


@pytest.mark.parametrize("title", [
    "Spécialité Suisse Kalbsbratwurst IGP", "Iliada Olivenöl Kalamata PDO", "Le Gruyère AOP",
    "Comté AOC 18 Monate", "Parmigiano Reggiano Riserva", "Jamón Serrano Reserva",
    "Appenzeller Bier Quöllfrisch 10x33cl", "Capri-Sun Multivitamin",
])
def test_ist_kein_wein(make_app, title):
    assert not make_app()._matches("Wein", {"productName": title})


def test_farbwunsch_braucht_beleg(make_app):
    app = make_app()
    assert app._matches("Rotwein", {"productName": "Chianti Classico DOCG"})
    assert not app._matches("Rotwein", {"productName": "La Perche Mont-sur-Rolle AOC"})   # Farbe unbekannt


# --- Datum, Preis, Beschreibung -------------------------------------------------------------
def test_datum():
    assert m.iso_date("2026-11-01T22:00:00.000Z") == "2026-11-01"          # Coop
    assert m.iso_date("verfügbar ab 02.11.2026") is None
    assert m.validity({"validUntil": "verfügbar ab 02.11.2026"}) == ("2026-11-02", None)   # Aldi
    assert m.validity({"validFrom": "2026-10-29", "validUntil": "2026-11-04"}) == ("2026-10-29", "2026-11-04")


def test_preise(make_app):
    app = make_app()
    assert app._prices(m.flatten({"price": {"current": 2.2, "regular": 3.2, "currency": "CHF"}})) == (2.2, 3.2, "CHF")
    assert app._prices({"price_current": 3.59, "description": f"Was: CHF{NBSP}3.79"})[:2] == (3.59, 3.79)   # Aldi


def test_beschreibung():
    promo = m.flatten({"size_value": 100, "size_unit": "g", "unitPrice_value": 16, "unitPrice_per": "kg",
                       "description": "13%"})
    assert m.AktionenSensor._describe(promo) == "100 g · CHF 16.00/kg · 13%"


# --- Meldung an die Einkaufsliste -----------------------------------------------------------
COOP = [
    {"productName": "Naturaplan Bio Fairtrade Bananen ca. 1kg", "productUrl": "https://www.coop.ch/p/1",
     "size_value": 1, "size_unit": "kg", "unitPrice_value": 2.2, "unitPrice_per": "kg",
     "price": {"current": 2.2, "regular": 3.2, "currency": "CHF"},
     "validUntil": "2026-11-01T22:00:00.000Z", "description": "-31%"},
]


def test_treffer_wird_gemeldet(make_app):
    app = make_app("coop", COOP, items=[{"name": "Bananen"}, {"name": "Milch"}])
    app.publish()
    assert app.states["sensor.einkaufsliste_coop_treffer"][0] == "1"
    (service, data), = app.calls
    assert service == "einkaufsliste/angebote_melden" and data["source"] == "coop"
    assert data["offers"] == [{
        "name": "Bananen", "store": "Coop", "title": "Naturaplan Bio Fairtrade Bananen ca. 1kg",
        "price": 2.2, "regular_price": 3.2, "valid_until": "2026-11-01", "currency": "CHF",
        "description": "1 kg · CHF 2.20/kg · -31%", "url": "https://www.coop.ch/p/1"}]


def test_null_treffer_state_ist_text_und_liste_wird_geleert(make_app):
    app = make_app("coop", COOP, items=[{"name": "Milch"}])
    app.publish()
    assert app.states["sensor.einkaufsliste_coop_treffer"][0] == "0"     # int 0 würde HTTP 400 auslösen
    assert app.calls[0][1]["offers"] == []


def test_unveraendert_wird_nicht_erneut_gemeldet(make_app):
    app = make_app("coop", COOP, items=[{"name": "Bananen"}])
    app.publish()
    app.publish()
    assert len(app.calls) == 1


def test_send_url_abschaltbar(make_app):
    app = make_app("coop", COOP, items=[{"name": "Bananen"}], send_url=False)
    app.publish()
    assert "url" not in app.calls[0][1]["offers"][0]


# --- Warteschlange --------------------------------------------------------------------------
pool = ThreadPoolExecutor(8)      # ersetzt die AppDaemon-Worker-Threads


class QApp:
    def __init__(self, name, queue, stats, forget=False, fail_start=False):
        self.chain, self.mcp_gap, self.q, self.stats = name, 0.0, queue, stats
        self.forget, self.fail_start, self.logs, self.done = forget, fail_start, [], threading.Event()

    def log(self, message, **kw): self.logs.append(message)

    def run_in(self, cb, delay, **kw):
        if self.fail_start:
            raise RuntimeError("App beendet")
        pool.submit(lambda: (time.sleep(delay), cb()))

    def _mcp_queue_execute(self):
        s = self.stats
        s["now"] += 1
        s["max"] = max(s["max"], s["now"])
        s["order"].append(self.chain)
        time.sleep(0.15)
        s["now"] -= 1
        self.done.set()
        if not self.forget:
            self.q.complete(self)


@pytest.fixture
def q():
    return m._Queue(), {"now": 0, "max": 0, "order": []}


def test_queue_nacheinander_in_reihenfolge(q):
    queue, stats = q
    apps = [QApp(n, queue, stats) for n in ["denner", "coop", "migros", "aldi", "lidl"]]
    assert [queue.submit(a) for a in apps] == [0, 1, 2, 3, 4]
    assert all(a.done.wait(5) for a in apps)
    assert stats["max"] == 1 and stats["order"] == ["denner", "coop", "migros", "aldi", "lidl"]


def test_queue_doppelt_einreihen_wird_ignoriert(q):
    queue, stats = q
    a = QApp("denner", queue, stats)
    assert queue.submit(a) == 0 and queue.submit(a) is None
    assert a.done.wait(5)
    time.sleep(0.1)
    assert stats["order"] == ["denner"]


def test_queue_notbremse_bei_vergessenem_complete(q):
    queue, stats = q
    queue.stall_seconds = 0.5
    stuck = QApp("coop", queue, stats, forget=True)
    queue.submit(stuck)
    assert stuck.done.wait(5)
    waiting, third = QApp("denner", queue, stats), QApp("migros", queue, stats)
    queue.submit(waiting)
    assert not waiting.done.wait(0.3)        # noch gesperrt
    time.sleep(0.4)
    queue.submit(third)                      # löst die Notbremse aus
    assert third.done.wait(5) and waiting.done.wait(5)
    assert any("zurückgesetzt" in line for line in third.logs)


def test_queue_remove_und_startfehler(q):
    queue, stats = q
    busy, gone = QApp("lidl", queue, stats), QApp("aldi", queue, stats)
    queue.submit(busy)
    queue.submit(gone)
    queue.remove(gone)
    assert busy.done.wait(5)
    time.sleep(0.2)
    assert not gone.done.is_set() and queue.length() == 0
    broken, ok = QApp("coop", queue, stats, fail_start=True), QApp("denner", queue, stats)
    queue.submit(broken)
    queue.submit(ok)
    assert ok.done.wait(5)                   # defekte App wird übersprungen
