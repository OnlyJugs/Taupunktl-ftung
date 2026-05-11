#!/usr/bin/env python3
"""
Findet den DHT-Sensor: probiert nacheinander mehrere GPIO-Pins mit dem
Kernel-Overlay `dht11` durch und meldet, an welchem Pin ein Sensor
antwortet.

Zweck: Auf dem Joy-Pi gibt es zusätzlich zum externen SERV01-Anschluss
(GPIO 26) einen onboard verdrahteten DHT (laut Joy-Pi-Doku BCM GPIO 4).
Damit lässt sich verifizieren, an welchem Pin der eingebaute Sensor
tatsächlich hängt.

Aufruf:
    sudo python3 find_dht.py            # probiert Standardliste
    sudo python3 find_dht.py 4 17 26    # eigene Pinliste
"""
from __future__ import annotations

import sys
import time

from taupunkt import (
    find_iio_device,
    load_overlay,
    read_sensor_retry,
    unload_overlay,
)

DEFAULT_PINS = [4, 17, 22, 26, 27]


def try_pin(pin: int) -> tuple[float, float] | None:
    print(f"\n--- Pin GPIO {pin} ---")
    try:
        load_overlay(pin)
    except Exception as e:
        print(f"  Overlay-Fehler: {e}")
        return None
    try:
        dev = find_iio_device()
        if not dev:
            print("  Kein IIO-Device erschienen.")
            return None
        # zwei Versuche, AM2302 ist manchmal stur
        for _ in range(2):
            werte = read_sensor_retry(dev, tries=4, pause=2.2)
            if werte is not None:
                t, h = werte
                print(f"  OK: T={t:.1f} °C  RH={h:.1f} %")
                return werte
            time.sleep(1.0)
        print("  Kein gültiges Lesen.")
        return None
    finally:
        unload_overlay()
        time.sleep(0.3)


def main() -> int:
    pins = [int(a) for a in sys.argv[1:]] or DEFAULT_PINS
    print(f"Suche DHT auf Pins: {pins}")
    hits: list[int] = []
    for p in pins:
        if try_pin(p) is not None:
            hits.append(p)
    print("\n=== Ergebnis ===")
    if hits:
        for p in hits:
            print(f"  Sensor reagiert auf GPIO {p}")
        print(f"\nStart mit: sudo python3 taupunkt.py --pin {hits[0]}")
    else:
        print("Kein DHT auf den getesteten Pins gefunden.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
