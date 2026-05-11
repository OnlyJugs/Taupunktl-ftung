# Projekt-Dokumentation: Taupunktlüftung

Dieses Projekt entsteht auf einem **Raspberry Pi 4B** mit dem **Joy-Pi**-Aufsatz und besteht aus zwei eigenständigen Python-Skripten:

| Datei | Zweck |
|-------|-------|
| [taupunkt.py](taupunkt.py) | Liest Temperatur/Feuchte vom DHT-Sensor, berechnet den Taupunkt, steuert den Lüfter und stellt eine Web-UI bereit. |
| [fan_test.py](fan_test.py) | Interaktiver Test eines Lüfters (oder anderen Aktors) per GPIO inkl. Software-PWM. |

Langfristiges Ziel: anhand der Taupunkt-Differenz zwischen Innen- und Außensensor automatisch einen Lüfter ein- und ausschalten. Aktuell sind beide Bausteine getrennt – Sensorlesung und Aktor-Ansteuerung – und können unabhängig voneinander getestet werden.

---

## 1. [taupunkt.py](taupunkt.py)

### 1.1 Aufgabe

Das Skript liest alle 3 Sekunden Temperatur und relative Luftfeuchte vom Sensor **AM2302 / DHT22** und berechnet daraus den **Taupunkt** in °C nach der Magnus-Formel. Die Ausgabe erfolgt tabellarisch im Terminal.

### 1.2 Hardware-Verdrahtung

- Sensor: DHT22 / AM2302 (DHT11-kompatibler Kernel-Treiber).
- Default: **eingebauter Joy-Pi-DHT auf BCM GPIO 4**.
- Alternativ extern an Joy-Pi-Buchse **SERV01** (Header-Pin 37 = BCM GPIO 26) – dann `--pin 26` mitgeben.
- Bei externem 3-poligem Stecker: antwortet der Sensor nicht, einmal um 180° drehen.

### 1.3 Implementierungsansatz

Statt einer reinen Python-Bitbang-Bibliothek (z. B. `Adafruit_DHT`) wird der **Linux-Kernel-Treiber `dht11`** verwendet. Dieser Treiber funktioniert auch für DHT22/AM2302 und liest die zeitkritischen µs-Pulse direkt im Kernel – auf dem Pi 4 mit Kernel 6.12 deutlich zuverlässiger.

Die Aktivierung erfolgt zur Laufzeit per `dtoverlay`:

```
sudo dtoverlay dht11 gpiopin=4
```

Der Treiber meldet sich danach im **IIO-Subsystem** als Gerät unter `/sys/bus/iio/devices/iio:deviceN`. Werte werden als reine Textdateien gelesen:

- `in_temp_input` – Temperatur in m°C (Tausendstel Grad)
- `in_humidityrelative_input` – relative Feuchte in m%

### 1.4 Code-Aufbau

Das Modul ist bewusst kompakt gehalten (~250 Zeilen). Die wichtigsten Bausteine:

| Symbol | Beschreibung |
|--------|--------------|
| `load_overlay()` / `unload_overlay()` | Laden/Entladen des `dht11`-Overlays, sucht das IIO-Device. |
| `read_sensor()` | Liest Temperatur und Feuchte aus dem IIO-Sysfs, mehrere Versuche bei Lesefehlern. |
| `dewpoint()` | Magnus-Formel mit DWD-Konstanten. |
| `Fan` | Hysterese-Lüftersteuerung auf GPIO 21 mit thread-sicherem Override (`auto` / `on` / `off`). |
| `build_app()` | Flask-App mit `/`, `/api/data`, `/api/settings`. |
| `main()` | CLI, Overlay laden, Web-Thread starten, Messschleife (3 s), CSV-Logging, Cleanup. |

### 1.5 Magnus-Formel

$$
\gamma = \frac{a \cdot T}{b + T} + \ln\!\left(\frac{\mathrm{RH}}{100}\right), \qquad
T_d = \frac{b \cdot \gamma}{a - \gamma}
$$

mit $T$ = Lufttemperatur in °C, $\mathrm{RH}$ = relative Feuchte in %, $T_d$ = Taupunkt in °C.

### 1.6 Start & Beenden

```bash
sudo python3 taupunkt.py            # Default: GPIO 4 (Joy-Pi onboard), Web :8080
sudo python3 taupunkt.py --pin 26   # externer AM2302 an SERV01
sudo python3 taupunkt.py --no-web   # ohne Web-UI
```

`sudo` ist erforderlich, weil `dtoverlay` Root-Rechte verlangt. `STRG+C` beendet das Skript sauber; im `finally`-Zweig wird das Overlay wieder entfernt, sodass der GPIO frei ist.

### 1.7 Beispielausgabe

```
Zeit        T °C   RH %   Td °C
----------------------------------
08:56:02     26.2   33.0     8.65
08:56:10     26.5   30.1     7.56  [FAN]
```

### 1.8 Mögliche Fehlerquellen

- **„Kernel-Treiber 'dht11' nicht aktiv.“** – Overlay nicht geladen (kein `sudo`?), GPIO falsch, Sensor nicht angeschlossen.
- **„Lesefehler.“** – Stecker verdreht, lange Leitung, fehlender Pull-up, oder zu schnelle Abfrage. `read_sensor` versucht es mehrfach.

---

## 2. [fan_test.py](fan_test.py)

### 2.1 Aufgabe

Interaktives Kommandozeilenwerkzeug, um auf einem GPIO-Pin gefahrlos die Reaktion eines Aktors (typischerweise eines Lüfters über Treiberstufe) zu testen: statisches Pegelumschalten, Kurzpuls und Software-PWM-Sweep. Beim Beenden wird der Pin garantiert auf LOW gesetzt und freigegeben.

### 2.2 Hardware

- Joy-Pi-Buchse **SERV02** (Header-Pin 22).
- Im Code aktuell konfiguriert: [PIN = 21](fan_test.py#L24) (BCM 21). Der Header-Kommentar nennt GPIO 25 / Pin 22 – die Konstante `PIN` ist die maßgebliche Quelle und sollte ggf. an die tatsächliche Verdrahtung angepasst werden.
- **Warnung im Skript-Kommentar**: Falls statt des Lüfters der Buzzer reagiert, sofort `STRG+C` drücken – der Pin wird dann sicher LOW geschaltet.

### 2.3 GPIO-Bibliothek

Verwendet wird die moderne Userspace-API **`libgpiod` v2** (Python-Binding `gpiod`) über das Zeichengerät [/dev/gpiochip0](fan_test.py#L23). Die alte `RPi.GPIO`-Bibliothek funktioniert auf Kernel 6.12 / Pi 5 ohnehin nicht mehr zuverlässig.

Eine Linie wird als Output mit Startwert `INACTIVE` (LOW) angefordert:

```python
gpiod.request_lines(
    CHIP,
    consumer="fan-test",
    config={PIN: gpiod.LineSettings(direction=Direction.OUTPUT,
                                    output_value=Value.INACTIVE)},
)
```

### 2.4 Code-Aufbau

| Funktion | Beschreibung |
|----------|--------------|
| [request_output()](fan_test.py#L27) | Fordert die GPIO-Linie als Output-LOW an und gibt das `Request`-Objekt zurück. |
| [safe_off()](fan_test.py#L36) | Setzt den Pin auf LOW und gibt ihn frei – Exceptions werden geschluckt, damit der Cleanup nie scheitert. |
| [soft_pwm()](fan_test.py#L46) | Software-PWM per `time.sleep`. Parameter: Tastgrad `duty` (0–1), Frequenz `freq_hz`, Dauer `seconds`. Bei 25 kHz ist die Periode 40 µs – bedingt durch Python/Scheduling ist die Auflösung dort grob, reicht für einen funktionalen Test aber aus. |
| [main()](fan_test.py#L60) | Registriert `SIGINT`/`SIGTERM`-Handler (`bye`), liest in einer Endlos-Schleife Kommandos ein und ruft die jeweilige Aktion auf. Im `finally` wird `safe_off` ausgeführt. |

### 2.5 Bedienung

| Eingabe | Wirkung |
|---------|---------|
| `ENTER` (leer) | Pegel umschalten LOW ↔ HIGH |
| `p` | Einzelpuls 200 ms HIGH, danach LOW |
| `s` | PWM-Sweep @ 25 kHz: 25 % → 50 % → 75 % → 100 %, je 1 s |
| `q` / `quit` / `exit` | Beenden |
| `STRG+C` | Sofortiges sicheres Beenden (Pin LOW, Freigabe) |

### 2.6 Sicherheits-Konzept

- Output-Wert wird **bei der Anforderung** schon auf `INACTIVE` gesetzt, damit der Pin nicht kurzzeitig undefiniert ist.
- Die Signal-Handler [bye()](fan_test.py#L65) rufen `safe_off` auf und beenden mit `sys.exit(0)`.
- `try/finally` in `main` garantiert auch bei Exceptions oder `EOFError` (gepiped stdin) den LOW-Zustand.

---

## 3. Zusammenspiel und Ausblick

Sensorlesung und Lüftersteuerung laufen gemeinsam in [taupunkt.py](taupunkt.py).
Zusätzlich startet das Skript einen kleinen **Flask-Webserver** (Default-Port 8080)
mit Live-Anzeige der aktuellen Messwerte (Aktualisierung jede Sekunde via
`fetch('/api/data')`). Die Web-UI erlaubt außerdem, die Hysterese-Schwellen
($T_{d,\text{on}}$, $T_{d,\text{off}}$) zur Laufzeit zu ändern und den Lüfter
manuell `EIN` / `AUS` zu schalten bzw. wieder in den Automatik-Modus zu versetzen.

### Endpunkte

| Endpoint | Methode | Zweck |
|----------|---------|-------|
| `/` | GET | HTML-Oberfläche mit Live-Daten und Override-Formular |
| `/api/data` | GET | JSON: `{ last, fan, s }` (letzte Messung, Lüfterstatus, Settings) |
| `/api/settings` | POST | JSON-Body mit `td_on`, `td_off`, `mode` (`auto`/`on`/`off`) |

### Weiter offen

1. Zwei Sensoren (innen/außen) parallel auslesen und die Differenz
   $\Delta T_d$ statt eines absoluten Schwellwerts auswerten.
2. Optional: MQTT-Anbindung, persistente Speicherung der Settings
   über Programmstarts hinweg.

## 4. Abhängigkeiten

- Python ≥ 3.10 (wegen `from __future__ import annotations` und Union-Syntax `str | None`)
- Systempakete: `device-tree-compiler`/`dtoverlay` (in Raspberry-Pi-OS enthalten)
- Python-Pakete: [gpiod](fan_test.py#L20) (`pip install gpiod` – v2-API), [flask](taupunkt.py) (`pip install flask`, optional – ohne Flask läuft `taupunkt.py` ohne Web-UI weiter)
- Kernel-Modul: `dht11` (Standard im Raspberry-Pi-OS-Kernel)

## 5. Dateiübersicht

- [taupunkt.py](taupunkt.py) – Sensorlesung + Taupunktberechnung + Lüftersteuerung + Web-UI
- [fan_test.py](fan_test.py) – GPIO-/PWM-Test
- [README.md](README.md) – Kurzanleitung (Hardware, Start, Magnus-Formel)
- [DOCUMENTATION.md](DOCUMENTATION.md) – diese ausführliche Dokumentation
