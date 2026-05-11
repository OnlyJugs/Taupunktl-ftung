#!/usr/bin/env python3
"""Taupunktlüftung: liest zwei DHT22/AM2302-Sensoren (intern + extern),
berechnet den Taupunkt, steuert einen Lüfter und bietet eine Live-Web-UI
mit Override.

Standard-Setup:
  intern : BCM GPIO 4   (Joy-Pi onboard)            -> Referenz / Zielwert
  extern : BCM GPIO 26  (SERV01)                    -> der zu lüftende Raum
  Lüfter : BCM GPIO 21  (active-low, Treiberstufe)

Standardmodus ist "diff": Lüfter EIN sobald t_extern - t_intern >= diff_on,
AUS sobald die Differenz <= diff_off. Zusätzlich gibt es Hand-Override
(Lüfter AN / Lüfter AUS).

Start:  sudo python3 taupunkt.py [--pin 4] [--pin-ext 26] [--no-ext]
                                 [--port 8080] [--no-web]
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
DHT_PIN_INT     = 4              # interner DHT (Joy-Pi onboard)
DHT_PIN_EXT     = 26             # externer DHT (SERV01)
INTERVAL_S      = 3.0            # Sensor-Takt
LOG_DIR         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tp_data")

FAN_CHIP        = "/dev/gpiochip0"
FAN_PIN         = 21
FAN_ACTIVE_LOW  = True
FAN_MIN_HOLD_S  = 1.0
DEFAULT_DIFF_ON  = 1.0           # ΔT (extern-intern) °C zum Einschalten
DEFAULT_DIFF_OFF = 0.0           # ΔT zum Ausschalten

WEB_HOST, WEB_PORT = "0.0.0.0", 8080


# ---------- DHT via Kernel-Overlay ------------------------------------------
def sh(cmd: list[str], check: bool = True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _iio_devices() -> set[str]:
    return set(glob.glob("/sys/bus/iio/devices/iio:device*"))


def _wait_new_dht_device(before: set[str], timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for d in _iio_devices() - before:
            try:
                with open(os.path.join(d, "name")) as f:
                    if f.read().startswith("dht"):
                        return d
            except OSError:
                pass
        time.sleep(0.2)
    raise RuntimeError("Kein neues dht-IIO-Device erschienen.")


def load_overlays(pins: list[int]) -> dict[int, str]:
    """Lädt das dht11-Overlay je Pin und liefert {pin: iio-device-path}."""
    sh(["sudo", "dtoverlay", "-r", "dht11"], check=False)
    time.sleep(0.3)
    devs: dict[int, str] = {}
    for pin in pins:
        before = _iio_devices()
        sh(["sudo", "dtoverlay", "dht11", f"gpiopin={pin}"])
        devs[pin] = _wait_new_dht_device(before)
        print(f"  GPIO {pin:>2} -> {devs[pin]}")
    return devs


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
    """Modi:
      "diff" – Differenz-Regelung: EIN wenn (t_ext - t_int) >= diff_on,
               AUS wenn (t_ext - t_int) <= diff_off.
      "on"   – manuell EIN
      "off"  – manuell AUS
    """

    def __init__(self) -> None:
        self.on = False
        self.last_change = 0.0
        self.diff_on = DEFAULT_DIFF_ON
        self.diff_off = DEFAULT_DIFF_OFF
        self.mode = "diff"
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

    def update(self, t_int: float | None, t_ext: float | None) -> None:
        with self.lock:
            mode, on_th, off_th = self.mode, self.diff_on, self.diff_off
        if mode == "on" and not self.on:
            self._set(True)
            return
        if mode == "off" and self.on:
            self._set(False)
            return
        if mode != "diff" or t_int is None or t_ext is None:
            return
        delta = t_ext - t_int
        held = time.monotonic() - self.last_change
        if not self.on and delta >= on_th and held >= FAN_MIN_HOLD_S:
            self._set(True)
        elif self.on and delta <= off_th and held >= FAN_MIN_HOLD_S:
            self._set(False)

    def settings(self) -> dict:
        with self.lock:
            return {"diff_on": self.diff_on, "diff_off": self.diff_off, "mode": self.mode}

    def configure(self, diff_on=None, diff_off=None, mode=None) -> None:
        with self.lock:
            if diff_on is not None:
                self.diff_on = float(diff_on)
            if diff_off is not None:
                self.diff_off = float(diff_off)
            if mode is not None:
                if mode not in ("diff", "on", "off"):
                    raise ValueError(f"invalid mode: {mode}")
                self.mode = mode
            if self.diff_off > self.diff_on:
                self.diff_off, self.diff_on = self.diff_on, self.diff_off

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
 body{font-family:system-ui,sans-serif;max-width:720px;margin:1.5rem auto;padding:0 1rem}
 h2{margin:1.25rem 0 .25rem;font-size:1.05rem}
 .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5rem 1.25rem;margin:.25rem 0 1rem}
 .v{font-size:1.4rem;font-weight:600}.l{opacity:.7;font-size:.8rem}
 #fan.on{color:#2e7d32}#fan.off{color:#999}
 .summary{display:grid;grid-template-columns:1fr 1fr;gap:.5rem 1.25rem}
 input[type=number]{width:6rem}label{display:block;margin:.35rem 0}
 button{padding:.45rem .9rem}
</style>
<h1>Taupunktlüftung</h1>

<h2>Sensor intern (GPIO 4)</h2>
<div class=grid>
 <div><div class=l>Temperatur</div><div class=v id=t1>–</div></div>
 <div><div class=l>Luftfeuchte</div><div class=v id=h1>–</div></div>
 <div><div class=l>Taupunkt</div><div class=v id=td1>–</div></div>
</div>

<h2>Sensor extern (GPIO 26)</h2>
<div class=grid>
 <div><div class=l>Temperatur</div><div class=v id=t2>–</div></div>
 <div><div class=l>Luftfeuchte</div><div class=v id=h2>–</div></div>
 <div><div class=l>Taupunkt</div><div class=v id=td2>–</div></div>
</div>

<h2>Status</h2>
<div class=summary>
 <div><div class=l>ΔT (extern - intern)</div><div class=v id=dt>–</div></div>
 <div><div class=l>Lüfter</div><div class="v off" id=fan>–</div></div>
</div>

<fieldset><legend>Einstellungen</legend><form id=f>
 <label>ΔT EIN (°C): <input type=number step=0.1 name=diff_on id=diff_on></label>
 <label>ΔT AUS (°C): <input type=number step=0.1 name=diff_off id=diff_off></label>
 <label>Modus:
  <select name=mode id=mm>
   <option value=diff>Automatik (Differenz)</option>
   <option value=on>Lüfter AN</option>
   <option value=off>Lüfter AUS</option>
  </select></label>
 <button>Speichern</button>
</form></fieldset>

<script>
const $=id=>document.getElementById(id);
const set=(id,v)=>{const e=$(id);if(document.activeElement!==e)e.value=v};
function show(s,t,h,td){
 $(t).textContent = s ? s.t.toFixed(1)+' °C' : '–';
 $(h).textContent = s ? s.h.toFixed(1)+' %' : '–';
 $(td).textContent= s ? s.td.toFixed(2)+' °C' : '–';
}
async function refresh(){
 try{
  const d=await(await fetch('/api/data',{cache:'no-store'})).json();
  show(d.int,'t1','h1','td1');
  show(d.ext,'t2','h2','td2');
  $('dt').textContent = (d.int&&d.ext)
    ? ((d.ext.t-d.int.t)>=0?'+':'')+(d.ext.t-d.int.t).toFixed(2)+' °C'
    : '–';
  const f=$('fan');f.textContent=d.fan?'EIN':'AUS';f.className='v '+(d.fan?'on':'off');
  set('diff_on',d.s.diff_on);set('diff_off',d.s.diff_off);
  if(document.activeElement!==$('mm'))$('mm').value=d.s.mode;
 }catch(e){}
}
$('f').onsubmit=async e=>{e.preventDefault();
 await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});refresh();};
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
        return jsonify({"int": state.get("int"), "ext": state.get("ext"),
                        "fan": fan.on, "s": fan.settings()})

    @app.post("/api/settings")
    def _settings():
        data = request.get_json(silent=True) or request.form.to_dict()
        try:
            fan.configure(diff_on=data.get("diff_on") or None,
                          diff_off=data.get("diff_off") or None,
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
        w.writerow(["timestamp", "iso_time",
                    "t_int", "h_int", "td_int",
                    "t_ext", "h_ext", "td_ext",
                    "fan_on"])
    print(f"Logfile: {path}")
    return f, w


def sample(dev: str | None) -> dict | None:
    if dev is None:
        return None
    werte = read_sensor(dev)
    if werte is None:
        return None
    t, h = werte
    return {"t": t, "h": h, "td": dewpoint(t, h)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Taupunktlüftung mit Web-UI")
    ap.add_argument("--pin", type=int, default=DHT_PIN_INT, help="interner DHT (Default: 4)")
    ap.add_argument("--pin-ext", type=int, default=DHT_PIN_EXT, help="externer DHT (Default: 26)")
    ap.add_argument("--no-ext", action="store_true", help="ohne zweiten Sensor")
    ap.add_argument("--port", type=int, default=WEB_PORT)
    ap.add_argument("--host", default=WEB_HOST)
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()

    pins = [args.pin] + ([] if args.no_ext else [args.pin_ext])
    print(f"Lade dht11-Overlays für GPIOs {pins} …")
    try:
        devs = load_overlays(pins)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    dev_int = devs[args.pin]
    dev_ext = devs.get(args.pin_ext) if not args.no_ext else None
    print("STRG+C zum Beenden.\n")

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

    print(f"{'Zeit':<10} {'T-int':>6} {'T-ext':>6} {'ΔT':>6} {'Td-int':>7} {'Td-ext':>7}")
    print("-" * 50)
    try:
        while True:
            t0 = time.monotonic()
            si = sample(dev_int)
            se = sample(dev_ext)
            now = time.time()
            iso = datetime.fromtimestamp(now).isoformat(timespec="seconds")
            state["int"] = si
            state["ext"] = se
            fan.update(si["t"] if si else None, se["t"] if se else None)

            def fmt(v, w, p):
                return f"{v:>{w}.{p}f}" if v is not None else f"{'–':>{w}}"
            ti = si["t"] if si else None
            te = se["t"] if se else None
            dt = (te - ti) if (ti is not None and te is not None) else None
            print(f"{iso[11:]:<10} {fmt(ti,6,1)} {fmt(te,6,1)} {fmt(dt,6,2)} "
                  f"{fmt(si['td'] if si else None,7,2)} "
                  f"{fmt(se['td'] if se else None,7,2)}  "
                  f"{'[FAN]' if fan.on else ''}")

            log_w.writerow([
                f"{now:.0f}", iso,
                f"{si['t']:.2f}" if si else "", f"{si['h']:.2f}" if si else "",
                f"{si['td']:.2f}" if si else "",
                f"{se['t']:.2f}" if se else "", f"{se['h']:.2f}" if se else "",
                f"{se['td']:.2f}" if se else "",
                "1" if fan.on else "0",
            ])
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
