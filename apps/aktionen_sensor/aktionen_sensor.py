"""AppDaemon-App: Schweizer Supermarkt-Aktionen -> Sensoren, Einkaufsliste-Angebote, Push.

Holt die Aktionen von einem MCP-Server, der als eigener Docker-Container läuft (HTTP-Endpunkt,
`mcp_url`), und gleicht sie mit der Einkaufsliste ab. Alle Ketten teilen sich die
eingebaute Warteschlange (`_Queue` weiter unten): es läuft immer nur EIN Abruf. Treffer werden
  * als Sensoren bereitgestellt,
  * per Aktion einkaufsliste.angebote_melden an die Einkaufsliste gemeldet
    (benötigt Einkaufsliste >= v2.76.24-beta.11),
  * per Push gemeldet (optional).

Kategorien: jeder Treffer bekommt
    category        = z. B. "wein"
    parent_category = z. B. "getraenke"
Die Kategorie wird über Produktfamilien (PRODUCT_FAMILIES) bestimmt, auch wenn das Wort
im Angebot nicht vorkommt (Einkaufsliste "Wein" -> Angebot "Chasselas AOC" = wein/getraenke).

Plausibilitätsprüfungen (verhindern unsinnige Zuordnungen):
  1. Kategorie der Einkaufsliste (z. B. "Haushalt") und Überkategorie des Angebots
     (z. B. "getraenke") dürfen sich nicht widersprechen.
  2. Produktfamilien dürfen sich nicht widersprechen (Tee vs. Eistee, Calgon vs. Fleisch).
  3. Einheit: Getränke/Milch erwarten Volumen (Liter), Lebensmittel Gewicht (g/kg).
     "Milch" (Liter) trifft daher keine "Milchschokolade" (Gramm).

Sensoren (Beispiel chain=denner):
  sensor.einkaufsliste_denner_aktionen  -> Anzahl Aktionen, Attribut "promotions"
  sensor.einkaufsliste_denner_treffer   -> Anzahl Treffer,  Attribute "artikel"/"details"/"kategorien"

Optionale Argumente:
  send_category: true   -> category/parent_category an angebote_melden mitsenden. Lehnt der Service
                           die Felder ab, wird einmal ohne sie gesendet und send_category abgeschaltet.
  family_guard:  true   -> Treffer verwerfen, wenn Kategorie/Familie/Einheit nicht zusammenpassen.
  plz / ort:            -> Standort fuer die ortsbasierte Suche. Ohne Angabe werden die Attribute
                           "plz" und "ort" des Sensors sensor.einkaufsliste_<chain> verwendet.
  zip_arg / city_arg:   -> Parameternamen im MCP-Aufruf (Standard "zip" und "city").
  country:              -> Länderkürzel, das als Attribut "country" im Treffer-Sensor steht
                           (Standard "CH").

Voraussetzung: der MCP-Container ist per HTTP erreichbar (POST {"chains": ["denner"]}).
"""
import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from functools import lru_cache

import appdaemon.plugins.hass.hassapi as hass

DEFAULT_MCP_URL = "http://local-swissgroceries-mcp:8080/promotions"   # Add-on im selben Docker-Netz


class _Queue:
    """Gemeinsame Warteschlange: immer nur EIN MCP-Abruf gleichzeitig.

    Alle Apps (denner, coop, ...) laufen im selben Python-Prozess und teilen sich dieses Modul,
    also auch die Instanz `_QUEUE`. Gegen vergessenes complete() gibt es eine Notbremse, doppelte
    Einträge werden ignoriert, beendete Apps fliegen mit remove() heraus.
    """

    def __init__(self, stall_seconds=180.0):
        self.stall_seconds = stall_seconds
        self._pending = deque()
        self._lock = threading.Lock()
        self._current = None      # App, deren Abruf gerade läuft
        self._started = 0.0
        self._last_end = 0.0

    def submit(self, app):
        """Einreihen. Liefert die Zahl der Abrufe davor, oder None, wenn die App schon läuft/wartet."""
        stalled = None
        with self._lock:
            if self._current is not None and time.time() - self._started > self.stall_seconds:
                stalled, self._current = self._current, None          # Notbremse
            if app is self._current or app in self._pending:
                return None
            ahead = len(self._pending) + (1 if self._current is not None else 0)
            self._pending.append(app)
            start = self._current is None
        if stalled is not None:
            self._log(app, f"MCP-Queue: Abruf von '{getattr(stalled, 'chain', stalled)}' hing "
                           f"> {self.stall_seconds:.0f} s, Queue wurde zurückgesetzt.", "WARNING")
        if start:
            self._dispatch_next()
        return ahead

    def complete(self, app):
        """Abruf der App ist fertig: nächste App starten. Veraltete Meldungen werden ignoriert."""
        with self._lock:
            if self._current is not app:
                return
            self._current = None
            self._last_end = time.time()
        self._dispatch_next()

    def remove(self, app):
        """App aus der Queue nehmen (terminate)."""
        with self._lock:
            try:
                self._pending.remove(app)
            except ValueError:
                pass
            was_current = self._current is app
            if was_current:
                self._current = None
        if was_current:
            self._dispatch_next()

    def length(self):
        with self._lock:
            return len(self._pending) + (1 if self._current is not None else 0)

    def _dispatch_next(self):
        while True:
            with self._lock:
                if self._current is not None or not self._pending:
                    return
                app = self._pending.popleft()
                self._current, self._started = app, time.time()
                gap = float(getattr(app, "mcp_gap", 0) or 0)
                delay = max(0.0, gap - (time.time() - self._last_end)) if self._last_end else 0.0
            try:
                app.run_in(app._mcp_queue_execute, delay)
                return
            except Exception as exc:                   # z. B. App wurde gerade beendet
                with self._lock:
                    if self._current is app:
                        self._current = None
                self._log(app, f"MCP-Queue: Auftrag konnte nicht gestartet werden: {exc}", "ERROR")

    @staticmethod
    def _log(app, message, level="INFO"):
        try:
            app.log(message, level=level)
        except Exception:
            pass


_QUEUE = _Queue()

# "2 kg Möhren", "1x Tomatenmark", "1 Sack Äpfel" -> Menge/Einheit am Anfang entfernen
QTY_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:x|×)?\s*"
    r"(?:(?:kg|g|l|dl|cl|ml|stk\.?|stück|sack|säcke|flaschen?|packungen?|pck\.?|"
    r"dosen?|bund|becher|gläser|glas|kisten?|beutel|schalen?|netz)\b\.?)?\s*",
    re.IGNORECASE,
)
# Füllwörter ohne Aussagekraft ("fur" = "für" nach fold(); "Calgon für Waschmaschine" darf nicht
# auf "Gewürzmischung für Fleisch" passen)
STOPWORDS = {"frisch", "frische", "frischer", "bio", "mit", "und", "ohne", "gross",
             "grosse", "klein", "kleine", "nur", "aber", "den", "der", "die", "das",
             "fur", "fuer", "von", "vom", "zum", "zur", "aus", "auf", "bei", "fr"}
ATTR_DROP = {"imageUrl", "productId"}  # spart Platz in den Attributen


def fold(text):
    """Kleinschreibung, Akzente und Umlaute entfernen (ä->a, é->e, ó->o, ß->ss)."""
    text = str(text).lower().replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def tokens(text):
    return re.findall(r"[a-z]+", fold(text))


# --- Weinerkennung (Begriffe ohne Umlaute/Akzente, kleingeschrieben) -------------
WINE_WORDS = {"wein", "wine", "vin", "vino", "weine"}
RED_WORDS = {"rotwein", "rouge", "rosso", "tinto"}
WHITE_WORDS = {"weisswein", "blanc", "bianco", "blanco"}
ROSE_WORDS = {"rose", "rosato", "rosewein"}
SPARKLING_WORDS = {"schaumwein", "sekt", "prosecco", "champagner", "champagne", "cremant",
                   "cava", "spumante", "frizzante", "brut"}
# Schutzkennzeichen, die (fast) nur bei Wein vorkommen. Käse/Wurst/Olivenöl mit AOP/DOP/IGP/PDO/PGI
# sind absichtlich NICHT dabei; AOC/DOC/... werden unten durch FOOD_WORDS wieder ausgeschlossen.
WINE_DESIGNATIONS = {"aoc", "doc", "docg", "doca", "igt", "dac", "ava", "vdp"}
# Eindeutige Hinweise auf Wein (IGP/AOP/DOP, Regionen und Marken sind absichtlich NICHT dabei,
# sie stehen auch auf Käse, Wurst, Olivenöl ...).
WINE_INDICATORS = WINE_DESIGNATIONS | {
    "riserva", "reserva", "barrique", "cuvee",
    "merlot", "pinot", "chardonnay", "cabernet", "sauvignon", "syrah", "shiraz",
    "chasselas", "gamay", "riesling", "malbec", "tempranillo", "primitivo", "sangiovese",
    "nebbiolo", "montepulciano", "negroamaro", "grenache", "garnacha", "carmenere",
    "zinfandel", "gruner", "veltliner", "muller", "thurgau", "fendant",
    "humagne", "arvine", "cornalin", "chianti", "barolo", "barbaresco", "valpolicella",
    "amarone", "rioja", "ribera", "priorat", "bordeaux", "bourgogne", "beaujolais",
    "chablis", "medoc", "dezaley", "salvagnin", "spatburgunder", "blauburgunder",
    "pinotage", "dole",
}
# Lebensmittel, bei denen AOC/DOC/Riserva/Reserva etc. NICHT Wein bedeuten
# (Endung des Wortes zählt: "reibkase", "bratwurst" ...)
FOOD_WORDS = (
    "kase", "kaese", "fromage", "formaggio", "wurst", "salami", "schinken", "jambon", "jamon",
    "speck", "prosciutto", "butter", "beurre", "rahm", "olivenol", "olivenoel", "oliven", "essig",
    "honig", "senf", "sauce", "sosse", "nudeln", "pasta", "brot", "fleisch", "poulet", "lachs",
    "gebaeck", "schokolade", "joghurt", "bier", "gruyere", "comte", "appenzeller", "emmentaler",
    "raclette", "vacherin", "sbrinz", "tilsiter", "roquefort", "brie", "camembert",
    "gorgonzola", "pecorino", "manchego", "parmigiano", "reggiano", "grana", "padano",
    "mozzarella", "burrata", "feta",
)
RED_INDICATORS = {"merlot", "cabernet", "syrah", "shiraz", "gamay", "malbec", "tempranillo",
                  "primitivo", "sangiovese", "nebbiolo", "montepulciano", "negroamaro",
                  "chianti", "barolo", "barbaresco", "valpolicella", "amarone", "rioja",
                  "salvagnin", "dole", "blauburgunder", "spatburgunder", "pinotage",
                  "zinfandel", "carmenere", "grenache", "garnacha", "beaujolais", "cornalin"}
WHITE_INDICATORS = {"chardonnay", "sauvignon", "chasselas", "riesling", "fendant",
                    "humagne", "arvine", "chablis", "muller", "thurgau", "veltliner",
                    "dezaley"}


def is_wine_word(token):
    """'Rotwein', 'Süsswein' ja, aber 'Schwein' nein."""
    return token.endswith("wein") and not token.endswith("schwein")


# --- Produktfamilien und Überkategorien --------------------------------------------
# Zum Kategorisieren und als Widerspruchs-Filter, NICHT als Ersatz für das Matching.
# Begriffe dürfen Umlaute/Akzente enthalten, sie werden beim Laden normalisiert.
PRODUCT_FAMILIES = {
    "wein": {
        "wein", "weine", "wine", "vin", "vino", "rotwein", "weisswein", "rosewein",
        "merlot", "pinot noir", "pinot nero", "pinot blanc", "pinot gris", "pinot grigio",
        "spätburgunder", "blauburgunder", "chardonnay", "sauvignon blanc", "riesling",
        "syrah", "shiraz", "gamay", "malbec", "tempranillo", "primitivo", "sangiovese",
        "nebbiolo", "barbera", "grenache", "garnacha", "cabernet sauvignon", "zinfandel",
        "pinotage", "chasselas", "fendant", "humagne", "petite arvine", "cornalin", "dôle",
        "salvagnin", "chianti", "barolo", "barbaresco", "amarone", "valpolicella", "rioja",
        "ribera del duero", "priorat", "bordeaux", "bourgogne", "beaujolais", "chablis",
        "médoc", "dézaley", "champagner", "champagne", "prosecco", "crémant", "cava",
        "spumante", "sekt", "schaumwein", "sparkling wine",
    },
    "bier": {"bier", "beer", "bière", "birra", "lager", "pils", "pilsner", "ale", "ipa",
             "weizenbier", "weissbier", "stout", "porter", "bockbier"},
    "wasser": {"wasser", "water", "eau", "acqua", "mineralwasser", "mineral water",
               "tafelwasser", "sprudelwasser", "gazosa"},
    "softdrinks": {"softdrink", "softdrinks", "limonade", "cola", "coca cola", "pepsi", "fanta",
                   "sprite", "eistee", "ice tea", "iced tea", "sirup", "energy drink",
                   "energydrink", "getränk", "getränke", "boisson", "bibita"},
    "kaffee": {"kaffee", "coffee", "café", "caffè", "espresso", "cappuccino",
               "latte macchiato"},
    # "the" (frz. Tee) ist absichtlich nicht dabei: kollidiert mit dem englischen Artikel.
    "tee": {"tee", "tea", "infusion", "kräutertee", "früchtetee", "schwarztee", "grüntee"},
    "milch": {"milch", "vollmilch", "lait"},
    "kaese": {"käse", "cheese", "fromage", "formaggio", "queso", "gruyère", "emmentaler",
              "appenzeller", "sbrinz", "raclette", "vacherin", "tilsiter", "roquefort",
              "comté", "brie", "camembert", "gorgonzola", "pecorino", "manchego",
              "parmigiano", "reggiano", "grana padano", "mozzarella", "burrata", "feta",
              "mascarpone", "ricotta"},
    "olivenoel": {"olivenöl", "olive oil", "olio d'oliva", "olio di oliva", "huile d'olive",
                  "aceite de oliva", "extra vergine", "extra virgin", "vierge extra"},
    "fleisch": {"fleisch", "meat", "viande", "carne", "schinken", "jambon", "jamón",
                "prosciutto", "speck", "salami", "wurst", "sausage", "bündnerfleisch",
                "bresaola", "pancetta", "coppa", "mortadella"},
    "essig": {"essig", "vinegar", "vinaigre", "aceto", "balsamico", "vinagre"},
    # Haushalt: Waschen, Putzen, destilliertes Wasser (Bügeleisen/Akku), Papierwaren
    "haushalt": {"calgon", "waschmittel", "waschpulver", "weichspüler", "entkalker",
                 "spülmittel", "geschirrspüler", "reiniger", "putzmittel",
                 "destilliert", "destilliertes wasser", "demineralisiert", "bügelwasser"},
    "wc_papier": {"wc papier", "toilettenpapier", "hygienepapier"},
    "haushaltspapier": {"haushaltspapier", "küchenpapier", "küchenrolle", "haushaltsrolle"},
}

PARENT_CATEGORIES = {
    "wein": "getraenke", "bier": "getraenke", "wasser": "getraenke",
    "softdrinks": "getraenke", "kaffee": "getraenke", "tee": "getraenke",
    "milch": "lebensmittel",
    "kaese": "lebensmittel", "olivenoel": "lebensmittel", "fleisch": "lebensmittel",
    "essig": "lebensmittel",
    "haushalt": "haushalt", "wc_papier": "haushalt", "haushaltspapier": "haushalt",
}
DEFAULT_PARENT = "lebensmittel"

# Wenn einer dieser Begriffe vorkommt, ist die Familie NICHT die genannte (Käse mit AOC ist kein Wein,
# destilliertes Wasser ist kein Trinkwasser).
FAMILY_EXCLUSIONS = {
    "wein": {"käse", "cheese", "fromage", "formaggio", "parmigiano", "gruyère", "emmentaler",
             "prosciutto", "wurst", "schinken", "fleisch", "olivenöl", "olive oil", "essig",
             "vinegar", "bier", "beer"},
    "wasser": {"destilliert", "destilliertes", "demineralisiert", "entkalker"},
}

# Kategorien der Einkaufsliste (Feld "kategorie") -> Überkategorie. Schlüssel ohne Akzente,
# Teilstring-Vergleich ("Kühlregal & Milch" -> "kuhlregal").
LIST_CATEGORY_PARENTS = (
    ("haushalt", "haushalt"), ("baumarkt", "baumarkt"), ("getrank", "getraenke"),
    ("kuhlregal", "lebensmittel"), ("milch", "lebensmittel"), ("obst", "lebensmittel"),
    ("gemuse", "lebensmittel"), ("fleisch", "lebensmittel"), ("brot", "lebensmittel"),
    ("tiefkuhl", "lebensmittel"), ("vorrat", "lebensmittel"), ("snack", "lebensmittel"),
)

# Familien, die trotz Überkategorie "lebensmittel" in Litern verkauft werden
VOLUME_FAMILIES = {"milch", "olivenoel", "essig"}


def _normalize_terms(terms):
    return {" ".join(tokens(t)) for t in terms if tokens(t)}


FAMILY_TERMS = {fam: _normalize_terms(terms) for fam, terms in PRODUCT_FAMILIES.items()}
FAMILY_EXCL_TERMS = {fam: _normalize_terms(terms) for fam, terms in FAMILY_EXCLUSIONS.items()}


def _has_family_term(tok_list, phrase_text, term):
    """Begriff im Text: Mehrwortbegriffe als Phrase, Einzelwörter exakt oder als Teil eines
    Kompositums (Endung ab 4 Zeichen: Reibkäse; Anfang ab 6 Zeichen: Kaffeekapseln)."""
    if " " in term:
        return f" {term} " in phrase_text
    for tok in tok_list:
        if tok == term:
            return True
        if len(term) >= 4 and tok.endswith(term):
            return True
        if len(term) >= 6 and tok.startswith(term):
            return True
    return False


@lru_cache(maxsize=8192)
def classify_family(text):
    """Produktfamilie eines Textes oder None (auch bei mehrdeutigen Treffern)."""
    tok_list = tokens(text)
    if not tok_list:
        return None
    phrase_text = " " + " ".join(tok_list) + " "
    matches = [fam for fam, terms in FAMILY_TERMS.items()
               if any(_has_family_term(tok_list, phrase_text, t) for t in terms)]
    if "wein" not in matches and any(is_wine_word(t) for t in tok_list):
        matches.append("wein")
    for fam in list(matches):
        excl = FAMILY_EXCL_TERMS.get(fam, ())
        if any(_has_family_term(tok_list, phrase_text, t) for t in excl):
            matches.remove(fam)
    return matches[0] if len(matches) == 1 else None


def parent_of(category):
    return PARENT_CATEGORIES.get(category, DEFAULT_PARENT) if category else None


def list_parent(kategorie):
    """Überkategorie aus der Kategorie des Einkaufslisten-Eintrags ('Haushalt' -> 'haushalt')."""
    key = fold(kategorie or "")
    return next((p for needle, p in LIST_CATEGORY_PARENTS if needle in key), None)


# --- Einheiten: Volumen (Liter) gegen Gewicht (Gramm) -------------------------------
MEASURE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g|lt|liter|litre|dl|cl|ml|l)\b",
                        re.IGNORECASE)
VOLUME_UNITS = {"l", "lt", "liter", "litre", "dl", "cl", "ml"}
WEIGHT_UNITS = {"g", "gr", "kg"}


def measure_kind(text):
    """'volume', 'weight' oder None (keine oder gemischte Angabe)."""
    kinds = {"volume" if unit.lower() in VOLUME_UNITS else "weight"
             for _, unit in MEASURE_RE.findall(str(text))}
    return kinds.pop() if len(kinds) == 1 else None


def promo_kind(promo):
    """Einheit des Angebots: erst strukturierte Felder, dann Titel + Beschreibung."""
    unit = str(promo.get("size_unit") or "").lower()
    if unit in VOLUME_UNITS:
        return "volume"
    if unit in WEIGHT_UNITS:
        return "weight"
    return measure_kind(f"{promo.get('productName') or ''} {promo.get('description') or ''}")


def iso_date(value):
    """'2026-11-01T22:00:00.000Z' -> '2026-11-01' (letzter Gültigkeitstag, Ortszeit Zürich)."""
    if not value:
        return None
    text = str(value)
    if not re.match(r"\d{4}-\d{2}-\d{2}", text):
        return None            # Freitext wie "verfügbar ab ..." ist kein Datum
    if len(text) <= 10:
        return text[:10]
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo("Europe/Zurich"))
            except Exception:
                pass
        return (dt - timedelta(minutes=1)).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


def validity(promo):
    """(gültig_ab, gültig_bis) als 'YYYY-MM-DD' oder None.
    Versteht ISO-Daten (Denner/Migros/Coop) und Aldi-Texte wie 'verfügbar ab 02.11.2026'."""
    valid_from = iso_date(promo.get("validFrom"))
    valid_until = iso_date(promo.get("validUntil"))
    raw = promo.get("validUntil")
    if raw and valid_until is None:
        text = str(raw)
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if m:
            day = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            if "bis" in text.lower():
                valid_until = day
            else:                       # "verfügbar ab/seit ..."
                valid_from = valid_from or day
    return valid_from, valid_until


def to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"\d+(?:[.,]\d+)?", value)
        if m:
            return float(m.group().replace(",", "."))
    return None


def flatten(obj, prefix="", depth=0):
    """Verschachtelte Felder flach machen (price: {a: 1} -> price_a, Liste -> erstes Element)."""
    out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = f"{prefix}_{key}" if prefix else str(key)
            if isinstance(value, (str, int, float, bool)):
                out[name] = value
            elif depth < 3 and isinstance(value, (dict, list)):
                out.update(flatten(value, name, depth + 1))
    elif isinstance(obj, list) and obj and depth < 3:
        out.update(flatten(obj[0], prefix, depth + 1))
    return out


class AktionenSensor(hass.Hass):
    def initialize(self):
        self.chain = self.args.get("chain", "denner")
        base = f"sensor.einkaufsliste_{self.chain}"
        self.store_entity = self.args.get("list_entity", base)       # Artikel dieses Geschäfts
        self.all_entity = self.args.get("all_entity", "sensor.einkaufsliste_offene_artikel")
        # "all": alle offenen Artikel prüfen (auch ohne/mit anderem Geschäft)
        # "store": nur Artikel, die diesem Geschäft zugewiesen sind
        self.scope = self.args.get("scope", "all")
        self.list_entity = self.all_entity if self.scope == "all" else self.store_entity
        self.entity = self.args.get("entity", f"{base}_aktionen")
        self.match_entity = self.args.get("treffer_entity", f"{base}_treffer")
        self.notify_service = self.args.get("notify_service", "notify/notify")  # None = aus
        self.report = bool(self.args.get("angebote_melden", True))
        self.source = self.args.get("source", self.chain)
        self.store_name = self.args.get("store_name", self.chain.capitalize())
        self.currency = self.args.get("currency", "CHF")
        self.country = self.args.get("country", "CH")              # Länderkürzel im Treffer-Sensor
        self.refresh_hours = float(self.args.get("refresh_hours", 6))
        self.max_items = int(self.args.get("max_items", 30))
        self.query = self.args.get("query")
        self.match_mode = self.args.get("match", "word")  # "word" oder "contains"
        self.mcp_url = self.args.get("mcp_url", DEFAULT_MCP_URL)
        self.timeout = int(self.args.get("timeout", 60))             # Sekunden pro HTTP-Abruf
        self.mcp_gap = float(self.args.get("mcp_gap", 1.0))          # Pause zwischen zwei Abrufen
        self.retry_minutes = float(self.args.get("retry_minutes", 2))  # nach Fehler erneut versuchen
        self.match_interval = int(self.args.get("match_interval", 15))  # Sekunden
        self.send_url = bool(self.args.get("send_url", True))      # Link zum Produkt mitsenden
        self.url_field = self.args.get("url_field", "url")         # Feldname bei angebote_melden
        self.compound = bool(self.args.get("compound", True))  # Wortteile in Komposita finden
        self.send_category = bool(self.args.get("send_category", True))  # Kategorien mitsenden
        self.family_guard = bool(self.args.get("family_guard", True))    # Kategorie-/Einheiten-Filter
        self.zip_arg = self.args.get("zip_arg", "zip")             # Parametername PLZ im MCP
        self.city_arg = self.args.get("city_arg", "city")          # Parametername Ort im MCP

        self.promos = []      # alle Aktionen (flach), für den Abgleich
        self.meta = None
        self.notified = set()
        self._sent_sig = None
        self._sent_at = 0.0
        self._retry_after = 0.0
        self._warned_price = False

        self.cache_file = self._cache_path()
        self._load_cache()
        self._retry_handle = None
        self._queued_at = 0.0
        plz, ort = self._location()
        self.log(f"{self.chain}: MCP-Server {self.mcp_url} (gemeinsame Warteschlange, "
                 f"Pause {self.mcp_gap:g} s), Standort {plz or '-'} {ort or ''}".rstrip())

        now = datetime.now()
        if self.promos:
            self.run_in(self.publish, 1)   # Sensoren sofort aus dem Cache setzen
        self.run_in(self.refresh, 1)       # frische Daten im Hintergrund holen
        self.run_every(self.refresh, now + timedelta(hours=self.refresh_hours),
                       int(self.refresh_hours * 3600))
        self.run_every(self.publish, now + timedelta(seconds=15), self.match_interval)
        # Sofort abgleichen, wenn sich die Einkaufsliste ändert (3 s entprellt)
        self._debounce = None
        self._last_treffer = None
        self.listen_state(self._list_changed, self.list_entity, attribute="artikel")
        if self.store_entity != self.list_entity and self.entity_exists(self.store_entity):
            self.listen_state(self._list_changed, self.store_entity, attribute="artikel")

    def _list_changed(self, *args, **kwargs):
        if self._debounce is not None:
            try:
                self.cancel_timer(self._debounce)
            except Exception:
                pass
        self._debounce = self.run_in(self.publish, 3)

    # --------------------------------------------------------------- Standort
    def _location(self):
        """(PLZ, Ort): erst App-Argumente, dann Attribute des Geschäfts-Sensors."""
        plz = self.args.get("plz")
        ort = self.args.get("ort")
        try:
            if not plz:
                plz = self.get_state(self.store_entity, attribute="plz")
            if not ort:
                ort = self.get_state(self.store_entity, attribute="ort")
        except Exception:
            pass
        plz = str(plz).strip() if plz not in (None, "", "unknown", "unavailable") else None
        ort = str(ort).strip() if ort not in (None, "", "unknown", "unavailable") else None
        return plz, ort

    # ------------------------------------------------------------------ Cache
    def _cache_path(self):
        base = self.args.get("cache_dir", "/homeassistant/appdaemon/cache")
        if not os.path.isdir(base):
            base = getattr(self, "config_dir", None) or "/config"
        plz, _ = self._location()
        suffix = f"_{plz}" if plz else ""
        return os.path.join(base, f"aktionen_{self.chain}{suffix}.json")

    def _load_cache(self):
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                data = json.load(f)
            self.promos = data.get("promos", [])
            self.meta = {**data.get("meta", {}), "cached": True}
            self.log(f"{self.chain}: {len(self.promos)} Aktionen aus Cache "
                     f"(Stand {self.meta.get('updated')})")
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.log(f"Cache nicht lesbar: {exc}", level="WARNING")

    def _save_cache(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump({"promos": self.promos, "meta": self.meta}, f, ensure_ascii=False)
        except Exception as exc:
            self.log(f"Cache nicht schreibbar: {exc}", level="WARNING")

    @staticmethod
    def _expired(promo):
        until = validity(promo)[1] or ""
        return bool(until) and until < datetime.now().strftime("%Y-%m-%d")

    @property
    def active(self):
        return [p for p in self.promos if not self._expired(p)]

    # ------------------------------------------------------------------ Abruf
    def _call_promotions(self):
        """HTTP-Abruf (läuft im AppDaemon-Thread dieser App, serialisiert durch _QUEUE)."""
        args = {"chains": [self.chain]}
        if self.query:
            args["query"] = self.query
        plz, ort = self._location()
        if plz:
            args[self.zip_arg] = plz
        if ort:
            args[self.city_arg] = ort
        request = urllib.request.Request(
            self.mcp_url,
            data=json.dumps(args).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"HTTP-Anfrage an MCP-Server fehlgeschlagen: {exc}") from exc

    @staticmethod
    def _extract(response):
        result = response.get("result", {})
        text = "\n".join(c.get("text", "") for c in result.get("content", [])
                         if c.get("type") == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], text
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return value, None
        return [], text

    def refresh(self, *args, **kwargs):
        """Reiht den Abruf in die gemeinsame Warteschlange ein und kehrt sofort zurück."""
        if self._retry_handle is not None:
            try:
                self.cancel_timer(self._retry_handle)
            except Exception:
                pass
            self._retry_handle = None
        self._queued_at = time.time()
        ahead = _QUEUE.submit(self)
        if ahead is None:
            return                          # läuft oder wartet für diese Kette schon
        if ahead:
            self.log(f"{self.chain}: Abruf eingereiht, {ahead} Abruf(e) davor")

    def _mcp_queue_execute(self, *args, **kwargs):
        """Wird von _QUEUE aufgerufen, wenn diese Kette an der Reihe ist."""
        started = time.time()
        waited = started - self._queued_at
        result = error = None
        try:
            result = self._call_promotions()
        except Exception as exc:
            error = exc
        finally:
            _QUEUE.complete(self)        # immer! sonst blockiert die Queue
        self._apply(result, error, waited, time.time() - started)

    def _apply(self, result, error, waited, took):
        now = datetime.now().isoformat(timespec="seconds")
        try:
            if error:
                raise error
            if "error" in result:
                raise RuntimeError(result["error"])
            items, raw = self._extract(result)
            if not items and raw:
                raise RuntimeError(raw)
            if items and not self.promos:
                self.log("Beispiel-Aktion (roh): " + json.dumps(items[0], ensure_ascii=False)[:1500])
            self.promos = [flatten(i) if isinstance(i, dict) else {"productName": str(i)}
                           for i in items]
            self.meta = {"updated": now, "ok": True, "error": None}
            self.log(f"{self.chain}: {len(self.promos)} Aktionen geladen "
                     f"(gewartet {waited:.1f} s, Abruf {took:.1f} s)")
            self._save_cache()
        except Exception as exc:
            self.log(f"{self.chain}: Abruf fehlgeschlagen: {exc} "
                     f"- neuer Versuch in {self.retry_minutes:g} min", level="WARNING")
            old = self.meta or {"updated": now}
            self.meta = {**old, "ok": False, "error": str(exc)}
            self._retry_handle = self.run_in(self.refresh, int(self.retry_minutes * 60))
        self.publish()

    def terminate(self):
        """AppDaemon ruft das beim Stoppen/Neuladen auf: App aus der Queue nehmen."""
        _QUEUE.remove(self)

    # ------------------------------------------------------------- Auswertung
    @staticmethod
    def _title(promo):
        for key in ("productName", "name", "title"):
            value = promo.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @classmethod
    def _promo_text(cls, promo):
        return f"{cls._title(promo)} {promo.get('description') or ''}"

    @staticmethod
    def _core(raw):
        """'1 Sack Äpfel · Aber nur Pink Lady' -> 'Äpfel'."""
        text = raw.split("·")[0].strip()
        return QTY_RE.sub("", text, count=1).strip() or raw.strip()

    def _wine_match(self, wanted_tokens, promo):
        """Wein-Artikel gegen Titel+Beschreibung: AOC/DOC/..., Rebsorten, Regionen."""
        kinds = set(wanted_tokens)
        is_wine = bool(kinds & (WINE_WORDS | RED_WORDS | WHITE_WORDS | ROSE_WORDS
                                | SPARKLING_WORDS)) or any(
            is_wine_word(t) for t in wanted_tokens)
        if not is_wine:
            return None  # kein Wein-Artikel
        tok = set(tokens(self._promo_text(promo)))
        if any(t.endswith(f) for t in tok for f in FOOD_WORDS):
            return False   # Käse, Wurst, Olivenöl ... mit AOC/Riserva ist kein Wein
        wine_hit = bool(tok & (WINE_INDICATORS | WINE_WORDS)) or any(
            is_wine_word(t) for t in tok)
        sparkling = bool(tok & SPARKLING_WORDS)
        red = bool(tok & (RED_WORDS | RED_INDICATORS))
        white = bool(tok & (WHITE_WORDS | WHITE_INDICATORS))
        rose = bool(tok & ROSE_WORDS)

        wants_red = bool(kinds & RED_WORDS) or "rotwein" in kinds
        wants_white = bool(kinds & WHITE_WORDS) or "weisswein" in kinds
        wants_rose = bool(kinds & ROSE_WORDS)
        wants_sparkling = bool(kinds & SPARKLING_WORDS)

        if wants_sparkling:
            return sparkling
        if wants_rose:
            return rose
        if wants_red:
            return wine_hit and red and not sparkling   # Farbe muss belegt sein
        if wants_white:
            return wine_hit and white and not sparkling
        return wine_hit or sparkling  # allgemein "Wein"

    def _expected_kind(self, raw, core, list_category, menge=None):
        """Erwartete Einheit des Listeneintrags.

        Getränke und Milch/Öl/Essig -> 'volume' (Liter), Lebensmittel -> 'weight' (g/kg),
        sonst None (z. B. Haushalt, Baumarkt: Flüssigkeiten und Feststoffe gemischt).
        Eine explizite Angabe im Eintrag ('1 l Milch', Menge '500 g') hat Vorrang."""
        explicit = measure_kind(f"{raw} {menge or ''}")
        if explicit:
            return explicit
        family = classify_family(core)
        parent = list_parent(list_category) or (parent_of(family) if family else None)
        if family in VOLUME_FAMILIES or parent == "getraenke":
            return "volume"
        if parent == "lebensmittel":
            return "weight"
        return None

    def _category_ok(self, wanted, list_category, promo):
        """Liste und Angebot dürfen sich bei eindeutiger Kategorie nicht widersprechen."""
        if not self.family_guard:
            return True
        w_fam = classify_family(wanted)
        p_fam = classify_family(self._promo_text(promo))
        if w_fam and p_fam and w_fam != p_fam:
            return False
        w_parent = list_parent(list_category) or (parent_of(w_fam) if w_fam else None)
        p_parent = parent_of(p_fam) if p_fam else None
        return not (w_parent and p_parent and w_parent != p_parent)

    def _matches(self, wanted, promo, list_category=None, expected=None):
        # Einheit: Liter gegen Gramm kann nicht dasselbe Produkt sein (Milch vs. Milchschokolade)
        if self.family_guard and expected:
            found = promo_kind(promo)
            if found and found != expected:
                return False
        # Kategorie/Familie: Haushalt vs. Lebensmittel, Tee vs. Eistee, Wasser vs. dest. Wasser
        if not self._category_ok(wanted, list_category, promo):
            return False
        title = self._title(promo)
        if self.match_mode == "contains" and fold(wanted) in fold(title):
            return True
        wanted_tokens = tokens(wanted)
        wine = self._wine_match(wanted_tokens, promo)
        if wine:
            return True
        if self.match_mode == "contains":
            return False
        title_tokens = tokens(title)
        for word in wanted_tokens:
            if word in STOPWORDS:
                continue
            if word.startswith("bio") and len(word) > 5:  # Biozitrone -> zitrone
                word = word[3:]
            if len(word) < 3:
                continue
            stems = {word}
            if word.endswith("en"):
                stems.add(word[:-2])
            if word.endswith("n") or word.endswith("e") or word.endswith("s"):
                stems.add(word[:-1])
            for t in title_tokens:
                if t == word or (t.startswith(word) and len(t) - len(word) <= 3):
                    return True
                # Mehrzahl auf der Liste, Einzahl im Titel (Bananen -> Banane)
                if len(t) >= 4 and word.startswith(t) and len(word) - len(t) <= 3:
                    return True
                # zusammengesetztes Wort: Lebkuchen -> Früchtelebkuchen, Semmeln -> Kaisersemmel
                if self.compound:
                    for st in stems:
                        if (len(st) >= 5 and len(t) > len(st)
                                and re.search(re.escape(st) + r"(?:n|en|e|s)?$", t)
                                and not (st == "lauch" and t.endswith(("knoblauch", "barlauch")))):
                            return True
        return False

    def _categorize(self, wanted, promo):
        """(category, parent_category) für einen Treffer.

        Reihenfolge: Weinlogik, Familie des Listeneintrags, Familie des Angebots.
        Beispiel: Liste 'Wein', Angebot 'Chasselas AOC' -> ('wein', 'getraenke')."""
        if self._wine_match(tokens(wanted), promo):
            category = "wein"
        else:
            category = classify_family(wanted) or classify_family(self._promo_text(promo))
        return category, parent_of(category)

    def _open_items(self):
        """Offene Artikel als Liste von (Name, Kategorie der Einkaufsliste, Menge)."""
        raw = self.get_state(self.list_entity, attribute="artikel")
        if not raw and self.list_entity != self.store_entity:
            raw = (self.get_state(self.store_entity, attribute="details")
                   or self.get_state(self.store_entity, attribute="artikel"))   # Fallback
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
        out = []
        for entry in raw:
            category = menge = None
            if isinstance(entry, dict):
                category, menge = entry.get("kategorie"), entry.get("menge")
                entry = entry.get("name")
            if isinstance(entry, str) and entry.strip():
                out.append((entry.strip(), category, menge))
        return out

    # ---------------------------------------------------------------- Preise
    PRICE_NOW = ("price_current", "currentprice", "promoprice", "saleprice", "actionprice",
                 "price_promo", "price_sale", "price_now", "price_value", "price_amount",
                 "price", "preis", "aktionspreis")
    PRICE_REG = ("price_regular", "regularprice", "price_old", "oldprice", "originalprice",
                 "price_original", "price_before", "price_normal", "normalprice",
                 "price_was", "price_strike", "price_base_regular", "normalpreis")
    PRICE_SKIP = ("currency", "unit", "per", "percent", "discount", "saving", "rabatt",
                  "quantity", "amount_unit", "id")

    def _prices(self, promo):
        """(aktuell, normal, waehrung) – Feldnamen tolerant erkannt."""
        low = {k.lower(): v for k, v in promo.items()}
        now = next((to_number(low[k]) for k in self.PRICE_NOW
                    if k in low and to_number(low[k]) is not None), None)
        reg = next((to_number(low[k]) for k in self.PRICE_REG
                    if k in low and to_number(low[k]) is not None), None)
        if now is None:  # Fallback: irgendwelche Zahlen in "price"-Feldern
            nums = sorted({to_number(v) for k, v in low.items()
                           if ("price" in k or "preis" in k)
                           and not any(s in k for s in self.PRICE_SKIP)
                           and to_number(v) is not None})
            if nums:
                now = nums[0]
                reg = reg or (nums[-1] if nums[-1] > nums[0] else None)
        if reg is None:  # Aldi: "Was: CHF 3.79" im Beschreibungstext
            m = re.search(r"(?:was|statt)\s*:?\s*chf\s*([\d.,]+)",
                          str(promo.get("description", "")), re.IGNORECASE)
            if m:
                reg = to_number(m.group(1))
        if reg is not None and now is not None and reg <= now:
            reg = None
        cur = next((str(v) for k, v in low.items() if "currency" in k and isinstance(v, str)),
                   None)
        return now, reg, cur

    @staticmethod
    def _describe(promo):
        """'1 kg · CHF 2.20/kg · -31%' – damit sieht man, worauf sich der Preis bezieht."""
        parts = []
        size, unit = promo.get("size_value"), promo.get("size_unit")
        if size not in (None, "") and unit:
            parts.append(f"{to_number(size):g} {'Stk.' if unit == 'piece' else unit}")
        up, per = to_number(promo.get("unitPrice_value")), promo.get("unitPrice_per")
        if up is not None and per:
            parts.append(f"CHF {up:.2f}/{'Stk.' if per == 'piece' else per}")
        desc = str(promo.get("description") or "").strip()
        if desc and not re.match(r"(?:was|statt)\s*:", desc, re.IGNORECASE):
            parts.append(desc)
        return " · ".join(parts) or None

    @staticmethod
    def _date(value):
        try:
            return datetime.strptime(iso_date(value) or "", "%Y-%m-%d").strftime("%d.%m.")
        except ValueError:
            return str(value)

    # -------------------------------------------------------------- Ausgabe
    def publish(self, *args, **kwargs):
        if self.meta is None:
            return
        active = self.active
        treffer, seen = [], set()
        items = self._open_items()
        for raw, list_cat, menge in items:
            core = self._core(raw)
            expected = self._expected_kind(raw, core, list_cat, menge)
            hit = next((p for p in active if self._matches(core, p, list_cat, expected)), None)
            if hit and raw not in seen:
                seen.add(raw)
                now, reg, cur = self._prices(hit)
                category, parent = self._categorize(core, hit)
                entry = {k: v for k, v in hit.items() if k not in ATTR_DROP}
                if "category" in entry:                  # eigene Kategorie des MCP-Servers retten
                    entry["source_category"] = entry.pop("category")
                entry.pop("parent_category", None)
                entry.update({"artikel": raw, "suche": core, "aktion": self._title(hit),
                              "preis": now, "normalpreis": reg,
                              "category": category, "parent_category": parent,
                              "liste_kategorie": list_cat})
                treffer.append(entry)

        shown = [{k: v for k, v in p.items() if k not in ATTR_DROP}
                 for p in active[: self.max_items]]
        self.set_state(self.entity, state=str(len(active)), attributes={
            "friendly_name": f"Einkaufsliste {self.chain.capitalize()} Aktionen",
            "icon": "mdi:tag-multiple",
            "unit_of_measurement": "Aktionen",
            "updated": self.meta.get("updated"),
            "ok": self.meta.get("ok"),
            "aus_cache": bool(self.meta.get("cached")),
            "error": self.meta.get("error"),
            "shown": len(shown),
            "promotions": shown,
        })
        self.set_state(self.match_entity, state=str(len(treffer)), attributes={
            "friendly_name": f"Einkaufsliste {self.chain.capitalize()} Treffer",
            "country": self.country,
            "icon": "mdi:cart-percent",
            "unit_of_measurement": "Treffer",
            "artikel": [t["artikel"] for t in treffer],
            "kategorien": {t["artikel"]: {"category": t["category"],
                                          "parent_category": t["parent_category"]}
                           for t in treffer},
            "details": treffer,
        })
        names = sorted(t["artikel"] for t in treffer)
        if names != self._last_treffer:
            self._last_treffer = names
            self.log(f"{self.chain}: {len(names)} Treffer {names} "
                     f"(geprueft: {len(items)} Artikel gegen {len(active)} Aktionen)")
        if self.report:
            self._report_offers(treffer)
        self._notify(treffer)

    @staticmethod
    def _strip_categories(offers):
        return [{k: v for k, v in o.items() if k not in ("category", "parent_category")}
                for o in offers]

    def _send_offers(self, offers):
        self.call_service("einkaufsliste/angebote_melden", source=self.source, offers=offers)

    def _report_offers(self, treffer):
        """Treffer an die Einkaufsliste melden (einkaufsliste.angebote_melden)."""
        offers = []
        for t in treffer:
            price, regular, cur = self._prices(t)
            if price is None:
                if not self._warned_price:
                    self._warned_price = True
                    self.log("Keine Preisangabe in der Aktion gefunden – ohne Preis kann "
                             "die Einkaufsliste das Angebot nicht anzeigen. Felder: "
                             + ", ".join(sorted(k for k in t if k not in ("details",))),
                             level="WARNING")
                continue
            offer = {
                "name": t["artikel"],
                "store": self.store_name,
                "title": t.get("aktion"),
                "price": price,
                "regular_price": regular,
                "valid_from": validity(t)[0],
                "valid_until": validity(t)[1],
                "description": self._describe(t),
                "currency": cur or self.currency,
            }
            if self.send_category:
                offer["category"] = t.get("category")
                offer["parent_category"] = t.get("parent_category")
            if self.send_url and t.get("productUrl"):
                offer[self.url_field] = t["productUrl"]
            offers.append({k: v for k, v in offer.items() if v not in (None, "")})

        sig = json.dumps(offers, sort_keys=True, ensure_ascii=False)
        if time.time() < self._retry_after:
            return
        # nur bei Änderung senden, sonst höchstens stündlich zur Auffrischung
        if sig == self._sent_sig and time.time() - self._sent_at < 3600:
            return
        try:
            self._send_offers(offers)
        except Exception as exc:
            stripped = self._strip_categories(offers)
            if stripped == offers:                       # keine Kategorien im Spiel: echter Fehler
                self._report_failed(exc, sig)
                return
            try:                                         # evtl. kennt der Service die Felder nicht
                self._send_offers(stripped)
            except Exception as exc2:
                self._report_failed(exc2, sig)
                return
            self.send_category = False
            offers = stripped
            sig = json.dumps(offers, sort_keys=True, ensure_ascii=False)
            self.log(f"{self.chain}: angebote_melden lehnt category/parent_category ab ({exc}) "
                     f"- Kategorien werden nicht mehr mitgesendet.", level="WARNING")
        self._sent_sig, self._sent_at = sig, time.time()
        self.log(f"{len(offers)} Angebot(e) an die Einkaufsliste gemeldet")

    def _report_failed(self, exc, sig):
        self._retry_after = time.time() + 300   # nach Fehler 5 Minuten Pause
        self.log(f"{self.chain}: einkaufsliste.angebote_melden fehlgeschlagen: {exc} "
                 f"| gesendet: {sig[:600]}", level="ERROR")

    def _notify(self, treffer):
        current = {t["artikel"].lower() for t in treffer}
        neu = [t for t in treffer if t["artikel"].lower() not in self.notified]
        self.notified &= current
        if neu and self.notify_service:
            zeilen = []
            for t in neu:
                zeile = f"{t['suche']}: {t['aktion']}"
                if t.get("preis") is not None:
                    zeile += f" ({self.currency} {t['preis']:.2f})"
                if validity(t)[1]:
                    zeile += f", bis {self._date(validity(t)[1])}"
                zeilen.append(zeile)
            self.call_service(self.notify_service,
                              title=f"{self.chain.capitalize()}-Aktion für deine Liste",
                              message="\n".join(zeilen))
        self.notified |= current
