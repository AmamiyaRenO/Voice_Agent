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
