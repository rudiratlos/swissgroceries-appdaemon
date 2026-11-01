# Swissgroceries für Home Assistant

Zeigt die Wochenaktionen von Migros, Coop, Aldi, Denner, Lidl & Co. an den Artikeln deiner
[Einkaufsliste](https://github.com/misterm2310/einkaufslisten-card) (ab v2.76.24-beta.11): mit Preis,
Normalpreis, Gültigkeit, Menge/Grundpreis und Produktlink.

```
Einkaufsliste → AppDaemon-App → HTTP → Add-on "Swissgroceries MCP" → Ketten
      ↑______ einkaufsliste.angebote_melden ______|
```

- `apps/aktionen_sensor/aktionen_sensor.py`: die AppDaemon-App (eine Datei)
- `swissgroceries_mcp/`: das Add-on, ein HTTP-Dienst um [swissgroceries-mcp](https://github.com/nicktcode/swissgroceries-mcp)

## Installation

1. **Add-on:** Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories →
   `https://github.com/rudiratlos/swissgroceries-appdaemon` hinzufügen, *Swissgroceries MCP*
   installieren, starten, *Beim Start* und *Watchdog* einschalten. Den **Hostnamen** von der Add-on-Seite
   notieren (bei lokaler Installation in `/addons`: `local-swissgroceries-mcp`).
2. **App:** `apps/aktionen_sensor/aktionen_sensor.py` nach `/config/appdaemon/apps/aktionen_sensor/` kopieren.
3. **`apps.yaml`:** pro Kette ein Eintrag, dann AppDaemon neu starten:

```yaml
denner_aktionen:
  module: aktionen_sensor
  class: AktionenSensor
  chain: denner          # denner | coop | migros | aldi | lidl | farmy | volgshop | ottos
  mcp_url: http://<hostname>:8080/promotions
  notify_service: null   # oder z. B. notify/mobile_app_dein_handy

coop_aktionen:
  module: aktionen_sensor
  class: AktionenSensor
  chain: coop
  mcp_url: http://<hostname>:8080/promotions
  notify_service: null
```

Im Log steht pro Kette `... Aktionen geladen`. Es entstehen `sensor.einkaufsliste_<kette>_aktionen` und
`sensor.einkaufsliste_<kette>_treffer`.

## Optionen (alle außer `chain` optional)

| Option | Standard | Bedeutung |
|---|---|---|
| `mcp_url` | `http://local-swissgroceries-mcp:8080/promotions` | Adresse des Add-ons |
| `scope` | `all` | `all`: alle offenen Artikel prüfen, `store`: nur Artikel dieses Geschäfts |
| `refresh_hours` / `match_interval` | `6` / `15` | Abruf bei der Kette (Stunden) / Abgleich mit der Liste (Sekunden) |
| `mcp_gap` / `retry_minutes` / `timeout` | `1.0` / `2` / `60` | Pause zwischen Abrufen / Wiederholung nach Fehler / Zeitlimit pro Abruf |
| `angebote_melden` | `true` | Treffer an die Einkaufsliste melden |
| `send_url` | `true` | Produktlink mitsenden (Coop, Migros) |
| `notify_service` | `notify/notify` | Push bei neuen Treffern, `null` = aus |
| `match` / `compound` | `word` / `true` | Wortstamm oder Teilwort / Wortteile in Komposita finden |
| `query`, `max_items` | – / `30` | Stichwortfilter / Zahl der Aktionen im Attribut `promotions` |

Weitere (`store_name`, `source`, `currency`, `list_entity`, `all_entity`, `entity`, `treffer_entity`, `cache_dir`,
`url_field`) stehen am Anfang von `initialize()` in der App.

**Treffer:** Menge und Notiz werden abgetrennt (`1 Sack Äpfel · nur Pink Lady` → `Äpfel`), Einzahl/Mehrzahl und
`Bio` werden berücksichtigt. Wein wird an AOC, DOC(G), IGT, Riserva und Rebsorten erkannt; Käse, Wurst und Olivenöl
mit IGP/AOP zählen nicht als Wein. Pro Kette und Artikel wird das erste passende Angebot gemeldet.

## Fehlersuche

| Beobachtung | Lösung |
|---|---|
| `Abruf fehlgeschlagen ... HTTP-Anfrage` | Add-on läuft nicht oder `mcp_url` stimmt nicht: `curl http://<hostname>:8080/health` |
| `angebote_melden fehlgeschlagen` | Einkaufsliste zu alt oder kennt `url` nicht: `send_url: false` |
| keine Treffer | Log `... Treffer [...] (geprueft: n Artikel gegen m Aktionen)` zeigt, was verglichen wurde |

## Neue Version veröffentlichen

Die Version steht nur in `swissgroceries_mcp/config.yaml` (Home Assistant braucht sie dort für Updates).

```sh
# 1. version in swissgroceries_mcp/config.yaml erhöhen, z. B. auf "1.1.0"
git commit -am "release: v1.1.0"
git tag v1.1.0
git push && git push origin v1.1.0
```

Der Workflow prüft, dass Tag und `version` übereinstimmen, führt die Tests aus und erstellt auf GitHub ein
Release mit automatischen Notizen und einem ZIP. Versionen folgen [SemVer](https://semver.org/lang/de/)
(`MAJOR` inkompatibel, `MINOR` neue Funktion, `PATCH` Fehlerkorrektur). Die Version des Pakets swissgroceries-mcp
steht im `Dockerfile` (`ARG MCP_VERSION`).

Tests: `pip install pytest && python3 -m pytest`

## Rechtliches

Die Ketten werden über **inoffizielle App-Schnittstellen** abgefragt, sie können sich jederzeit ändern oder
wegfallen. Es besteht keine Verbindung zu den Unternehmen. Eigener Code: MIT. Das Paket swissgroceries-mcp
(PolyForm Shield License 1.0.0) ist **nicht** enthalten, es wird beim Bauen des Add-ons aus npm installiert.
