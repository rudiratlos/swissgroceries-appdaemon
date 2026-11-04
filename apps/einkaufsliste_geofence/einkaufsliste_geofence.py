"""AppDaemon-App: Automatische Geofences für Einkaufslisten-Filialen.

Erkennt sensor.einkaufsliste_* und erstellt dynamische Binary-Sensoren:

binary_sensor.einkaufsliste_bei_<geschaeft>_<plz>

- Adresse aus den Sensorattributen
- Geocoding über OpenStreetMap Nominatim
- Lokaler Geocoding-Cache
- Standortbestimmung über device_tracker.*
- Standardradius 35 Meter
- Individueller Radius pro Filiale
- Keine Home-Assistant-Zonen erforderlich
"""

import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request

import appdaemon.plugins.hass.hassapi as hass


class EinkaufslisteGeofence(hass.Hass):

    def initialize(self):

        self.radius = float(self.args.get("radius", 35))
        self.update_interval = int(self.args.get("update_interval", 30))

        self.sensor_prefix = self.args.get(
            "sensor_prefix", "sensor.einkaufsliste_"
        )

        self.entity_prefix = self.args.get(
            "entity_prefix", "binary_sensor.einkaufsliste_bei_"
        )

        self.cache_file = self.args.get(
            "cache_file",
            "/config/appdaemon/apps/einkaufsliste_geofence/geocode_cache.json"
        )

        # Optional: nur bestimmte Smartphones überwachen.
        self.trackers_config = self.args.get("trackers", [])

        self.stores = {}
        self.geocode_cache = {}
        self.last_geocode = 0

        self._load_cache()

        self.log(
            f"Einkaufsliste Geofence gestartet. "
            f"Standardradius: {self.radius} m"
        )

        self.run_every(
            self.update,
            self.datetime(),
            self.update_interval
        )

    # ---------------------------------------------------------
    # Cache
    # ---------------------------------------------------------

    def _load_cache(self):

        try:
            if os.path.exists(self.cache_file):

                with open(self.cache_file, "r", encoding="utf-8") as file:
                    self.geocode_cache = json.load(file)

        except Exception as exc:

            self.log(
                f"Geocoding-Cache konnte nicht geladen werden: {exc}",
                level="WARNING"
            )

    def _save_cache(self):

        try:
            directory = os.path.dirname(self.cache_file)

            if directory:
                os.makedirs(directory, exist_ok=True)

            temporary = self.cache_file + ".tmp"

            with open(temporary, "w", encoding="utf-8") as file:

                json.dump(
                    self.geocode_cache,
                    file,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(temporary, self.cache_file)

        except Exception as exc:

            self.log(
                f"Cache konnte nicht gespeichert werden: {exc}",
                level="WARNING"
            )

    # ---------------------------------------------------------
    # Hilfsfunktionen
    # ---------------------------------------------------------

    @staticmethod
    def slugify(value):

        value = str(value or "").strip().lower()
        value = value.replace("ß", "ss")

        value = unicodedata.normalize("NFKD", value)

        value = "".join(
            char for char in value
            if not unicodedata.combining(char)
        )

        value = re.sub(r"[^a-z0-9]+", "_", value)

        return value.strip("_")

    @staticmethod
    def distance_m(lat1, lon1, lat2, lon2):

        """Berechnet die Entfernung zwischen zwei GPS-Koordinaten."""

        earth_radius = 6371000

        lat1 = math.radians(float(lat1))
        lon1 = math.radians(float(lon1))
        lat2 = math.radians(float(lat2))
        lon2 = math.radians(float(lon2))

        dlat = lat2 - lat1
        dlon = lon2 - lon1

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1)
            * math.cos(lat2)
            * math.sin(dlon / 2) ** 2
        )

        return earth_radius * 2 * math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a)
        )

    # ---------------------------------------------------------
    # Geocoding
    # ---------------------------------------------------------

    def geocode(self, address, plz="", ort="", land="CH"):

        """Ermittelt GPS-Koordinaten über OpenStreetMap Nominatim."""

        # Vollständige Adresse zusammensetzen:
        # Via Indipendenza 10, 6826 Riva S. Vitale, CH

        plz_ort = " ".join(
            str(part).strip()
            for part in (plz, ort)
            if part
        )

        full_address = ", ".join(
            str(part).strip()
            for part in (address, plz_ort, land)
            if part
        )

        cache_key = full_address.lower()

        # Bereits gespeicherte Koordinaten verwenden.
        if cache_key in self.geocode_cache:
            return self.geocode_cache[cache_key]

        # Nominatim: maximal eine Anfrage pro Sekunde.
        wait = 1.1 - (time.time() - self.last_geocode)

        if wait > 0:
            time.sleep(wait)

        params = {
            "q": full_address,
            "format": "jsonv2",
            "limit": 1
        }

        # Länderkennung als zusätzliche Suchbegrenzung.
        if land:
            params["countrycodes"] = str(land).lower()

        url = (
            "https://nominatim.openstreetmap.org/search?"
            + urllib.parse.urlencode(params)
        )

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "SwissGroceriesGeofence/1.0"
            }
        )

        try:

            self.last_geocode = time.time()

            with urllib.request.urlopen(request, timeout=15) as response:

                result = json.loads(
                    response.read().decode("utf-8")
                )

            if not result:

                self.log(
                    f"Keine GPS-Koordinaten gefunden: {full_address}",
                    level="WARNING"
                )

                return None

            coordinates = {
                "latitude": float(result[0]["lat"]),
                "longitude": float(result[0]["lon"])
            }

            # Koordinaten lokal zwischenspeichern.
            self.geocode_cache[cache_key] = coordinates

            self._save_cache()

            self.log(
                f"Adresse geocodiert: {full_address} -> "
                f"{coordinates['latitude']}, "
                f"{coordinates['longitude']}"
            )

            return coordinates

        except Exception as exc:

            self.log(
                f"Geocoding-Fehler ({full_address}): {exc}",
                level="WARNING"
            )

            return None

    # ---------------------------------------------------------
    # Einkaufslisten-Sensoren erkennen
    # ---------------------------------------------------------

    def discover_stores(self):

        states = self.get_state() or {}

        for entity_id, entity in states.items():

            if not entity_id.startswith(self.sensor_prefix):
                continue

            if not isinstance(entity, dict):
                continue

            attributes = entity.get("attributes", {})

            if not isinstance(attributes, dict):
                continue

            address = attributes.get("adresse")
            store_name = attributes.get("geschaeft")
            plz = attributes.get("plz")
            ort = attributes.get("ort")
            land = attributes.get("land", "CH")

            if not address or not store_name or not plz:
                continue

            slug = self.slugify(store_name)
            zip_slug = self.slugify(plz)

            if not slug or not zip_slug:
                continue

            binary_entity = (
                f"{self.entity_prefix}{slug}_{zip_slug}"
            )

            address_key = f"{address}|{plz}|{ort}|{land}"

            existing = self.stores.get(binary_entity)

            if (
                existing
                and existing.get("address_key") == address_key
            ):
                continue

            self.log(
                f"Geschäft erkannt: {store_name} ({plz} {ort})"
            )

            coordinates = self.geocode(
                address,
                plz,
                ort,
                land
            )

            if coordinates is None:
                continue

            self.stores[binary_entity] = {

                "entity_id": entity_id,
                "name": store_name,
                "address": address,
                "plz": str(plz),
                "ort": ort,
                "land": land,
                "address_key": address_key,

                "latitude": coordinates["latitude"],
                "longitude": coordinates["longitude"]
            }

            self.log(
                f"Geofence erstellt: {binary_entity}"
            )

    # ---------------------------------------------------------
    # Companion-Geräte erkennen
    # ---------------------------------------------------------

    def get_trackers(self):

        states = self.get_state() or {}

        trackers = []

        for entity_id, entity in states.items():

            if not entity_id.startswith("device_tracker."):
                continue

            if (
                self.trackers_config
                and entity_id not in self.trackers_config
            ):
                continue

            if not isinstance(entity, dict):
                continue

            attributes = entity.get("attributes", {})

            if not isinstance(attributes, dict):
                continue

            latitude = attributes.get("latitude")
            longitude = attributes.get("longitude")

            if latitude is None or longitude is None:
                continue

            try:

                latitude = float(latitude)
                longitude = float(longitude)

                if not (-90 <= latitude <= 90):
                    continue

                if not (-180 <= longitude <= 180):
                    continue

                accuracy = float(
                    attributes.get("gps_accuracy", 0) or 0
                )

                trackers.append({

                    "entity_id": entity_id,
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": accuracy

                })

            except (ValueError, TypeError):
                continue

        return trackers

    # ---------------------------------------------------------
    # Geofence berechnen und Sensoren aktualisieren
    # ---------------------------------------------------------

    def update(self, kwargs):

        try:

            self.discover_stores()

            trackers = self.get_trackers()

            for entity_id, store in self.stores.items():

                # Individueller Radius pro Geschäft möglich.
                radius_key = (
                    "radius_" + entity_id.split(".")[-1]
                )

                radius = float(
                    self.args.get(radius_key, self.radius)
                )

                distances = []

                for tracker in trackers:

                    distance = self.distance_m(
                        store["latitude"],
                        store["longitude"],
                        tracker["latitude"],
                        tracker["longitude"]
                    )

                    distances.append({

                        "device": tracker["entity_id"],
                        "distance": round(distance, 1),
                        "gps_accuracy": tracker["accuracy"]

                    })

                # ON, sobald mindestens ein Gerät sicher
                # innerhalb des Radius ist.

                inside = any(
                    item["distance"] + item["gps_accuracy"] <= radius
                    for item in distances
                )

                self.set_state(
                    entity_id,
                    state="on" if inside else "off",
                    attributes={

                        "friendly_name": (
                            f"Einkaufsliste bei {store['name']} "
                            f"({store['plz']})"
                        ),

                        "device_class": "presence",

                        "geschaeft": store["name"],
                        "adresse": store["address"],
                        "plz": store["plz"],
                        "ort": store["ort"],
                        "land": store["land"],

                        "latitude": store["latitude"],
                        "longitude": store["longitude"],

                        "radius": radius,
                        "distances": distances,

                        "source_sensor": store["entity_id"]

                    }
                )

        except Exception as exc:

            self.log(
                f"Fehler bei Geofence-Aktualisierung: {exc}",
                level="ERROR"
            )

    # ---------------------------------------------------------
    # Beenden
    # ---------------------------------------------------------

    def terminate(self):

        self._save_cache()

        self.log(
            "Einkaufsliste Geofence beendet."
        )
