#!/usr/bin/env python3
"""
Taupunktlüftung – liest Temperatur und Luftfeuchte vom AM2302/DHT22-Sensor
am Joy-Pi-Stecker SERV01 (Header-Pin 37 = BCM GPIO 26), berechnet den
Taupunkt nach Magnus und gibt die Werte alle 3 Sekunden aus.

Zusätzlich:
  * CSV-Logging in tp_data/tp_YYYY-MM-DD.csv (eine Zeile pro Messung)
  * gleitende Mittelwerte über 1 / 5 / 15 Minuten (Temp, Feuchte, Taupunkt)
  * Min/Max-Anzeige seit Programmstart

Implementierung: nutzt den Linux-Kernel-Treiber `dht11` (funktioniert für
DHT11/DHT22/AM2302) über das Device-Tree-Overlay `dht11`.

Start (sudo nur für `dtoverlay` nötig):
    sudo python3 taupunkt.py
STRG+C beendet das Programm und entlädt das Overlay wieder.
"""

from __future__ import annotations

import csv
import glob
import math
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime

try:
    import gpiod
    from gpiod.line import Bias, Direction, Value
    _HAS_GPIOD = True
except ImportError:  # pragma: no cover
    _HAS_GPIOD = False

GPIO_PIN   = 26    # SERV01 Signal = Header-Pin 37 = BCM GPIO 26
INTERVAL_S = 3.0
LOG_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tp_data")

# Fenstergrößen für gleitende Mittelwerte (in Sekunden)
WINDOWS_S = (60, 300, 900)  # 1 min, 5 min, 15 min

# ---------- Lüftersteuerung -------------------------------------------------
FAN_CHIP        = "/dev/gpiochip0"
FAN_PIN         = 21      # BCM GPIO 21 (Header-Pin 40)
FAN_ACTIVE_LOW  = True    # Treiberstufe schaltet bei LOW ein -> invertieren
FAN_TD_ON       = 15.0    # °C – Lüfter EIN, sobald Taupunkt ≥ diesen Wert
FAN_TD_OFF      = 15.0    # °C – Lüfter AUS, sobald Taupunkt < diesen Wert
FAN_MIN_RUN_S   = 1      # Mindest-Laufzeit, sobald eingeschaltet
FAN_MIN_PAUSE_S = 1      # Mindest-Pause nach dem Ausschalten
FAN_AVG_WINDOW  = 0       # 0 = ungeglättet, sonst Sekunden für Mittelwert


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
    """Hysterese-Steuerung für den Lüfter auf BCM GPIO 25.

    Schaltet EIN, sobald der gleitende Td-Mittelwert >= FAN_TD_ON
    (typisch: kräftige Feuchte-Spitze, z. B. Atmen / Duschen / Kochen).
    Schaltet AUS, sobald er <= FAN_TD_OFF fällt. Mindest-Lauf- und
    Pausenzeiten verhindern Klappern.

    Bei fehlendem gpiod-Modul läuft der Controller im Dry-Run-Modus
    und gibt nur Statuswechsel auf der Konsole aus.
    """

    def __init__(self) -> None:
        self.on = False
        self.last_change = 0.0
        self.req = None
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
        since = now - self.last_change
        if not self.on and td_avg >= FAN_TD_ON and since >= FAN_MIN_PAUSE_S:
            self.on = True
            self.last_change = now
            self._set(True)
            return f"FAN ON  (Td⌀={td_avg:.2f} °C ≥ {FAN_TD_ON:.1f} °C)"
        if self.on and td_avg <= FAN_TD_OFF and since >= FAN_MIN_RUN_S:
            self.on = False
            self.last_change = now
            self._set(False)
            return f"FAN OFF (Td⌀={td_avg:.2f} °C ≤ {FAN_TD_OFF:.1f} °C)"
        return None

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


def main() -> int:
    print(f"Lade Kernel-Treiber dht11 auf GPIO {GPIO_PIN} (SERV01, Pin 37) …")
    try:
        load_overlay(GPIO_PIN)
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
    print("Taupunktlüftung – AM2302/DHT22 an SERV01 (Pin 37 / GPIO 26).")
    print("STRG+C zum Beenden.\n")

    log_f, log_w = open_logfile()
    stats = RollingStats(max_window_s=max(max(WINDOWS_S), FAN_AVG_WINDOW or 1))
    fan = FanController()
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
                log_w.writerow([
                    f"{now_wall:.0f}",
                    datetime.fromtimestamp(now_wall).isoformat(timespec="seconds"),
                    f"{t:.2f}", f"{h:.2f}", f"{td:.2f}",
                    "1" if fan.on else "0",
                ])

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
