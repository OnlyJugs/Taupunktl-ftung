#!/usr/bin/env python3
"""Taupunktlüftung für den Joy-Pi.

Wichtig:
	GPIO 4  [DHT intern]
    GPIO 26 [DHT extern]
    GPIO 21 [Lüfter]

Aufruf:
    sudo python3 taupunkt.py
    sudo python3 taupunkt.py --no-ext --port 8000
"""
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


# --- Konfiguration ---------------------------------------------------------
PIN_INTERN = 4
PIN_EXTERN = 26
FAN_PIN    = 21
FAN_ACTIVE_LOW = True       # MOSFET-Treiber: LOW schaltet ein
FAN_CHIP   = "/dev/gpiochip0"

LOOP_INTERVAL  = 3.0        # Sekunden zwischen Messungen
FAN_MIN_HOLD   = 60.0       # Mindesthaltezeit gegen Flattern (s)
DIFF_ON_DEFAULT  = 2.0      # Lüfter EIN ab ΔTd (Td_ext - Td_int) °C
DIFF_OFF_DEFAULT = 0.5      # Lüfter AUS bei ΔTd ≤ … °C

WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
LOG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tp_data")


# --- DHT über Kernel-Overlay ----------------------------------------------
# Der dht11-Overlay-Treiber legt jedes Sensor-Gerät unter
# /sys/bus/iio/devices/iio:device* ab. Wir laden ihn pro Pin einmal und
# merken uns, welches Device neu dazugekommen ist.

def run(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def iio_devices():
    return set(glob.glob("/sys/bus/iio/devices/iio:device*"))


def wait_for_new_dht(known, timeout=5.0):
    """Wartet bis zu ``timeout`` s auf ein neues iio-Device mit Namen 'dht…'."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for dev in iio_devices() - known:
            try:
                with open(os.path.join(dev, "name")) as f:
                    if f.read().startswith("dht"):
                        return dev
            except OSError:
                continue
        time.sleep(0.2)
    raise RuntimeError("Kein neues dht-Device aufgetaucht.")


def load_overlays(pins):
    """Lädt das dht11-Overlay je Pin und liefert {pin: device_pfad}."""
    run(["sudo", "dtoverlay", "-r", "dht11"], check=False)
    time.sleep(0.3)
    mapping = {}
    for pin in pins:
        before = iio_devices()
        run(["sudo", "dtoverlay", "dht11", f"gpiopin={pin}"])
        mapping[pin] = wait_for_new_dht(before)
        print(f"  GPIO {pin:>2}  →  {mapping[pin]}")
    return mapping


def unload_overlays():
    run(["sudo", "dtoverlay", "-r", "dht11"], check=False)


def read_dht(dev, tries=4):
    """Liefert (T °C, RH %) oder None. DHT22 antwortet nicht jedes Mal."""
    for attempt in range(tries):
        try:
            with open(os.path.join(dev, "in_temp_input")) as f:
                t = int(f.read()) / 1000.0
            with open(os.path.join(dev, "in_humidityrelative_input")) as f:
                h = int(f.read()) / 1000.0
            return t, h
        except (OSError, ValueError):
            if attempt < tries - 1:
                time.sleep(2.2)
    return None


def dewpoint(temp_c, rh):
    """Taupunkt nach Magnus-Formel mit DWD-Konstanten."""
    a, b = 17.62, 243.12
    gamma = a * temp_c / (b + temp_c) + math.log(max(rh, 1e-3) / 100.0)
    return b * gamma / (a - gamma)


# --- Lüfter ---------------------------------------------------------------
class Fan:
    """GPIO-Lüfter mit drei Modi.

    diff  – Automatik (Taupunktlüftung): an bei (td_ext - td_int) ≥ diff_on,
            aus bei ≤ diff_off.
    on    – Handbetrieb: immer an
    off   – Handbetrieb: immer aus
    """

    def __init__(self):
        self.on = False
        self.last_change = 0.0
        self.diff_on  = DIFF_ON_DEFAULT
        self.diff_off = DIFF_OFF_DEFAULT
        self.mode = "diff"
        self.lock = threading.Lock()

        self._level_off = Value.ACTIVE   if FAN_ACTIVE_LOW else Value.INACTIVE
        self._level_on  = Value.INACTIVE if FAN_ACTIVE_LOW else Value.ACTIVE
        self.req = gpiod.request_lines(
            FAN_CHIP,
            consumer="taupunkt-fan",
            config={FAN_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=self._level_off,
                bias=Bias.PULL_UP if FAN_ACTIVE_LOW else Bias.PULL_DOWN,
            )},
        )

    def _switch(self, on):
        self.req.set_value(FAN_PIN, self._level_on if on else self._level_off)
        self.on = on
        self.last_change = time.monotonic()

    def update(self, td_int, td_ext):
        with self.lock:
            mode, on_th, off_th = self.mode, self.diff_on, self.diff_off

        if mode == "on":
            if not self.on:
                self._switch(True)
            return
        if mode == "off":
            if self.on:
                self._switch(False)
            return
        # Automatik (Taupunktdifferenz: extern - intern)
        if td_int is None or td_ext is None:
            return
        delta = td_ext - td_int
        if time.monotonic() - self.last_change < FAN_MIN_HOLD:
            return
        if not self.on and delta >= on_th:
            self._switch(True)
        elif self.on and delta <= off_th:
            self._switch(False)

    def settings(self):
        with self.lock:
            return {"diff_on": self.diff_on, "diff_off": self.diff_off, "mode": self.mode}

    def configure(self, diff_on=None, diff_off=None, mode=None):
        def number(v):
            return float(str(v).replace(",", ".").strip())

        with self.lock:
            if diff_on is not None:
                self.diff_on = number(diff_on)
            if diff_off is not None:
                self.diff_off = number(diff_off)
            if mode is not None:
                if mode not in ("diff", "on", "off"):
                    raise ValueError(f"Unbekannter Modus: {mode}")
                self.mode = mode
            # AUS-Schwelle darf nicht über der EIN-Schwelle liegen
            if self.diff_off > self.diff_on:
                self.diff_off, self.diff_on = self.diff_on, self.diff_off

    def shutdown(self):
        try:
            self.req.set_value(FAN_PIN, self._level_off)
            self.req.release()
        except Exception:
            pass


# --- Web-UI ---------------------------------------------------------------
INDEX_HTML = """<!doctype html><html lang="de"><meta charset="utf-8">
<title>Taupunktlüftung</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:1.5rem auto;padding:0 1rem}
 h2{margin:1.25rem 0 .25rem;font-size:1.05rem}
 .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5rem 1.25rem;margin:.25rem 0 1rem}
 .v{font-size:1.4rem;font-weight:600}.l{opacity:.7;font-size:.8rem}
 input[type=text]{width:6rem}label{display:block;margin:.35rem 0}
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
<div class=grid>
 <div><div class=l>ΔTd (extern - intern)</div><div class=v id=dt>–</div></div>
 <div><div class=l>Lüfter</div><div class=v id=fan>–</div></div>
 <div></div>
</div>

<fieldset><legend>Einstellungen</legend><form id=f>
 <label>ΔTd EIN (°C): <input type=text inputmode=decimal name=diff_on id=diff_on></label>
 <label>ΔTd AUS (°C): <input type=text inputmode=decimal name=diff_off id=diff_off></label>
 <label>Modus:
  <select name=mode id=mm>
   <option value=diff>Automatik (Differenz)</option>
   <option value=on>Lüfter AN</option>
   <option value=off>Lüfter AUS</option>
  </select></label>
 <button>Speichern</button>
</form></fieldset>

<script>
const $ = id => document.getElementById(id);
let dirty = false;

function fillIfIdle(id, v){
  const el = $(id);
  if (!dirty && document.activeElement !== el) el.value = v;
}

function showSensor(s, tId, hId, tdId){
  $(tId).textContent  = s ? s.t.toFixed(1)  + ' °C' : '–';
  $(hId).textContent  = s ? s.h.toFixed(1)  + ' %'  : '–';
  $(tdId).textContent = s ? s.td.toFixed(2) + ' °C' : '–';
}

async function refresh(){
  try {
    const d = await (await fetch('/api/data', {cache:'no-store'})).json();
    showSensor(d.int, 't1','h1','td1');
    showSensor(d.ext, 't2','h2','td2');
    if (d.int && d.ext) {
      const delta = d.ext.td - d.int.td;
      $('dt').textContent = (delta >= 0 ? '+' : '') + delta.toFixed(2) + ' °C';
    } else {
      $('dt').textContent = '–';
    }
    $('fan').textContent = d.fan ? 'EIN' : 'AUS';
    fillIfIdle('diff_on',  d.s.diff_on);
    fillIfIdle('diff_off', d.s.diff_off);
    if (!dirty && document.activeElement !== $('mm')) $('mm').value = d.s.mode;
  } catch (e) { /* ignorieren – nächster Tick versucht es erneut */ }
}

for (const el of document.querySelectorAll('#f input, #f select'))
  el.addEventListener('input', () => { dirty = true; });

$('f').onsubmit = async e => {
  e.preventDefault();
  const payload = Object.fromEntries(new FormData(e.target));
  for (const k of ['diff_on','diff_off'])
    if (payload[k]) payload[k] = String(payload[k]).replace(',', '.');
  await fetch('/api/settings', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  dirty = false;
  refresh();
};

refresh();
setInterval(refresh, 1000);
</script></html>"""


def build_app(state, fan):
    app = Flask(__name__)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/api/data")
    def api_data():
        return jsonify({
            "int": state.get("int"),
            "ext": state.get("ext"),
            "fan": fan.on,
            "s":   fan.settings(),
        })

    @app.post("/api/settings")
    def api_settings():
        data = request.get_json(silent=True) or request.form.to_dict()
        try:
            fan.configure(
                diff_on  = data.get("diff_on")  or None,
                diff_off = data.get("diff_off") or None,
                mode     = data.get("mode")     or None,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "s": fan.settings()})

    return app


# --- Logging --------------------------------------------------------------
def open_logfile():
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"tp_{datetime.now():%Y-%m-%d}.csv")
    fresh = not os.path.exists(path)
    fp = open(path, "a", newline="", buffering=1)
    writer = csv.writer(fp)
    if fresh:
        writer.writerow([
            "timestamp", "iso_time",
            "t_int", "h_int", "td_int",
            "t_ext", "h_ext", "td_ext",
            "fan_on",
        ])
    print(f"Logfile: {path}")
    return fp, writer


# --- Hauptschleife --------------------------------------------------------
def sample(dev):
    if dev is None:
        return None
    raw = read_dht(dev)
    if raw is None:
        return None
    t, h = raw
    return {"t": t, "h": h, "td": dewpoint(t, h)}


def parse_args():
    ap = argparse.ArgumentParser(description="Taupunktlüftung mit Web-UI")
    ap.add_argument("--pin",     type=int, default=PIN_INTERN, help="interner DHT (Default 4)")
    ap.add_argument("--pin-ext", type=int, default=PIN_EXTERN, help="externer DHT (Default 26)")
    ap.add_argument("--no-ext",  action="store_true", help="ohne zweiten Sensor laufen")
    ap.add_argument("--host",    default=WEB_HOST)
    ap.add_argument("--port",    type=int, default=WEB_PORT)
    ap.add_argument("--no-web",  action="store_true", help="Web-UI deaktivieren")
    return ap.parse_args()


def start_web(state, fan, host, port):
    app = build_app(state, fan)
    threading.Thread(
        target=lambda: app.run(host=host, port=port,
                               debug=False, use_reloader=False, threaded=True),
        name="web",
        daemon=True,
    ).start()
    print(f"Web-UI: http://{host}:{port}/")


def main():
    args = parse_args()
    pins = [args.pin] + ([] if args.no_ext else [args.pin_ext])
    print(f"Lade dht11-Overlay für GPIO {pins} …")
    try:
        devs = load_overlays(pins)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"Fehler beim Laden der Overlays: {e}", file=sys.stderr)
        return 1

    dev_int = devs[args.pin]
    dev_ext = devs.get(args.pin_ext) if not args.no_ext else None
    print("STRG+C zum Beenden.\n")

    log_fp, log_writer = open_logfile()
    fan = Fan()
    state = {}

    if not args.no_web:
        start_web(state, fan, args.host, args.port)

    print(f"{'Zeit':<10} {'T-int':>6} {'T-ext':>6} {'ΔTd':>6} {'Td-int':>7} {'Td-ext':>7}")
    print("-" * 50)

    try:
        while True:
            tick = time.monotonic()
            s_int = sample(dev_int)
            s_ext = sample(dev_ext)

            now = time.time()
            iso = datetime.fromtimestamp(now).isoformat(timespec="seconds")

            state["int"] = s_int
            state["ext"] = s_ext

            t_int  = s_int["t"]  if s_int else None
            t_ext  = s_ext["t"]  if s_ext else None
            td_int = s_int["td"] if s_int else None
            td_ext = s_ext["td"] if s_ext else None
            fan.update(td_int, td_ext)

            def cell(value, width, prec):
                return f"{value:>{width}.{prec}f}" if value is not None else f"{'–':>{width}}"

            delta_td = (td_ext - td_int) if (td_int is not None and td_ext is not None) else None
            print(
                f"{iso[11:]:<10} "
                f"{cell(t_int, 6, 1)} {cell(t_ext, 6, 1)} {cell(delta_td, 6, 2)} "
                f"{cell(td_int, 7, 2)} "
                f"{cell(td_ext, 7, 2)}  "
                f"{'[FAN]' if fan.on else ''}"
            )

            log_writer.writerow([
                f"{now:.0f}", iso,
                f"{s_int['t']:.2f}"  if s_int else "",
                f"{s_int['h']:.2f}"  if s_int else "",
                f"{s_int['td']:.2f}" if s_int else "",
                f"{s_ext['t']:.2f}"  if s_ext else "",
                f"{s_ext['h']:.2f}"  if s_ext else "",
                f"{s_ext['td']:.2f}" if s_ext else "",
                "1" if fan.on else "0",
            ])

            time.sleep(max(0.0, LOOP_INTERVAL - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        fan.shutdown()
        log_fp.close()
        unload_overlays()
    return 0


if __name__ == "__main__":
    sys.exit(main())
