# Swissgroceries für Home Assistant

🇩🇪 Deutsch · [🇬🇧 English](README.en.md)

[![HACS-Repository öffnen](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rudiratlos&repository=swissgroceries-appdaemon&category=appdaemon)
[![Add-on-Repository hinzufügen](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Frudiratlos%2Fswissgroceries-appdaemon)

Zeigt die Wochenaktionen von **Migros, Coop, Aldi, Denner, Lidl** und weiteren Schweizer Ketten direkt an den
Artikeln deiner [Einkaufsliste](https://github.com/misterm2310/einkaufslisten-card) in Home Assistant: mit Preis,
Normalpreis, Gültigkeit, Menge/Grundpreis und Produktlink.

```
Einkaufsliste → AppDaemon-App (HACS) → Add-on "Swissgroceries MCP" → Ketten
      ↑______ einkaufsliste.angebote_melden ______|
```

## Installation

Es muss nichts von Hand kopiert werden. Zwei Teile, zwei Klicks, dann eine kurze Konfiguration:

1. **App über HACS:** Voraussetzung ist, dass in HACS die Kategorie *AppDaemon apps* aktiviert ist
   (Einstellungen → Geräte & Dienste → HACS → Konfigurieren). Dann auf den Button **HACS-Repository öffnen**
   klicken und *Herunterladen* wählen. HACS legt die App selbst in `/config/appdaemon/apps/aktionen_sensor/` ab.
   Ohne Button: HACS → ⋮ → *Benutzerdefinierte Repositories* → `https://github.com/rudiratlos/swissgroceries-appdaemon`,
   Kategorie *AppDaemon*.
2. **Add-on:** HACS kann keine Add-ons installieren, das macht der Supervisor. Auf **Add-on-Repository
   hinzufügen** klicken, bestätigen, dann *Swissgroceries MCP* installieren (der erste Build dauert auf einem
   Raspberry Pi einige Minuten), starten und *Beim Start* sowie *Watchdog* einschalten.
3. **`apps.yaml`** von AppDaemon ergänzen, pro Kette ein Eintrag, dann AppDaemon neu starten:

```yaml
denner_aktionen:
  module: aktionen_sensor
  class: AktionenSensor
  chain: denner          # denner | coop | migros | aldi | lidl | farmy | volgshop | ottos
  notify_service: null

coop_aktionen:
  module: aktionen_sensor
  class: AktionenSensor
  chain: coop
  notify_service: null
```

Die App findet das Add-on selbst, eine Adresse muss nicht eingetragen werden. Im AppDaemon-Log steht pro Kette
`... Aktionen geladen`. Es entstehen `sensor.einkaufsliste_<kette>_aktionen` und `sensor.einkaufsliste_<kette>_treffer`,
und Treffer erscheinen an den Artikeln der Einkaufsliste.

**Voraussetzungen:** Home Assistant OS oder Supervised (für Add-ons), das AppDaemon-Add-on (4.5), HACS und die
Einkaufsliste ab **v2.76.24-beta.11** (Aktion `einkaufsliste.angebote_melden`).

**Hinweise**

- Früher von Hand kopierte Dateien (`aktionen_sensor.py`, `mcp_queue.py`) in `/config/appdaemon/apps/` bitte löschen,
  sonst gibt es zwei Module mit gleichem Namen.
- Bei einem **Fork oder einem umbenannten Repository** ändert sich der Hostname des Add-ons (er enthält einen Hash der
  Repository-URL). Dann `mcp_url: http://<hostname>:8080/promotions` setzen; der Hostname steht auf der Add-on-Seite.
- Lokale Installation des Add-ons (Ordner `/addons/swissgroceries_mcp`) wird ebenfalls erkannt.

## Optionen (alle außer `chain` optional)

| Option | Standard | Bedeutung |
|---|---|---|
| `mcp_url` | automatisch | Adresse des Add-ons, nur nötig bei Fork oder besonderem Netz |
| `notify_service` | aus | z. B. `notify/mobile_app_dein_handy`: Push bei neuen Treffern |
| `scope` | `all` | `all`: alle offenen Artikel prüfen, `store`: nur Artikel dieses Geschäfts |
| `angebote_melden` | `true` | Treffer an die Einkaufsliste melden |
| `send_url` | `true` | Produktlink mitsenden (Coop, Migros) |
| `refresh_hours` / `match_interval` | `6` / `15` | Abruf bei der Kette (Stunden) / Abgleich mit der Liste (Sekunden) |
| `mcp_gap` / `retry_minutes` / `timeout` | `1.0` / `2` / `60` | Pause zwischen Abrufen / Wiederholung nach Fehler / Zeitlimit pro Abruf |
| `match` / `compound` | `word` / `true` | Wortstamm oder Teilwort / Wortteile in Komposita finden |
| `query`, `max_items` | – / `30` | Stichwortfilter / Zahl der Aktionen im Attribut `promotions` |

Weitere (`store_name`, `source`, `currency`, `list_entity`, `all_entity`, `entity`, `treffer_entity`, `cache_dir`,
`url_field`) stehen am Anfang von `initialize()` in der App.

**So werden Treffer gefunden:** Menge und Notiz werden abgetrennt (`1 Sack Äpfel · nur Pink Lady` → `Äpfel`),
Einzahl/Mehrzahl und `Bio` werden berücksichtigt. Wein wird an AOC, DOC(G), IGT, Riserva und Rebsorten erkannt; Käse,
Wurst und Olivenöl mit IGP/AOP zählen nicht als Wein. Pro Kette und Artikel wird das erste passende Angebot gemeldet.

### Geofencing-Konfiguration

Die Geofencing-App wird über folgenden Eintrag in `apps.yaml` konfiguriert:

    einkaufsliste_geofence:
      module: einkaufsliste_geofence.einkaufsliste_geofence
      class: EinkaufslisteGeofence
      # Standardradius in Metern
      radius: 35
      # Aktualisierung alle 30 Sekunden
      update_interval: 30
      # Optional: nur bestimmte Smartphones
      # trackers:
      #   - device_tracker.iphone
      #   - device_tracker.android
      # Optional: individueller Radius pro Filiale
      # radius_<geschaeft>_<plz>: 50

**Konfigurationsparameter:**

| Parameter | Beschreibung |
|---|---|
| `radius` | Standardradius des Geofence in Metern |
| `update_interval` | Aktualisierungsintervall in Sekunden |
| `trackers` | Optionale Liste der zu überwachenden Smartphone-Tracker |
| `radius_<geschaeft>_<plz>` | Individueller Geofence-Radius pro Filiale in Metern |

### Erzeugte Binary-Sensoren

Für jede erkannte Filiale wird ein eigener Binary-Sensor in Home Assistant angelegt.

**Entity-ID:**

    binary_sensor.einkaufsliste_bei_<geschaeft>_<plz>

**Zustände:**

- `on`: Mindestens ein überwachtes Smartphone befindet sich innerhalb des Geofence.
- `off`: Kein überwachtes Smartphone befindet sich innerhalb des Geofence.

Die Sensoren enthalten zusätzliche Attribute mit Filial- und Standortinformationen sowie den ermittelten Entfernungen.


## Fehlersuche

| Beobachtung | Lösung |
|---|---|
| `MCP-Add-on nicht gefunden` | Add-on installieren und starten; bei Fork `mcp_url` setzen. Test: `curl http://<hostname>:8080/health` |
| `angebote_melden fehlgeschlagen` | Einkaufsliste zu alt oder kennt `url` nicht: `send_url: false` |
| keine Treffer | Im Log zeigt `... Treffer [...] (geprueft: n Artikel gegen m Aktionen)`, was verglichen wurde |
| `Entity ... not found` beim Start | harmlos, AppDaemon legt die Sensoren selbst an |

Logs: `ha apps logs <slug>` (Add-on) und `ha apps logs a0d7b954_appdaemon` (AppDaemon).


## Updates

Updates werden über GitHub Releases bereitgestellt.

AppDaemon-App: Updates über HACS installieren.
Swissgroceries MCP: Updates über den Home Assistant Add-on-Store installieren.

Nach einem Update gegebenenfalls AppDaemon neu starten.

Repository: https://github.com/rudiratlos/swissgroceries-appdaemon

Release Notes: https://github.com/rudiratlos/swissgroceries-appdaemon/releases

## Rechtliches

Die Ketten werden über **inoffizielle App-Schnittstellen** abgefragt, sie können sich jederzeit ändern oder wegfallen.
Es besteht keine Verbindung zu den Unternehmen. Eigener Code: [MIT](LICENSE). Das Paket
[swissgroceries-mcp](https://github.com/nicktcode/swissgroceries-mcp) (PolyForm Shield License 1.0.0) ist **nicht**
enthalten, es wird beim Bauen des Add-ons aus npm installiert.
