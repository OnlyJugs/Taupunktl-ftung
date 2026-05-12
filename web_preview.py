"""Windows-Vorschau für die Taupunkt-Web-UI (zwei Sensoren).

Mockt beide DHTs und die GPIO-Lüftersteuerung, damit die Oberfläche ohne
Raspberry getestet werden kann.

Start:
    pip install flask
    python web_preview.py
Dann im Browser:  http://localhost:8080/
"""
from __future__ import annotations

import csv
import io
import math
import random
import sys
import threading
import time
import types
from collections import deque
from datetime import datetime

from flask import Flask, Response, jsonify, request

# gpiod ist auf Windows nicht installiert – Dummys einschieben, bevor
# taupunkt importiert wird.
_fake_gpiod = types.ModuleType("gpiod")
_fake_gpiod.request_lines = lambda *a, **kw: None         # type: ignore[attr-defined]
_fake_gpiod.LineSettings = lambda **kw: None              # type: ignore[attr-defined]
_fake_line = types.ModuleType("gpiod.line")


class _Enum:
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    OUTPUT = "OUT"
    INPUT = "IN"
    PULL_UP = "PU"
    PULL_DOWN = "PD"


_fake_line.Value = _Enum
_fake_line.Direction = _Enum
_fake_line.Bias = _Enum
sys.modules["gpiod"] = _fake_gpiod
sys.modules["gpiod.line"] = _fake_line

from taupunkt import INDEX_HTML, dewpoint  # noqa: E402


class MockFan:
    def __init__(self) -> None:
        self.on = False
        self.diff_on = 1.0
        self.diff_off = 0.0
        self.mode = "diff"
        self.lock = threading.Lock()

    def update(self, td_int, td_ext) -> None:
        with self.lock:
            mode, on_th, off_th = self.mode, self.diff_on, self.diff_off
        if mode == "on":
            self.on = True
            return
        if mode == "off":
            self.on = False
            return
        if td_int is None or td_ext is None:
            return
        d = td_ext - td_int
        if not self.on and d >= on_th:
            self.on = True
        elif self.on and d <= off_th:
            self.on = False

    def settings(self) -> dict:
        with self.lock:
            return {"diff_on": self.diff_on, "diff_off": self.diff_off, "mode": self.mode}

    def configure(self, diff_on=None, diff_off=None, mode=None) -> None:
        def _f(v):
            return float(str(v).replace(",", ".").strip())
        with self.lock:
            if diff_on is not None:
                self.diff_on = _f(diff_on)
            if diff_off is not None:
                self.diff_off = _f(diff_off)
            if mode is not None:
                if mode not in ("diff", "on", "off"):
                    raise ValueError(mode)
                self.mode = mode
            if self.diff_off > self.diff_on:
                self.diff_off, self.diff_on = self.diff_on, self.diff_off


def sensor_thread(state: dict, fan: MockFan, history: deque) -> None:
    """Mockt zwei Sensoren: intern fast konstant, extern schwankt um intern."""
    t0 = time.time()
    while True:
        now = time.time()
        phase = (now - t0) / 60.0
        # intern: ~21 °C, kaum Schwankung
        t_i = 21.0 + 0.3 * math.sin(phase * 2 * math.pi) + random.uniform(-0.05, 0.05)
        h_i = 50.0 + random.uniform(-1, 1)
        # extern: pendelt 2 K um intern -> Δ kreuzt 0/1 °C, Lüfter schaltet
        t_e = t_i + 2.0 * math.sin(phase * 2 * math.pi) + random.uniform(-0.1, 0.1)
        h_e = 60.0 + 10.0 * math.sin(phase * 2 * math.pi + 0.5) + random.uniform(-1, 1)
        h_e = max(20.0, min(95.0, h_e))
        td_i = dewpoint(t_i, h_i)
        td_e = dewpoint(t_e, h_e)
        fan.update(td_i, td_e)
        state["int"] = {"t": t_i, "h": h_i, "td": td_i}
        state["ext"] = {"t": t_e, "h": h_e, "td": td_e}
        history.append({
            "time":   datetime.fromtimestamp(now).strftime("%H:%M:%S"),
            "t_int":  t_i, "h_int":  h_i, "td_int": td_i,
            "t_ext":  t_e, "h_ext":  h_e, "td_ext": td_e,
            "fan":    fan.on,
        })
        time.sleep(1.0)


def main() -> None:
    state: dict = {}
    fan = MockFan()
    history: deque = deque(maxlen=120)
    threading.Thread(target=sensor_thread, args=(state, fan, history), daemon=True).start()

    app = Flask(__name__)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def _index():
        return INDEX_HTML

    @app.get("/api/data")
    def _data():
        return jsonify({"int": state.get("int"), "ext": state.get("ext"),
                        "fan": fan.on, "s": fan.settings()})

    @app.get("/api/history")
    def _history():
        return jsonify({"rows": list(history)})

    @app.get("/api/download")
    def _download():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "t_int", "h_int", "td_int",
                    "t_ext", "h_ext", "td_ext", "fan_on"])
        for r in history:
            w.writerow([r["time"],
                        f"{r['t_int']:.2f}"  if r['t_int']  is not None else "",
                        f"{r['h_int']:.2f}"  if r['h_int']  is not None else "",
                        f"{r['td_int']:.2f}" if r['td_int'] is not None else "",
                        f"{r['t_ext']:.2f}"  if r['t_ext']  is not None else "",
                        f"{r['h_ext']:.2f}"  if r['h_ext']  is not None else "",
                        f"{r['td_ext']:.2f}" if r['td_ext'] is not None else "",
                        "1" if r["fan"] else "0"])
        fname = f"tp_preview_{datetime.now():%Y-%m-%d}.csv"
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

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

    print("Vorschau läuft: http://localhost:8080/   (STRG+C zum Beenden)")
    app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
