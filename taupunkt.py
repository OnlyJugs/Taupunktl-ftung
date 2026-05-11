#!/usr/bin/env python3
"""
Taupunktlüftung – liest Temperatur und Luftfeuchte von einem AM2302/DHT22
(Standardpin: BCM GPIO 26 / Joy-Pi-Stecker SERV01, Header-Pin 37) und
schaltet einen Lüfter (GPIO 21) per Hysterese.

Zusätzlich:
  * CSV-Logging in tp_data/tp_YYYY-MM-DD.csv (eine Zeile pro Messung)
  * gleitende Mittelwerte über 1 / 5 / 15 Minuten (Temp, Feuchte, Taupunkt)
  * Min/Max-Anzeige seit Programmstart
  * eingebauter Web-Server (Flask) mit Live-Anzeige (Update jede Sekunde)
    und Override-Formular (Schwellen + Manuell An/Aus)

Implementierung: nutzt den Linux-Kernel-Treiber `dht11` (funktioniert für
DHT11/DHT22/AM2302) über das Device-Tree-Overlay `dht11`.

Start (sudo nur für `dtoverlay` nötig):
    sudo python3 taupunkt.py                  # GPIO 26, Web auf :8080
    sudo python3 taupunkt.py --pin 4          # interner Joy-Pi-DHT
    sudo python3 taupunkt.py --no-web         # ohne Webserver
STRG+C beendet das Programm und entlädt das Overlay wieder.
"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

try:
    import gpiod
    from gpiod.line import Bias, Direction, Value
    _HAS_GPIOD = True
except ImportError:  # pragma: no cover
    _HAS_GPIOD = False

GPIO_PIN   = 26    # Default: SERV01 = BCM GPIO 26 (per CLI --pin überschreibbar)
INTERVAL_S = 3.0
LOG_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tp_data")

# Fenstergrößen für gleitende Mittelwerte (in Sekunden)
WINDOWS_S = (60, 300, 900)  # 1 min, 5 min, 15 min

# ---------- Lüftersteuerung -------------------------------------------------
FAN_CHIP        = "/dev/gpiochip0"
FAN_PIN         = 21      # BCM GPIO 21 (Header-Pin 40)
FAN_ACTIVE_LOW  = True    # Treiberstufe schaltet bei LOW ein -> invertieren
FAN_TD_ON       = 15.0    # Default-Schwelle EIN (°C); zur Laufzeit über Web änderbar
FAN_TD_OFF      = 10.0    # Default-Schwelle AUS (°C); zur Laufzeit über Web änderbar
FAN_MIN_RUN_S   = 1       # Mindest-Laufzeit, sobald eingeschaltet
FAN_MIN_PAUSE_S = 1       # Mindest-Pause nach dem Ausschalten
FAN_AVG_WINDOW  = 0       # 0 = ungeglättet, sonst Sekunden für Mittelwert

# ---------- Web-UI ----------------------------------------------------------
WEB_HOST       = "0.0.0.0"
WEB_PORT       = 8080


def sh(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_overlay(pin: int) -> None:
    sh(["sudo", "dtoverlay", "-r", "dht11"], check=False)
    time.sleep(0.3)
    sh(["sudo", "dtoverlay", "dht11", f"gpiopin={pin}"])
    for _ in range(25):
        if find_iio_device():
            return
        time.sleep(0.2)
    raise RuntimeError("Kernel-Treiber 'dht11' wurde nicht aktiv.")


def unload_overlay() -> None:
    sh(["sudo", "dtoverlay", "-r", "dht11"], check=False)


def find_iio_device() -> str | None:
    for d in glob.glob("/sys/bus/iio/devices/iio:device*"):
        try:
            with open(os.path.join(d, "name")) as f:
                if f.read().startswith("dht11"):
                    return d
        except OSError:
            continue
    return None


def read_sensor(dev: str) -> tuple[float, float] | None:
    """Liefert (Temperatur °C, rel. Feuchte %) oder None bei Lesefehler."""
    try:
        with open(os.path.join(dev, "in_temp_input")) as f:
            t_milli = int(f.read().strip())
        with open(os.path.join(dev, "in_humidityrelative_input")) as f:
            h_milli = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return t_milli / 1000.0, h_milli / 1000.0


def read_sensor_retry(dev: str, tries: int = 4, pause: float = 2.2) -> tuple[float, float] | None:
    """Bis zu `tries` Versuche – AM2302 liefert sporadisch keine Antwort."""
    for i in range(tries):
        werte = read_sensor(dev)
        if werte is not None:
            return werte
        if i < tries - 1:
            time.sleep(pause)
    return None


def taupunkt(temp_c: float, rel_humidity: float) -> float:
    """Taupunkt in °C nach Magnus-Formel (Konstanten nach DWD)."""
    a, b = 17.62, 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(max(rel_humidity, 1e-3) / 100.0)
    return (b * gamma) / (a - gamma)


# ---------- Logging & Statistik ---------------------------------------------

class RollingStats:
    """Hält Messpunkte (timestamp, t, h, td) und liefert gleitende Mittel."""

    def __init__(self, max_window_s: int) -> None:
        self.max_window_s = max_window_s
        self.buf: deque[tuple[float, float, float, float]] = deque()
        self.t_min = self.t_max = None
        self.h_min = self.h_max = None
        self.td_min = self.td_max = None

    def add(self, ts: float, t: float, h: float, td: float) -> None:
        self.buf.append((ts, t, h, td))
        # alte Werte droppen
        cutoff = ts - self.max_window_s
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()
        # Min/Max global aktualisieren
        self.t_min  = t  if self.t_min  is None else min(self.t_min,  t)
        self.t_max  = t  if self.t_max  is None else max(self.t_max,  t)
        self.h_min  = h  if self.h_min  is None else min(self.h_min,  h)
        self.h_max  = h  if self.h_max  is None else max(self.h_max,  h)
        self.td_min = td if self.td_min is None else min(self.td_min, td)
        self.td_max = td if self.td_max is None else max(self.td_max, td)

    def avg(self, window_s: int, now: float) -> tuple[float, float, float, int] | None:
        cutoff = now - window_s
        rows = [(t, h, td) for (ts, t, h, td) in self.buf if ts >= cutoff]
        if not rows:
            return None
        n = len(rows)
        return (sum(r[0] for r in rows) / n,
                sum(r[1] for r in rows) / n,
                sum(r[2] for r in rows) / n,
                n)


class FanController:
    """Hysterese-Steuerung für den Lüfter auf BCM GPIO 21.

    Schaltet EIN, sobald der Td-Wert >= ``td_on``; schaltet AUS,
    sobald er <= ``td_off`` fällt. Mindest-Lauf- und Pausenzeiten
    verhindern Klappern.

    Über ``manual_mode`` kann der Automatikbetrieb übersteuert werden:
        "auto" – Hysterese aktiv (Default)
        "on"   – Lüfter zwangsweise EIN
        "off"  – Lüfter zwangsweise AUS

    Bei fehlendem gpiod-Modul läuft der Controller im Dry-Run-Modus
    und gibt nur Statuswechsel auf der Konsole aus.
    """

    def __init__(self) -> None:
        self.on = False
        self.last_change = 0.0
        self.req = None
        # Laufzeit-Einstellungen (vom Web-UI änderbar)
        self.td_on = FAN_TD_ON
        self.td_off = FAN_TD_OFF
        self.manual_mode = "auto"   # "auto" | "on" | "off"
        self.lock = threading.Lock()
        # "Aus"-Pegel je nach Verdrahtung (active-low -> HIGH = aus)
        self._off_value = Value.ACTIVE if FAN_ACTIVE_LOW else Value.INACTIVE
        self._on_value  = Value.INACTIVE if FAN_ACTIVE_LOW else Value.ACTIVE
        if _HAS_GPIOD:
            try:
                self.req = gpiod.request_lines(
                    FAN_CHIP,
                    consumer="taupunkt-fan",
                    config={FAN_PIN: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=self._off_value,
                        bias=Bias.PULL_UP if FAN_ACTIVE_LOW else Bias.PULL_DOWN,
                    )},
                )
                polarity = "active-low" if FAN_ACTIVE_LOW else "active-high"
                print(f"Lüftersteuerung aktiv auf GPIO {FAN_PIN} ({polarity}, "
                      f"EIN > {FAN_TD_ON:.1f} °C, AUS < {FAN_TD_OFF:.1f} °C).")
            except Exception as e:  # pragma: no cover
                print(f"Konnte GPIO {FAN_PIN} nicht anfordern: {e}", file=sys.stderr)
                self.req = None
        else:
            print("gpiod nicht verfügbar – Lüftersteuerung im Dry-Run.")

    def _set(self, on: bool) -> None:
        if self.req is None:
            return
        try:
            self.req.set_value(FAN_PIN, self._on_value if on else self._off_value)
        except Exception as e:  # pragma: no cover
            print(f"GPIO-Schreibfehler: {e}", file=sys.stderr)

    def update(self, td_avg: float, now: float) -> str | None:
        """Liefert eine Statusmeldung bei Schaltvorgang, sonst None."""
        with self.lock:
            mode = self.manual_mode
            td_on = self.td_on
            td_off = self.td_off

        # Manueller Override
        if mode == "on" and not self.on:
            self.on = True
            self.last_change = now
            self._set(True)
            return "FAN ON  (manuell)"
        if mode == "off" and self.on:
            self.on = False
            self.last_change = now
            self._set(False)
            return "FAN OFF (manuell)"
        if mode != "auto":
            return None

        # Automatik mit Hysterese
        since = now - self.last_change
        if not self.on and td_avg >= td_on and since >= FAN_MIN_PAUSE_S:
            self.on = True
            self.last_change = now
            self._set(True)
            return f"FAN ON  (Td⌀={td_avg:.2f} °C ≥ {td_on:.1f} °C)"
        if self.on and td_avg <= td_off and since >= FAN_MIN_RUN_S:
            self.on = False
            self.last_change = now
            self._set(False)
            return f"FAN OFF (Td⌀={td_avg:.2f} °C ≤ {td_off:.1f} °C)"
        return None

    def settings(self) -> dict:
        with self.lock:
            return {
                "td_on": self.td_on,
                "td_off": self.td_off,
                "manual_mode": self.manual_mode,
            }

    def apply_settings(self, td_on: float | None = None,
                       td_off: float | None = None,
                       manual_mode: str | None = None) -> None:
        with self.lock:
            if td_on is not None:
                self.td_on = float(td_on)
            if td_off is not None:
                self.td_off = float(td_off)
            if manual_mode is not None:
                if manual_mode not in ("auto", "on", "off"):
                    raise ValueError(f"invalid manual_mode: {manual_mode}")
                self.manual_mode = manual_mode
            if self.td_off > self.td_on:
                # Sicherheits-Tausch falls Nutzer Schwellen verkehrt einträgt
                self.td_off, self.td_on = self.td_on, self.td_off

    def shutdown(self) -> None:
        if self.req is None:
            return
        try:
            self.req.set_value(FAN_PIN, self._off_value)
            time.sleep(0.02)
            self.req.reconfigure_lines(
                {FAN_PIN: gpiod.LineSettings(
                    direction=Direction.INPUT,
                    bias=Bias.PULL_UP if FAN_ACTIVE_LOW else Bias.PULL_DOWN,
                )}
            )
            self.req.release()
        except Exception:
            pass
        self.req = None


def open_logfile() -> tuple[object, csv.writer]:
    """Öffnet (oder legt an) tp_data/tp_YYYY-MM-DD.csv und schreibt ggf. Header."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"tp_{datetime.now().strftime('%Y-%m-%d')}.csv")
    new_file = not os.path.exists(path)
    f = open(path, "a", newline="", buffering=1)  # line-buffered
    w = csv.writer(f)
    if new_file:
        w.writerow(["timestamp", "iso_time", "temp_c", "humidity_pct",
                    "dewpoint_c", "fan_on"])
    print(f"Logfile: {path}")
    return f, w


# ---------- Shared State + Web-Server ---------------------------------------

class SharedState:
    """Thread-sicherer Container für die aktuellen Messwerte."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: dict | None = None
        self.history: deque[dict] = deque(maxlen=600)  # ca. 10 min @ 1 Hz
        self.started_at = time.time()

    def update(self, payload: dict) -> None:
        with self.lock:
            self.last = payload
            self.history.append(payload)

    def snapshot(self) -> dict | None:
        with self.lock:
            return dict(self.last) if self.last else None

    def recent(self, n: int = 120) -> list[dict]:
        with self.lock:
            return list(self.history)[-n:]


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Taupunktlüftung</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { font-family: system-ui, sans-serif; color-scheme: light dark; }
  body { max-width: 720px; margin: 1.5rem auto; padding: 0 1rem; }
  h1 { margin-bottom: .25rem; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: .75rem 1.5rem; margin: 1rem 0; }
  .v { font-size: 1.6rem; font-weight: 600; }
  .label { opacity: .7; font-size: .85rem; }
  .fan { padding: .4rem .8rem; border-radius: .5rem; display: inline-block; font-weight: 600; }
  .fan.on  { background: #2e7d32; color: #fff; }
  .fan.off { background: #555; color: #fff; }
  fieldset { margin-top: 1rem; }
  label { display: block; margin: .35rem 0; }
  input[type=number] { width: 6rem; }
  button { padding: .45rem .9rem; margin-right: .5rem; }
  small.err { color: #c62828; }
</style>
</head>
<body>
  <h1>Taupunktlüftung</h1>
  <div class="label" id="sub">warte auf Messwert…</div>

  <div class="grid">
    <div><div class="label">Temperatur</div><div class="v" id="t">–</div></div>
    <div><div class="label">Luftfeuchte</div><div class="v" id="h">–</div></div>
    <div><div class="label">Taupunkt</div><div class="v" id="td">–</div></div>
    <div><div class="label">Lüfter</div><div class="v"><span id="fan" class="fan off">–</span></div></div>
  </div>

  <fieldset>
    <legend>Einstellungen / Override</legend>
    <form id="f">
      <label>Schwelle EIN (°C):
        <input type="number" step="0.1" name="td_on" id="td_on">
      </label>
      <label>Schwelle AUS (°C):
        <input type="number" step="0.1" name="td_off" id="td_off">
      </label>
      <label>Modus:
        <select name="manual_mode" id="mm">
          <option value="auto">Automatik (Hysterese)</option>
          <option value="on">manuell EIN</option>
          <option value="off">manuell AUS</option>
        </select>
      </label>
      <button type="submit">Speichern</button>
      <button type="button" id="auto">Auf Automatik</button>
      <small class="err" id="err"></small>
    </form>
  </fieldset>

  <p class="label">Aktualisierung jede Sekunde. Sensor-Takt: 3&nbsp;s.</p>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/data', {cache: 'no-store'});
    const d = await r.json();
    document.getElementById('sub').textContent =
      d.last ? ('Letzte Messung: ' + d.last.iso_time) : 'warte auf Messwert…';
    if (d.last) {
      document.getElementById('t').textContent  = d.last.temp_c.toFixed(1) + ' °C';
      document.getElementById('h').textContent  = d.last.humidity_pct.toFixed(1) + ' %';
      document.getElementById('td').textContent = d.last.dewpoint_c.toFixed(2) + ' °C';
    }
    const f = document.getElementById('fan');
    f.textContent = d.fan_on ? 'EIN' : 'AUS';
    f.className = 'fan ' + (d.fan_on ? 'on' : 'off');
    // Settings nur befüllen, wenn das jeweilige Feld nicht gerade fokussiert ist
    const set = (id, v) => {
      const e = document.getElementById(id);
      if (document.activeElement !== e) e.value = v;
    };
    set('td_on',  d.settings.td_on);
    set('td_off', d.settings.td_off);
    if (document.activeElement !== document.getElementById('mm'))
      document.getElementById('mm').value = d.settings.manual_mode;
  } catch (e) {
    document.getElementById('sub').textContent = 'Verbindung verloren…';
  }
}
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const body = new FormData(ev.target);
  const payload = Object.fromEntries(body.entries());
  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const err = document.getElementById('err');
  err.textContent = r.ok ? '' : ('Fehler: ' + r.status);
  refresh();
});
document.getElementById('auto').addEventListener('click', async () => {
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({manual_mode: 'auto'}),
  });
  refresh();
});
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


def build_app(state: SharedState, fan: FanController):
    """Flask-App für Live-Anzeige und Settings-Override."""
    try:
        from flask import Flask, jsonify, request
    except ImportError as e:
        raise RuntimeError(
            "Flask ist nicht installiert. Installation: "
            "`pip install flask` (oder `sudo apt install python3-flask`)."
        ) from e

    app = Flask(__name__)
    # Flask-Logging leiser stellen – sonst wird die Konsole zugespammt.
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/api/data")
    def api_data():
        return jsonify({
            "last": state.snapshot(),
            "fan_on": fan.on,
            "settings": fan.settings(),
            "history": state.recent(60),
        })

    @app.post("/api/settings")
    def api_settings():
        data = request.get_json(silent=True) or request.form.to_dict()
        try:
            td_on = float(data["td_on"]) if data.get("td_on") not in (None, "") else None
            td_off = float(data["td_off"]) if data.get("td_off") not in (None, "") else None
            mode = data.get("manual_mode") or None
            fan.apply_settings(td_on=td_on, td_off=td_off, manual_mode=mode)
        except (ValueError, KeyError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "settings": fan.settings()})

    return app


def start_web_thread(state: SharedState, fan: FanController,
                     host: str, port: int) -> threading.Thread | None:
    try:
        app = build_app(state, fan)
    except RuntimeError as e:
        print(f"Web-UI deaktiviert: {e}", file=sys.stderr)
        return None

    def run() -> None:
        # use_reloader=False, sonst startet Flask einen Sub-Prozess.
        app.run(host=host, port=port, debug=False, use_reloader=False,
                threaded=True)

    th = threading.Thread(target=run, name="web", daemon=True)
    th.start()
    print(f"Web-UI: http://{host}:{port}/  (über LAN: http://<pi-ip>:{port}/)")
    return th


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Taupunktlüftung mit Web-UI")
    p.add_argument("--pin", type=int, default=GPIO_PIN,
                   help=f"BCM GPIO des DHT-Sensors (Default: {GPIO_PIN})")
    p.add_argument("--port", type=int, default=WEB_PORT,
                   help=f"TCP-Port des Webservers (Default: {WEB_PORT})")
    p.add_argument("--host", default=WEB_HOST,
                   help=f"Bind-Adresse des Webservers (Default: {WEB_HOST})")
    p.add_argument("--no-web", action="store_true",
                   help="Webserver nicht starten")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    pin = args.pin
    print(f"Lade Kernel-Treiber dht11 auf GPIO {pin} …")
    try:
        load_overlay(pin)
    except subprocess.CalledProcessError as e:
        print(f"Konnte Overlay nicht laden: {e.stderr or e}", file=sys.stderr)
        print("Tipp: Skript mit 'sudo' starten.", file=sys.stderr)
        return 1

    dev = find_iio_device()
    if not dev:
        print("Kein iio-Device für dht11 gefunden.", file=sys.stderr)
        unload_overlay()
        return 1

    print(f"Sensor-Device: {dev}")
    print(f"Taupunktlüftung – DHT auf GPIO {pin}.")
    print("STRG+C zum Beenden.\n")

    log_f, log_w = open_logfile()
    stats = RollingStats(max_window_s=max(max(WINDOWS_S), FAN_AVG_WINDOW or 1))
    fan = FanController()
    state = SharedState()
    if not args.no_web:
        start_web_thread(state, fan, args.host, args.port)
    n_samples = 0

    print(f"{'Zeit':<10} {'Temp °C':>8} {'Feuchte %':>10} {'Taupunkt °C':>12}")
    print("-" * 46)

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        while True:
            t0 = time.monotonic()
            werte = read_sensor_retry(dev)
            now_wall = time.time()
            now_str = time.strftime("%H:%M:%S")

            if werte is None:
                print(f"{now_str:<10}  Lesefehler – Sensor antwortet nicht.")
            else:
                t, h = werte
                td = taupunkt(t, h)
                stats.add(now_wall, t, h, td)
                n_samples += 1

                # Td-Wert für die Lüftersteuerung (geglaettet wenn FAN_AVG_WINDOW > 0)
                if FAN_AVG_WINDOW > 0:
                    fan_avg = stats.avg(FAN_AVG_WINDOW, now_wall)
                    td_for_fan = fan_avg[2] if fan_avg else td
                else:
                    td_for_fan = td
                msg = fan.update(td_for_fan, now_wall)

                # CSV-Zeile (mit Lüfterstatus)
                iso = datetime.fromtimestamp(now_wall).isoformat(timespec="seconds")
                log_w.writerow([
                    f"{now_wall:.0f}", iso,
                    f"{t:.2f}", f"{h:.2f}", f"{td:.2f}",
                    "1" if fan.on else "0",
                ])

                # Shared State für die Web-UI aktualisieren
                state.update({
                    "timestamp": now_wall,
                    "iso_time": iso,
                    "temp_c": t,
                    "humidity_pct": h,
                    "dewpoint_c": td,
                    "fan_on": fan.on,
                })

                fan_tag = "[FAN]" if fan.on else "     "
                print(f"{now_str:<10} {t:>8.1f} {h:>10.1f} {td:>12.2f}  {fan_tag}")
                if msg:
                    print(f"  >>> {msg}")

                # Alle 5 Messungen (~15 s) eine Statistikzeile ausgeben
                if n_samples % 5 == 0:
                    parts = []
                    for w_s in WINDOWS_S:
                        a = stats.avg(w_s, now_wall)
                        if a is None:
                            continue
                        avg_t, avg_h, avg_td, k = a
                        label = f"{w_s // 60}min"
                        parts.append(
                            f"{label}(n={k}): T={avg_t:.1f}°C  "
                            f"RH={avg_h:.1f}%  Td={avg_td:.2f}°C"
                        )
                    if parts:
                        print("  ⌀ " + "  |  ".join(parts))
                    print(
                        f"  Min/Max seit Start: "
                        f"T {stats.t_min:.1f}/{stats.t_max:.1f} °C  "
                        f"RH {stats.h_min:.1f}/{stats.h_max:.1f} %  "
                        f"Td {stats.td_min:.2f}/{stats.td_max:.2f} °C"
                    )

            # 3-Sekunden-Takt einhalten, egal wie lang die Retries dauerten
            rest = INTERVAL_S - (time.monotonic() - t0)
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        try:
            fan.shutdown()
        except Exception:
            pass
        try:
            log_f.close()
        except Exception:
            pass
        # Abschluss-Statistik
        if n_samples:
            now_wall = time.time()
            print("\nZusammenfassung:")
            for w_s in WINDOWS_S:
                a = stats.avg(w_s, now_wall)
                if a is None:
                    continue
                avg_t, avg_h, avg_td, k = a
                print(f"  letzte {w_s // 60:>2} min (n={k}): "
                      f"T={avg_t:.2f} °C  RH={avg_h:.2f} %  Td={avg_td:.2f} °C")
            print(f"  Gesamt-Min/Max: "
                  f"T {stats.t_min:.1f}…{stats.t_max:.1f} °C  "
                  f"RH {stats.h_min:.1f}…{stats.h_max:.1f} %  "
                  f"Td {stats.td_min:.2f}…{stats.td_max:.2f} °C")
            print(f"  Gesamtanzahl Messungen: {n_samples}")
        unload_overlay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
