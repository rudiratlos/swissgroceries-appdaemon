# Swissgroceries for Home Assistant

[🇩🇪 Deutsch](README.md) · 🇬🇧 English

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rudiratlos&repository=swissgroceries-appdaemon&category=appdaemon)
[![Add add-on repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Frudiratlos%2Fswissgroceries-appdaemon)

Shows this week's offers from **Migros, Coop, Aldi, Denner, Lidl** and other Swiss supermarket chains right on the
items of your [shopping list](https://github.com/misterm2310/einkaufslisten-card) in Home Assistant: with price,
regular price, validity, quantity/unit price and product link.

```
Shopping list → AppDaemon app (HACS) → add-on "Swissgroceries MCP" → supermarket chains
      ↑______ einkaufsliste.angebote_melden ______|
```

## Installation

Nothing has to be copied by hand. Two parts, two clicks, then a short configuration:

1. **App via HACS:** make sure the category *AppDaemon apps* is enabled in HACS
   (Settings → Devices & services → HACS → Configure). Then click **Open in HACS** and choose *Download*.
   HACS places the app in `/config/appdaemon/apps/aktionen_sensor/` by itself. Without the button: HACS → ⋮ →
   *Custom repositories* → `https://github.com/rudiratlos/swissgroceries-appdaemon`, category *AppDaemon*.
2. **Add-on:** HACS cannot install add-ons, that is done by the Supervisor. Click **Add add-on repository**,
   confirm, then install *Swissgroceries MCP* (the first build takes a few minutes on a Raspberry Pi), start it and
   enable *Start on boot* and *Watchdog*.
3. **Extend AppDaemon's `apps.yaml`**, one entry per chain, then restart AppDaemon:

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

The app finds the add-on by itself, no address has to be entered. The AppDaemon log shows `... Aktionen geladen`
(offers loaded) for each chain. The sensors `sensor.einkaufsliste_<chain>_aktionen` and
`sensor.einkaufsliste_<chain>_treffer` are created, and matches appear on the items of the shopping list.

**Requirements:** Home Assistant OS or Supervised (for add-ons), the AppDaemon add-on (4.5), HACS and the shopping
list card **v2.76.24-beta.11 or newer** (action `einkaufsliste.angebote_melden`).

**Notes**

- Files copied by hand earlier (`aktionen_sensor.py`, `mcp_queue.py`) must be deleted from `/config/appdaemon/apps/`,
  otherwise two modules with the same name exist.
- For a **fork or a renamed repository** the add-on's hostname changes (it contains a hash of the repository URL).
  Then set `mcp_url: http://<hostname>:8080/promotions`; the hostname is shown on the add-on page.
- A local installation of the add-on (folder `/addons/swissgroceries_mcp`) is detected as well.

## Options (all optional except `chain`)

| Option | Default | Meaning |
|---|---|---|
| `mcp_url` | automatic | address of the add-on, only needed for a fork or special network setups |
| `notify_service` | off | e.g. `notify/mobile_app_your_phone`: push message for new matches |
| `scope` | `all` | `all`: check all open items, `store`: only items assigned to this store |
| `angebote_melden` | `true` | report matches to the shopping list |
| `send_url` | `true` | send the product link (Coop, Migros) |
| `refresh_hours` / `match_interval` | `6` / `15` | fetch from the chain (hours) / compare with the list (seconds) |
| `mcp_gap` / `retry_minutes` / `timeout` | `1.0` / `2` / `60` | pause between requests / retry after an error / timeout per request |
| `match` / `compound` | `word` / `true` | word stem or substring / find word parts in compound words |
| `query`, `max_items` | – / `30` | keyword filter / number of offers in the `promotions` attribute |

More options (`store_name`, `source`, `currency`, `list_entity`, `all_entity`, `entity`, `treffer_entity`,
`cache_dir`, `url_field`) are listed at the top of `initialize()` in the app.

**How matches are found:** quantity and notes are stripped (`1 Sack Äpfel · nur Pink Lady` → `Äpfel`), singular/plural
and the `Bio` prefix are handled. Wine is recognised by AOC, DOC(G), IGT, Riserva and grape varieties; cheese, sausage
and olive oil with IGP/AOP do not count as wine. Per chain and item, the first matching offer is reported.


## Troubleshooting

| Observation | Fix |
|---|---|
| `MCP-Add-on nicht gefunden` (add-on not found) | install and start the add-on; for a fork set `mcp_url`. Test: `curl http://<hostname>:8080/health` |
| `angebote_melden fehlgeschlagen` | shopping list too old or does not know `url`: set `send_url: false` |
| no matches | the log line `... Treffer [...] (geprueft: n Artikel gegen m Aktionen)` shows what was compared |
| `Entity ... not found` at startup | harmless, AppDaemon creates the sensors itself |

Logs: `ha apps logs <slug>` (add-on) and `ha apps logs a0d7b954_appdaemon` (AppDaemon).

## Updates

Updates available via GitHub Releases.

AppDaemon-App: install updates with HACS
Swissgroceries MCP: Updates via Home Assistant Add-on-Store

After the update restart AppDaemon

Repository: https://github.com/rudiratlos/swissgroceries-appdaemon

Release Notes: https://github.com/rudiratlos/swissgroceries-appdaemon/releases

## Legal

The chains are queried through **unofficial app interfaces**, which may change or disappear at any time. There is no
affiliation with the companies. Own code: [MIT](LICENSE). The package
[swissgroceries-mcp](https://github.com/nicktcode/swissgroceries-mcp) (PolyForm Shield License 1.0.0) is **not**
included, it is installed from npm when the add-on is built.
