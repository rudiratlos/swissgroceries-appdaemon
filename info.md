# Swissgroceries – Aktionen Sensor

AppDaemon-App für Home Assistant zur Integration aktueller Schweizer Supermarkt-Angebote.

Die App ruft Aktionen und Sonderangebote über den Swissgroceries MCP-Dienst ab und stellt diese als Sensoren in Home Assistant bereit.

## Unterstützte Supermärkte

- Migros
- Coop
- Denner
- Weitere unterstützte Händler abhängig vom MCP-Dienst

## Funktionen

- Automatisches Abrufen aktueller Aktionen
- Sensoren für die konfigurierten Supermarktketten
- Integration in Home Assistant
- Bereitstellung der Angebotsdaten über AppDaemon
- Konfigurierbare Aktualisierungsintervalle

## Voraussetzungen

- Home Assistant
- HACS
- AppDaemon Add-on
- Swissgroceries MCP Add-on

## Installation

1. Repository über HACS als AppDaemon-Repository hinzufügen.
2. AppDaemon-App installieren.
3. Swissgroceries MCP Add-on separat installieren und starten.
4. Die gewünschten Supermarktketten in `apps.yaml` konfigurieren.
5. AppDaemon neu starten.

## Repository

https://github.com/rudiratlos/swissgroceries-appdaemon

## Lizenz

Siehe [LICENSE](LICENSE).

