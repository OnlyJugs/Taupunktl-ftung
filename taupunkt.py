#!/usr/bin/env python3
"""Taupunktlüftung: liest DHT22/AM2302, berechnet Taupunkt, steuert Lüfter
und bietet eine Live-Web-UI mit Override.

Standard-Sensor: BCM GPIO 4 (Joy-Pi onboard).  Lüfter: BCM GPIO 21.
Start:  sudo python3 taupunkt.py [--pin 4] [--port 8080] [--no-web]
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import gpiod
from gpiod.line import Bias, Direction, Value
from flask import Flask, jsonify, request

# ---------- Konfiguration ---------------------------------------------------
DHT_PIN        = 4               # BCM GPIO des DHT-Sensors
INTERVAL_S     = 3.0             # Sensor-Takt
LOG_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tp_data")

FAN_CHIP       = "/dev/gpiochip0"
FAN_PIN        = 21              # BCM GPIO 21 (Header-Pin 40)
FAN_ACTIVE_LOW = True            # Treiberstufe: LOW = an
FAN_MIN_HOLD_S = 1.0             # min. Lauf-/Pausezeit
DEFAULT_TD_ON  = 15.0
DEFAULT_TD_OFF = 10.0

WEB_HOST, WEB_PORT = "0.0.0.0", 8080


# ---------- DHT via Kernel-Overlay ------------------------------------------
def sh(cmd: list[str], check: bool = True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_overlay(pin: int) -> str:
    sh(["sudo", "dtoverlay", "-r", "dht11"], check=False)
    time.sleep(0.3)
    sh(["sudo", "dtoverlay", "dht11", f"gpiopin={pin}"])
    for _ in range(25):
        for d in glob.glob("/sys/bus/iio/devices/iio:device*"):
            try:
                with open(os.path.join(d, "name")) as f:
                    if f.read().startswith("dht"):
                        return d
            except OSError:
                pass
        time.sleep(0.2)
    raise RuntimeError("Kernel-Treiber 'dht11' nicht aktiv (sudo nötig?).")


def unload_overlay() -> None:
    sh(["sudo", "dtoverlay", "-r", "dht11"], check=False)


def read_sensor(dev: str, tries: int = 4) -> tuple[float, float] | None:
    """Liest (T °C, RH %). Bis zu ``tries`` Versuche – AM2302 ist sporadisch."""
    for i in range(tries):
        try:
            with open(os.path.join(dev, "in_temp_input")) as f:
                t = int(f.read()) / 1000.0
            with open(os.path.join(dev, "in_humidityrelative_input")) as f:
                h = int(f.read()) / 1000.0
            return t, h
        except (OSError, ValueError):
            if i < tries - 1:
                time.sleep(2.2)
    return None


def dewpoint(t: float, rh: float) -> float:
    """Taupunkt °C nach Magnus (DWD-Konstanten)."""
    a, b = 17.62, 243.12
    g = a * t / (b + t) + math.log(max(rh, 1e-3) / 100.0)
    return b * g / (a - g)


# ---------- Lüfter -----------------------------------------------------------
class Fan:
    """Hysterese-Steuerung mit Manuell-Override (auto/on/off)."""

    def __init__(self) -> None:
        self.on = False
        self.last_change = 0.0
        self.td_on = DEFAULT_TD_ON
        self.td_off = DEFAULT_TD_OFF
        self.mode = "auto"        # "auto" | "on" | "off"
        self.lock = threading.Lock()
        self._off = Value.ACTIVE if FAN_ACTIVE_LOW else Value.INACTIVE
        self._on = Value.INACTIVE if FAN_ACTIVE_LOW else Value.ACTIVE
        self.req = gpiod.request_lines(
            FAN_CHIP, consumer="taupunkt-fan",
            config={FAN_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=self._off,
                bias=Bias.PULL_UP if FAN_ACTIVE_LOW else Bias.PULL_DOWN)},
        )

    def _set(self, on: bool) -> None:
        self.req.set_value(FAN_PIN, self._on if on else self._off)
        self.on = on
        self.last_change = time.monotonic()

    def update(self, td: float) -> None:
        with self.lock:
            mode, on_th, off_th = self.mode, self.td_on, self.td_off
        if mode == "on" and not self.on:
            self._set(True)
        elif mode == "off" and self.on:
            self._set(False)
        elif mode == "auto":
            held = time.monotonic() - self.last_change
            if not self.on and td >= on_th and held >= FAN_MIN_HOLD_S:
                self._set(True)
            elif self.on and td <= off_th and held >= FAN_MIN_HOLD_S:
                self._set(False)

    def settings(self) -> dict:
        with self.lock:
            return {"td_on": self.td_on, "td_off": self.td_off, "mode": self.mode}

    def configure(self, td_on=None, td_off=None, mode=None) -> None:
        with self.lock:
            if td_on is not None:
                self.td_on = float(td_on)
            if td_off is not None:
                self.td_off = float(td_off)
            if mode is not None:
                if mode not in ("auto", "on", "off"):
                    raise ValueError(f"invalid mode: {mode}")
                self.mode = mode
            if self.td_off > self.td_on:
                self.td_off, self.td_on = self.td_on, self.td_off

    def shutdown(self) -> None:
        try:
            self.req.set_value(FAN_PIN, self._off)
            self.req.release()
        except Exception:
            pass


# ---------- Web-UI -----------------------------------------------------------
INDEX_HTML = """<!doctype html><html lang="de"><meta charset="utf-8">
<title>Taupunktlüftung</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;max-width:640px;margin:1.5rem auto;padding:0 1rem}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem 1.5rem;margin:1rem 0}
 .v{font-size:1.6rem;font-weight:600}.l{opacity:.7;font-size:.85rem}
 .fan{padding:.4rem .8rem;border-radius:.5rem;color:#fff;font-weight:600}
 .fan.on{background:#2e7d32}.fan.off{background:#555}
 input[type=number]{width:6rem}label{display:block;margin:.35rem 0}
 button{padding:.45rem .9rem;margin-right:.5rem}
</style>
<h1>Taupunktlüftung</h1><div class=l id=sub>warte auf Messwert…</div>
<div class=grid>
 <div><div class=l>Temperatur</div><div class=v id=t>–</div></div>
 <div><div class=l>Luftfeuchte</div><div class=v id=h>–</div></div>
 <div><div class=l>Taupunkt</div><div class=v id=td>–</div></div>
 <div><div class=l>Lüfter</div><div class=v><span id=fan class="fan off">–</span></div></div>
</div>
<fieldset><legend>Einstellungen / Override</legend><form id=f>
 <label>Schwelle EIN (°C): <input type=number step=0.1 name=td_on id=td_on></label>
 <label>Schwelle AUS (°C): <input type=number step=0.1 name=td_off id=td_off></label>
 <label>Modus:
  <select name=mode id=mm>
   <option value=auto>Automatik</option>
   <option value=on>manuell EIN</option>
   <option value=off>manuell AUS</option>
  </select></label>
 <button>Speichern</button>
 <button type=button id=auto>Auf Automatik</button>
</form></fieldset>
<p class=l>Aktualisierung jede Sekunde. Sensor-Takt: 3 s.</p>
<script>
const $=id=>document.getElementById(id);
const set=(id,v)=>{const e=$(id);if(document.activeElement!==e)e.value=v};
async function refresh(){
 try{
  const d=await(await fetch('/api/data',{cache:'no-store'})).json();
  $('sub').textContent=d.last?'Letzte Messung: '+d.last.iso:'warte auf Messwert…';
  if(d.last){$('t').textContent=d.last.t.toFixed(1)+' °C';
   $('h').textContent=d.last.h.toFixed(1)+' %';
   $('td').textContent=d.last.td.toFixed(2)+' °C';}
  const f=$('fan');f.textContent=d.fan?'EIN':'AUS';f.className='fan '+(d.fan?'on':'off');
  set('td_on',d.s.td_on);set('td_off',d.s.td_off);
  if(document.activeElement!==$('mm'))$('mm').value=d.s.mode;
 }catch(e){$('sub').textContent='Verbindung verloren…';}
}
$('f').onsubmit=async e=>{e.preventDefault();
 await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});refresh();};
$('auto').onclick=async()=>{await fetch('/api/settings',{method:'POST',
 headers:{'Content-Type':'application/json'},body:'{"mode":"auto"}'});refresh();};
refresh();setInterval(refresh,1000);
</script></html>"""


def build_app(state: dict, fan: Fan) -> Flask:
    app = Flask(__name__)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def _():
        return INDEX_HTML

    @app.get("/api/data")
    def _data():
        return jsonify({"last": state.get("last"), "fan": fan.on, "s": fan.settings()})

    @app.post("/api/settings")
    def _settings():
        data = request.get_json(silent=True) or request.form.to_dict()
        try:
            fan.configure(td_on=data.get("td_on") or None,
                          td_off=data.get("td_off") or None,
                          mode=data.get("mode") or None)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "s": fan.settings()})

    return app


# ---------- Hauptprogramm ---------------------------------------------------
def open_log() -> tuple[object, csv.writer]:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"tp_{datetime.now().strftime('%Y-%m-%d')}.csv")
    new = not os.path.exists(path)
    f = open(path, "a", newline="", buffering=1)
    w = csv.writer(f)
    if new:
        w.writerow(["timestamp", "iso_time", "temp_c", "humidity_pct", "dewpoint_c", "fan_on"])
    print(f"Logfile: {path}")
    return f, w


def main() -> int:
    ap = argparse.ArgumentParser(description="Taupunktlüftung mit Web-UI")
    ap.add_argument("--pin", type=int, default=DHT_PIN)
    ap.add_argument("--port", type=int, default=WEB_PORT)
    ap.add_argument("--host", default=WEB_HOST)
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()

    print(f"Lade dht11-Overlay auf GPIO {args.pin} …")
    try:
        dev = load_overlay(args.pin)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    print(f"Sensor-Device: {dev}\nSTRG+C zum Beenden.\n")

    log_f, log_w = open_log()
    fan = Fan()
    state: dict = {}

    if not args.no_web:
        app = build_app(state, fan)
        threading.Thread(
            target=lambda: app.run(host=args.host, port=args.port,
                                   debug=False, use_reloader=False, threaded=True),
            name="web", daemon=True,
        ).start()
        print(f"Web-UI: http://{args.host}:{args.port}/")

    print(f"{'Zeit':<10} {'T °C':>6} {'RH %':>6} {'Td °C':>7}")
    print("-" * 34)
    try:
        while True:
            t0 = time.monotonic()
            werte = read_sensor(dev)
            now = time.time()
            iso = datetime.fromtimestamp(now).isoformat(timespec="seconds")
            if werte is None:
                print(f"{iso[11:]:<10}  Lesefehler.")
            else:
                t, h = werte
                td = dewpoint(t, h)
                fan.update(td)
                state["last"] = {"ts": now, "iso": iso, "t": t, "h": h, "td": td}
                log_w.writerow([f"{now:.0f}", iso, f"{t:.2f}", f"{h:.2f}",
                                f"{td:.2f}", "1" if fan.on else "0"])
                tag = "[FAN]" if fan.on else "     "
                print(f"{iso[11:]:<10} {t:>6.1f} {h:>6.1f} {td:>7.2f}  {tag}")
            time.sleep(max(0.0, INTERVAL_S - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        fan.shutdown()
        log_f.close()
        unload_overlay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
