#!/usr/bin/env python3
"""
Taupunktlüftung – liest Temperatur und Luftfeuchte vom AM2302/DHT22-Sensor
am Joy-Pi-Stecker SERV01 (Header-Pin 37 = BCM GPIO 26), berechnet den
Taupunkt nach Magnus und gibt die Werte alle 3 Sekunden aus.

Implementierung: nutzt den Linux-Kernel-Treiber `dht11` (funktioniert für
DHT11/DHT22/AM2302) über das Device-Tree-Overlay `dht11`. Dieser Treiber
liest die kritischen µs-Pulse im Kernel und ist auf dem Pi 4 mit Kernel
6.12 deutlich zuverlässiger als reine Python-Bitbang-Lösungen.

Start (sudo nur für `dtoverlay` nötig):
    sudo python3 taupunkt.py
STRG+C beendet das Programm und entlädt das Overlay wieder.
"""

from __future__ import annotations

import glob
import math
import os
import signal
import subprocess
import sys
import time

GPIO_PIN   = 26    # SERV01 Signal = Header-Pin 37 = BCM GPIO 26
INTERVAL_S = 3.0


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
    print(f"{'Zeit':<10} {'Temp °C':>8} {'Feuchte %':>10} {'Taupunkt °C':>12}")
    print("-" * 46)

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        while True:
            t0 = time.monotonic()
            werte = read_sensor_retry(dev)
            now = time.strftime("%H:%M:%S")
            if werte is None:
                print(f"{now:<10}  Lesefehler – Sensor antwortet nicht.")
            else:
                t, h = werte
                td = taupunkt(t, h)
                print(f"{now:<10} {t:>8.1f} {h:>10.1f} {td:>12.2f}")

            # 3-Sekunden-Takt einhalten, egal wie lang die Retries dauerten
            rest = INTERVAL_S - (time.monotonic() - t0)
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        unload_overlay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
