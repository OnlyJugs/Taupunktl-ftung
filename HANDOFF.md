# Handoff — wo wir aufgehört haben (2026-05-12)

## Status
- `taupunkt.py`: **neu, dual-sensor + diff-Modus**. Läuft auf Windows nicht
  (gpiod fehlt), wird auf dem Pi getestet.
- `web_preview.py`: an dual-sensor angepasst, Windows-Vorschau funktioniert.
- **Noch nicht committed.** Letzter Commit: `3ae4d73` (.gitignore).
- README.md / DOCUMENTATION.md sind noch auf single-sensor Stand.

## Hardware
- Pi 4B + Joy-Pi
- DHT22 intern: BCM **GPIO 4** (Joy-Pi onboard)  → Referenz
- DHT22 extern: BCM **GPIO 26** (SERV01)         → zu lüftender Raum
- Lüfter: BCM **GPIO 21**, active-low (LOW = AN)

## Was die neue Logik macht
Default-Modus `diff`:
- Lüfter EIN wenn `t_ext - t_int >= diff_on` (default 1.0 °C)
- Lüfter AUS wenn `t_ext - t_int <= diff_off` (default 0.0 °C)
- Hysterese + `FAN_MIN_HOLD_S = 1.0` s gegen Flattern
- Override-Modi `on` / `off` weiterhin verfügbar (Web-UI Select)

CLI:
```
sudo python3 taupunkt.py [--pin 4] [--pin-ext 26] [--no-ext]
                         [--port 8080] [--no-web]
```

## Offene Punkte / TODO in der Schule
1. **Auf dem Pi testen ob `dtoverlay dht11 gpiopin=26` ein zweites IIO-Device
   erzeugt, obwohl bereits eines für GPIO 4 geladen ist.**
   - Falls JA → fertig, einfach `sudo python3 taupunkt.py` starten.
   - Falls NEIN (Overlay nur einmal ladbar) → Plan B nötig:
     - Variante a) Custom DT-Overlay schreiben mit zwei dht-Nodes.
     - Variante b) Zweiten Sensor via Userspace lesen (`adafruit_dht` mit
       `lgpio` Backend, dann beide Sensoren über die gleiche Bibliothek).
2. Web-UI live auf dem Pi gegenchecken (Layout, ΔT-Anzeige, Mode-Wechsel).
3. README.md + DOCUMENTATION.md auf dual-sensor / `--pin-ext` / Diff-Modus
   updaten (Quick-Start, CLI-Tabelle, CSV-Header).
4. **Commit machen** (logisch zusammengefasst: "Dual-sensor support with
   differential fan control"). Pushen erst auf Zuruf.

## Schnelltest Windows-Vorschau
```
cd "d:\Laika Ivanova\Storage\Workshop\Misc\Raspberry"
py web_preview.py
# Browser: http://localhost:8080/
```
Interner Sensor pendelt um 21 °C, externer ±2 °C drumherum, sodass im
Automatik-Modus der Lüfter ca. einmal pro Minute schaltet.

## Schnelltest Pi
```
sudo python3 taupunkt.py
# Browser: http://<pi-ip>:8080/
# STRG+C zum Beenden – Overlay wird sauber entfernt.
```

## Datei-Diff seit letztem Commit
- `taupunkt.py` — komplett umgeschrieben (zwei Sensoren, diff-Modus, neue UI,
  neuer CSV-Header).
- `web_preview.py` — Mock für zwei Sensoren + MockFan mit diff-Modus.

## Wichtige Defaults (in `taupunkt.py` oben)
```
DHT_PIN_INT      = 4
DHT_PIN_EXT      = 26
FAN_PIN          = 21
FAN_ACTIVE_LOW   = True
INTERVAL_S       = 3.0
FAN_MIN_HOLD_S   = 1.0
DEFAULT_DIFF_ON  = 1.0
DEFAULT_DIFF_OFF = 0.0
WEB_HOST, WEB_PORT = "0.0.0.0", 8080
```

## CSV-Schema (neu)
`timestamp, iso_time, t_int, h_int, td_int, t_ext, h_ext, td_ext, fan_on`

Alte Logs (`tp_data/tp_2026-05-05.csv`) haben das alte single-sensor-Schema
und werden nicht migriert.
