# Taupunktlüftung

Liest Temperatur und Luftfeuchte von einem **AM2302 / DHT22**-Sensor am
Joy-Pi-Anschluss **SERV01** (Header-Pin 37 = BCM GPIO 26) auf einem
Raspberry Pi 4B, berechnet den Taupunkt nach der Magnus-Formel und gibt
die Werte alle 3 Sekunden im Terminal aus.

## Hardware

| SERV01-Pin | AM2302   |
|------------|----------|
| Pin 37 (GPIO 26) – Signal | DATA |
| +5 V       | VCC      |
| GND        | GND      |

> Hinweis: Falls der Stecker falsch herum sitzt, antwortet der Sensor nicht.
> Dann den 3-Pin-Stecker einmal um 180° drehen.

## Software

Das Programm verwendet den Linux-Kernel-Treiber `dht11` (funktioniert auch
für DHT22/AM2302) per Device-Tree-Overlay. Auf dem Pi 4 mit Kernel 6.12 ist
das deutlich zuverlässiger als reine Python-Bitbang-Bibliotheken.

### Start

```bash
sudo python3 taupunkt.py                  # GPIO 26 (SERV01), Web-UI :8080
sudo python3 taupunkt.py --pin 4          # eingebauter Joy-Pi-DHT
sudo python3 taupunkt.py --no-web         # ohne Web-Server
```

`sudo` wird nur für `dtoverlay` benötigt. Beenden mit `STRG+C` – das Overlay
wird beim Beenden automatisch entladen.

### Web-Interface

Nach dem Start ist unter `http://<pi-ip>:8080/` eine Live-Anzeige verfügbar,
die jede Sekunde aktualisiert. Dort lassen sich die Hysterese-Schwellen
(EIN/AUS) ändern und der Lüfter manuell EIN/AUS schalten bzw. wieder auf
Automatik stellen.

Voraussetzung: `pip install flask` (oder `sudo apt install python3-flask`).
Ohne Flask startet das Skript trotzdem – nur ohne Web-UI.

### Sensor finden

Falls unklar ist, an welchem GPIO der eingebaute DHT hängt:

```bash
sudo python3 find_dht.py            # Standardliste 4, 17, 22, 26, 27
sudo python3 find_dht.py 4 26       # eigene Pinliste
```

### Beispielausgabe

```
Zeit        Temp °C  Feuchte %  Taupunkt °C
----------------------------------------------
08:56:02       26.2       33.0         8.65
08:56:10       26.5       30.1         7.56
```

## Taupunktformel

Magnus-Formel mit den DWD-Konstanten $a = 17{,}62$ und $b = 243{,}12\\,°\!C$:

$$
T_d = \frac{b \cdot \gamma}{a - \gamma}, \quad
\gamma = \frac{a \cdot T}{b + T} + \ln\!\left(\frac{\mathrm{RH}}{100}\right)
$$
