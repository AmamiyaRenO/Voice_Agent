# Firmware Scripts

This folder mirrors the Raspberry Pi hardware-control scripts currently used by the Voice Agent setup.

Synced from Pi:
- Host: `10.0.0.2`
- Remote base path: `/home/RACHEL/RACHEL`
- Sync date: `2026-03-20`

## Files

- `face_agent.py`
  Unified MQTT subscriber on the Pi for hardware commands. It listens for:
  - `robot/pi/face/cmd`
  - `robot/pi/servo/cmd`
  - `robot/pi/led/cmd`

- `face_agent.env`
  Runtime environment for `face-agent.service`. This includes the broker host and broker port used by the Pi.

- `faceScript.py`
  Persistent face renderer for the Pi screen. It subscribes to face MQTT commands and swaps facial expression images on the display.

- `servoScript.py`
  Flower/servo controller script for GPIO pin control on the Pi.

- `ledScript.py`
  LED controller script for the Pi lighting hardware.

- `LCD Output/facialExpressions/`
  Archived face image assets that the display script expects. These were kept because they may still be useful for local testing and asset reference.

## Current Pi Runtime Model

The Pi currently uses two services for hardware display/control:

- `face-neutral.service`
  Runs `faceScript.py` as the persistent face renderer.

- `face-agent.service`
  Handles servo and LED commands and leaves face rendering to `faceScript.py`.

## Notes

- These files were copied from the live Pi, so they reflect the current hardware behavior more accurately than older local copies.
- Some files contain non-ASCII comments from the Pi source. They were preserved as-is during sync.
- If the Pi scripts change again, re-sync this folder from `/home/RACHEL/RACHEL`.
- Older duplicate scripts and ad-hoc usage notes were removed during cleanup.

## Recommended Deployment

The recommended direct Ethernet topology is:

- Windows PC / MQTT broker: `10.0.0.1/24`, port `1883`
- Raspberry Pi: `10.0.0.2/24`

The checked-in `face_agent.env` still mirrors the older live-Pi port `1884`. Update the deployed environment to `BROKER_PORT=1883` when using the default Voice Agent launcher.

Copy the scripts and face assets:

```bash
mkdir -p /home/RACHEL/RACHEL/facialExpressions
# Copy the Python files into /home/RACHEL/RACHEL/.
# Copy LCD Output/facialExpressions/*.png into /home/RACHEL/RACHEL/facialExpressions/.

cd /home/RACHEL/RACHEL
sudo raspi-config nonint do_spi 0
sudo usermod -aG gpio,spi,video,input RACHEL
python3 -m venv venv
./venv/bin/pip install paho-mqtt pygame RPi.GPIO adafruit-blinka adafruit-circuitpython-neopixel-spi
```

Reboot after enabling SPI or changing group membership.

Test the unified subscriber before installing or restarting services:

```bash
/home/RACHEL/RACHEL/venv/bin/python /home/RACHEL/RACHEL/face_agent.py \
  --broker 10.0.0.1 --port 1883 \
  --venv /home/RACHEL/RACHEL/venv --base /home/RACHEL/RACHEL
```

In another Pi terminal, verify MQTT traffic:

```bash
ping -c 4 10.0.0.1
mosquitto_sub -h 10.0.0.1 -p 1883 -t 'robot/pi/#' -v
```

## systemd Configuration

Use these service definitions if the Pi does not already have equivalent units.

`/etc/systemd/system/face-agent.service`:

```ini
[Unit]
Description=Voice Agent Pi hardware subscriber
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=RACHEL
WorkingDirectory=/home/RACHEL/RACHEL
ExecStart=/home/RACHEL/RACHEL/venv/bin/python /home/RACHEL/RACHEL/face_agent.py --broker 10.0.0.1 --port 1883 --venv /home/RACHEL/RACHEL/venv --base /home/RACHEL/RACHEL
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/face-neutral.service`:

```ini
[Unit]
Description=Voice Agent Pi face renderer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=RACHEL
WorkingDirectory=/home/RACHEL/RACHEL
Environment=FACE_BROKER=10.0.0.1
Environment=FACE_BROKER_PORT=1883
ExecStart=/home/RACHEL/RACHEL/venv/bin/python /home/RACHEL/RACHEL/faceScript.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable and inspect them:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now face-agent.service face-neutral.service
systemctl status face-agent.service face-neutral.service --no-pager
journalctl -u face-agent.service -u face-neutral.service -n 100 --no-pager
```

Only one face subscriber should process `robot/pi/face/cmd`. The current `face_agent.py` intentionally leaves face rendering to `faceScript.py`.
