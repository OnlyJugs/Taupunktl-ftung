#!/usr/bin/env python3
"""
Sicherer Lüfter-Test auf GPIO 25 (SERV02, Header-Pin 22).

Steuerung:
    [ENTER]   – Pegel umschalten (LOW <-> HIGH)
    p         – kurzer 200 ms HIGH-Puls
    s         – Software-PWM 25 kHz, Tastgrad 25/50/75/100 % je 1 s
    q / STRG+C – beenden (Pin wird LOW + Pull-down gesetzt und freigegeben)

Wichtig:
    Vor dem Freigeben der Linie wird sie als INPUT mit PULL_DOWN
    rekonfiguriert. Damit bleibt der Pegel auch nach Programmende
    sicher LOW – sonst kann ein MOSFET-Gate floaten und der Lüfter
    läuft scheinbar weiter.
"""
from __future__ import annotations

import signal
import sys
import time

import gpiod
from gpiod.line import Bias, Direction, Value

CHIP = "/dev/gpiochip0"
PIN  = 21                       # BCM GPIO 21 (Header-Pin 40)


def request_output():
    """Linie als Output mit Startwert LOW und Pull-down anfordern."""
    return gpiod.request_lines(
        CHIP,
        consumer="fan-test",
        config={
            PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.INACTIVE,
                bias=Bias.PULL_DOWN,
            )
        },
    )


def safe_off(req) -> None:
    """Pin LOW schalten, danach als Input+Pull-down freigeben."""
    if req is None:
        return
    try:
        req.set_value(PIN, Value.INACTIVE)
        time.sleep(0.02)
    except Exception:
        pass
    # Linie auf INPUT + PULL_DOWN umkonfigurieren, damit sie nach
    # dem release() definiert auf GND gezogen bleibt.
    try:
        req.reconfigure_lines(
            {PIN: gpiod.LineSettings(direction=Direction.INPUT,
                                     bias=Bias.PULL_DOWN)}
        )
    except Exception:
        pass
    try:
        req.release()
    except Exception:
        pass


def soft_pwm(req, duty: float, freq_hz: float, seconds: float) -> None:
    period = 1.0 / freq_hz
    on_t  = period * max(0.0, min(1.0, duty))
    off_t = period - on_t
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if on_t > 0:
            req.set_value(PIN, Value.ACTIVE)
            time.sleep(on_t)
        if off_t > 0:
            req.set_value(PIN, Value.INACTIVE)
            time.sleep(off_t)


def main() -> int:
    req = request_output()
    state = False  # LOW

    def bye(*_a):
        print(f"\nBeende, schalte GPIO {PIN} LOW + Pull-down.")
        safe_off(req)
        sys.exit(0)
    signal.signal(signal.SIGINT,  bye)
    signal.signal(signal.SIGTERM, bye)

    print(f"Lüfter-Test auf GPIO {PIN} (SERV02 / Pin 22).")
    print("Befehle: [ENTER]=toggle  p=200 ms Puls  s=PWM-Sweep  q=Quit")
    print("Falls etwas Falsches reagiert: STRG+C drücken – Pin wird LOW gesetzt.\n")
    print("Pegel: LOW")

    try:
        while True:
            cmd = input("> ").strip().lower()
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd == "p":
                print("  Puls: HIGH 200 ms ...")
                req.set_value(PIN, Value.ACTIVE)
                time.sleep(0.2)
                req.set_value(PIN, Value.INACTIVE)
                state = False
                print("  Pegel: LOW")
            elif cmd == "s":
                print("  PWM-Sweep @ 25 kHz: 25 % -> 50 % -> 75 % -> 100 %, je 1 s")
                for d in (0.25, 0.5, 0.75, 1.0):
                    print(f"    {int(d*100)} %")
                    soft_pwm(req, d, 25_000, 1.0)
                req.set_value(PIN, Value.INACTIVE)
                state = False
                print("  PWM aus, Pegel: LOW")
            else:
                state = not state
                req.set_value(PIN, Value.ACTIVE if state else Value.INACTIVE)
                print(f"  Pegel: {'HIGH' if state else 'LOW'}")
    except EOFError:
        pass
    finally:
        safe_off(req)
        print("Pin freigegeben (LOW + Pull-down).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
