# Rachel Station — Complete Setup Guide
### Windows Mini PC + Raspberry Pi 5

> This document is the authoritative setup guide for a fresh Rachel station. It combines the original `FRESH_MACHINE_SETUP.md`, the full project README, and every fix and lesson learned from real deployment on a Raspberry Pi 5 running Bookworm. Follow it top to bottom on a new machine — do not skip sections.

---

## Table of Contents

1. [What You Need](#1-what-you-need)
2. [Critical Warnings — Read Before You Start](#2-critical-warnings--read-before-you-start)
3. [Flash and Customise the SD Card](#3-flash-and-customise-the-sd-card)
4. [Set Up the Windows Mini PC](#4-set-up-the-windows-mini-pc)
5. [Configure the Direct Ethernet Link](#5-configure-the-direct-ethernet-link)
6. [Set Up the Raspberry Pi](#6-set-up-the-raspberry-pi)
7. [Install Pi Services (Auto-Start on Boot)](#7-install-pi-services-auto-start-on-boot)
8. [Display Customisation](#8-display-customisation)
9. [Face Expressions Setup](#9-face-expressions-setup)
10. [End-to-End Test](#10-end-to-end-test)
11. [Servo Fix for Raspberry Pi 5](#11-servo-fix-for-raspberry-pi-5)
12. [PC Auto Power-On When Plugged In](#12-pc-auto-power-on-when-plugged-in)
13. [Shutdown Pi Safely from the PC Desktop](#13-shutdown-pi-safely-from-the-pc-desktop)
14. [Optional Unity or Game Setup](#14-optional-unity-or-game-setup)
15. [What to Send When Setup Fails](#15-what-to-send-when-setup-fails)
16. [Updating Later](#16-updating-later)

---

## 1. What You Need

**Hardware**

- Windows 11 mini PC with internet access, a microphone, speakers, and an Ethernet port
- Raspberry Pi 5 (2 GB or 4 GB) running 64-bit Raspberry Pi OS 12 (Bookworm) — Desktop version, not Lite
- A **good quality** microSD card — 32 GB minimum, 64 GB recommended (see SD card warning below)
- A direct Ethernet cable between the PC and Pi (both devices can keep Wi-Fi for internet)
- Rachel hardware connected to the Pi: LCD display, servo (flower), NeoPixel LEDs

**Credentials**

- A Gemini API key if you will use Gemini conversation or Gemini Live recognition
- A separate Google Cloud Text-to-Speech API key if you will use Google Cloud TTS — a Gemini key cannot replace this

**Username convention**

> ⚠️ **Your Pi username matters everywhere in this guide.** Every path like `/home/rachel/RACHEL` and every `User=rachel` line in service files must match the username you chose when flashing the SD card. This guide uses `rachel` as the example. If your Pi username is different (e.g. `RACHEL`), replace `rachel` with your actual username in every command and every file throughout this guide.

> **Do not send API keys in chat and do not commit `scripts/local_services.user.json`.** That file is intentionally ignored by Git.

---

## 2. Critical Warnings — Read Before You Start

### SD Card Power Loss = Corruption

**This is the most common way to lose hours of work.** If the Raspberry Pi loses power while it is writing to the SD card — during an `apt install`, a `pip install`, or any file write — the SD card can become corrupted and unbootable. Recovery is not guaranteed.

**Always:**
- Use a reliable power supply for the Pi (official Raspberry Pi 5 27W USB-C PSU recommended)
- Never pull the power cable while the Pi is running
- Always shut down cleanly: `sudo shutdown now` — wait for the activity LED to stop blinking before cutting power
- Keep a second blank SD card as a spare so a corruption doesn't halt your project

### Raspberry Pi 5 GPIO — RPi.GPIO Does Not Work

`RPi.GPIO` was written for Pi 1–4 and **cannot control GPIO on the Pi 5**. It will crash with:

```
RuntimeError: Cannot determine SOC peripheral base address
```

The LED works because it uses a different library (`adafruit-circuitpython-neopixel-spi`). The servo will not work until you replace `RPi.GPIO` with `lgpio`. This guide covers the fix in [Section 11](#11-servo-fix-for-raspberry-pi-5).

### Pygame on Bookworm — Wayland Blocks the Display

Raspberry Pi OS 12 (Bookworm) uses Wayland by default. A systemd service running without a desktop session token cannot open a Pygame window. The fix is covered in [Section 7](#7-install-pi-services-auto-start-on-boot) — the service must run as your user with `DISPLAY=:0` and `SDL_VIDEODRIVER=x11`.

### MQTT Broker Must Listen on All Interfaces

The checked-in Mosquitto configuration listens on `0.0.0.0:1883`, but an older or separately installed broker may still bind only to `127.0.0.1`. The Pi cannot reach a localhost-only broker. Before testing Pi connectivity, confirm which address owns port `1883`. This is covered in [Section 4.6](#46-confirm-the-mqtt-broker-is-reachable-from-the-pi).

---

## 3. Flash and Customise the SD Card

1. Download **Raspberry Pi Imager** on your PC: https://www.raspberrypi.com/software/
2. Insert the SD card into your PC
3. In Imager, choose:
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS (64-bit) — the **Desktop** version, not Lite. Pygame needs a desktop environment.
   - **Storage:** your SD card
4. Click the **gear / settings icon** before writing and configure:
   - Hostname: `RACHEL` (or your preferred name — this is your Pi's network name)
   - Username: `rachel` (or your preferred username — **remember this exactly, you will use it everywhere**)
   - Password: set a strong password and remember it
   - Enable SSH: yes, use password authentication
   - Wi-Fi: enter your network credentials so the Pi has internet on first boot
   - Locale: set your timezone and keyboard layout
5. Click **Write** and wait for it to finish and verify
6. Insert the SD card into the Pi and boot

> If the SD card gets corrupted at any point, re-flash from scratch using these same steps. Always start from a fresh flash — never try to repair a corrupted card.

---

## 4. Set Up the Windows Mini PC

### 4.1 Clone the Repository

Install Git for Windows, open PowerShell, and run:

```powershell
New-Item -ItemType Directory -Force C:\unityproject | Out-Null
Set-Location C:\unityproject
git clone https://github.com/AmamiyaRenO/Voice_Agent.git
Set-Location .\Voice_Agent
git status
```

`git status` should say the branch is up to date and the working tree is clean.

### 4.2 Start Rachel for the First Time

```powershell
.\helper.bat
```

The first start can take several minutes — it creates Python environments and downloads local models. Keep the terminal open. When startup completes, open these pages:

| Page | URL |
|---|---|
| Main console | `http://127.0.0.1:8787` |
| Setup checks | `http://127.0.0.1:8787/setup` |
| Settings | `http://127.0.0.1:8787/runtime.html` |
| Hardware controls | `http://127.0.0.1:8787/controls.html#controller` |

### 4.3 Browser Opens Automatically

The checked-in `helper.bat` already waits until `http://127.0.0.1:8787/healthz` responds and then opens the Rachel Console. Do not edit the batch file or add a second delayed browser command.

To suppress automatic opening for one PowerShell session:

```powershell
$env:VOICE_AGENT_OPEN_PANEL = "0"
.\helper.bat
```

### 4.4 Choose One Speech-Input Path

Configure explicitly in **Settings > Advanced Configuration**:

**Option A — Gemini Live (recommended for new cloud-connected stations)**

1. Select the cloud/Gemini conversation profile
2. Select Gemini Live as the streaming recognition mode
3. Enter the Gemini API key in Credentials and save
4. Restart `helper.bat` if the page says managed services need a restart

**Option B — Windows Live Captions**

Requires the separate `EnableLcMic.exe` listener (not included in the repo). Set `LIVE_CAPTIONS_LISTENER_EXE` to the full path of that executable, enable Windows Live Captions with microphone audio (`Win + Ctrl + L`), and restart. If this executable has not been supplied, use Gemini Live instead.

### 4.5 Configure Speech Output

Choose the default backend in **Settings > Advanced Configuration > Models and TTS**, then click **Save Configuration**. The Controller page can audition a backend during the current session, but the Settings save controls what is selected after the next restart.

#### Piper (local and recommended when cloud TTS is not required)

Piper binaries and voice models are intentionally not stored in Git because they are large platform-specific files, and the launcher does not download them automatically. Obtain the tested Windows Piper package from the Rachel station maintainer, or install Piper and a compatible voice model separately.

For the standard station layout, extract the package so these files exist:

```text
D:\piper\piper.exe
D:\piper\models\en_US-amy-medium.onnx
D:\piper\models\en_US-amy-medium.onnx.json
```

Merge the following keys into the existing `env` object in `scripts\local_services.user.json`. Do not replace unrelated machine-specific settings already in that file.

```json
{
  "env": {
    "VOICE_AGENT_TTS_BACKEND": "piper",
    "PIPER_EXECUTABLE": "D:\\piper\\piper.exe",
    "VOICE_MODELS_DIR": "D:\\piper\\models",
    "PIPER_MODELS_DIR": "D:\\piper\\models",
    "PIPER_MODEL_PATH": "D:\\piper\\models\\en_US-amy-medium.onnx",
    "PIPER_CONFIG_PATH": "D:\\piper\\models\\en_US-amy-medium.onnx.json"
  }
}
```

Restart `helper.bat`, open **Controller**, select Piper, and press **Speak**. If the model appears but no sound is heard, test synthesis independently:

```powershell
Invoke-WebRequest "http://127.0.0.1:5005/speak?text=Hello" `
  -OutFile "$env:TEMP\piper-test.wav"
Start-Process "$env:TEMP\piper-test.wav"
```

If the WAV plays, Piper is working and Windows is using the wrong default output device. Select the intended speaker in Windows and restart `helper.bat`.

#### Google Cloud TTS

Google Cloud TTS uses a separate credential from Gemini. Enable the Cloud Text-to-Speech API for the Google Cloud project, save its key under **Settings > Credentials**, and select a compatible voice. Never paste a key into chat or include it in a screenshot; revoke any key that has been exposed.

### 4.6 Confirm the MQTT Broker is Reachable from the Pi

In a second PowerShell window:

```powershell
Get-NetTCPConnection -LocalPort 8787,1883 -State Listen
```

Check the `LocalAddress` column for port `1883`. It **must** show `0.0.0.0` or `::` — not `127.0.0.1`. If it shows `127.0.0.1`, the Pi cannot reach the broker over the Ethernet cable.

**Fix:** Open `scripts/mqtt/mosquitto.conf` in the repository and confirm these two lines exist:

```conf
listener 1883 0.0.0.0
allow_anonymous true
```

Save, stop `helper.bat` with `Ctrl+C`, and restart it. Re-run the check — `LocalAddress` must now show `0.0.0.0`.

Also confirm the health endpoint responds:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/healthz
```

---

## 5. Configure the Direct Ethernet Link

Use this fixed network for the direct cable between PC and Pi:

| Device | Ethernet Address |
|---|---|
| Windows mini PC | `10.0.0.1/24` |
| Raspberry Pi | `10.0.0.2/24` |

Keep Wi-Fi enabled on both devices. Do not add a gateway to this Ethernet link.

### On the PC (PowerShell as Administrator)

```powershell
Get-NetAdapter
# This lists your network adapters. Replace "Ethernet" below with the actual
# name of your wired adapter if it is different (e.g. "Local Area Connection").
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.0.0.1 -PrefixLength 24
New-NetFirewallRule -DisplayName "Voice Agent MQTT" -Direction Inbound -Protocol TCP -LocalPort 1883 -RemoteAddress 10.0.0.2 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "Voice Agent Panel" -Direction Inbound -Protocol TCP -LocalPort 8787 -RemoteAddress 10.0.0.2 -Action Allow -Profile Any
```

### On the Pi (SSH or terminal)

```bash
nmcli connection show
# This lists your network connections. Replace "Wired connection 1" below with
# the actual name of your wired connection shown in the output above.
sudo nmcli connection modify "Wired connection 1" \
  ipv4.method manual ipv4.addresses 10.0.0.2/24 \
  ipv4.gateway "" ipv4.dns "" ipv4.never-default yes
sudo nmcli connection up "Wired connection 1"
ip -4 addr show
ping -c 4 10.0.0.1
```

From the PC, also confirm:

```powershell
ping 10.0.0.2
```

Both pings must show 0% packet loss before continuing. This connection survives reboots once set.

---

## 6. Set Up the Raspberry Pi

### 6.1 Enable Autologin to the Desktop

The face display service requires a desktop session at `:0`. Make sure autologin is enabled:

```bash
sudo raspi-config
```

Go to **System Options → Boot / Auto Login → Desktop Autologin**. This ensures the Pygame window can open when the service starts at boot.

### 6.2 Copy Hardware Files from the PC

From the repository folder on the PC (PowerShell):

> ⚠️ Replace `rachel` in the commands below with your Pi's actual username if it is different.

```powershell
ssh rachel@10.0.0.2 "mkdir -p /home/rachel/RACHEL/facialExpressions"
scp Firmware\face_agent.py Firmware\faceScript.py Firmware\servoScript.py Firmware\ledScript.py Firmware\face_agent.env rachel@10.0.0.2:/home/rachel/RACHEL/
scp "Firmware\LCD Output\facialExpressions\*.png" rachel@10.0.0.2:/home/rachel/RACHEL/facialExpressions/
```

Verify the files arrived on the Pi:

```bash
ls /home/rachel/RACHEL/
# Replace /home/rachel with /home/YOUR_USERNAME if different
ls /home/rachel/RACHEL/facialExpressions/
```

You should see the `.py` files, `face_agent.env`, and all the `.png` expression images.

### 6.3 Install Pi Dependencies

SSH into the Pi and run:

> ⚠️ Replace `rachel` with your Pi username where it appears in these commands.

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip mosquitto-clients python3-lgpio
sudo raspi-config nonint do_spi 0
sudo usermod -aG gpio,spi,video,input "$USER"
cd /home/rachel/RACHEL
# Replace /home/rachel with your actual home directory if different
python3 -m venv venv
./venv/bin/pip install paho-mqtt pygame RPi.GPIO adafruit-blinka adafruit-circuitpython-neopixel-spi
sudo reboot
```

> **Why `python3-lgpio` via apt?** The Pi 5 requires `lgpio` for GPIO/servo control instead of `RPi.GPIO`. Installing via `apt` bypasses pip's SSL certificate checks, which can fail if the Pi's clock is wrong or internet is not yet connected.

After the Pi reboots and you reconnect via SSH, link `lgpio` into the venv:

```bash
ln -s /usr/lib/python3/dist-packages/lgpio.py /home/rachel/RACHEL/venv/lib/python3*/site-packages/ 2>/dev/null || true
ln -s /usr/lib/python3/dist-packages/lgpio*.so /home/rachel/RACHEL/venv/lib/python3*/site-packages/ 2>/dev/null || true
# Replace /home/rachel with your actual home directory if different
```

Confirm it works:

```bash
cd /home/rachel/RACHEL
./venv/bin/python -c "import lgpio; print('lgpio OK')"
```

### 6.4 Test the Connection Manually

Before installing services, confirm the Python app connects to the broker:

```bash
cd /home/rachel/RACHEL
./venv/bin/python ./face_agent.py \
  --broker 10.0.0.1 --port 1883 \
  --venv /home/rachel/RACHEL/venv --base /home/rachel/RACHEL
# Replace /home/rachel with your actual home directory if different
```

You should see it log `connected, subscribing to...` and stay listening. Stop it with `Ctrl+C`. If it cannot connect, fix the Ethernet/firewall/MQTT broker step before proceeding.

---

## 7. Install Pi Services (Auto-Start on Boot)

Two systemd services make the face display and hardware agent start automatically on every boot.

> ⚠️ Replace every occurrence of `rachel` in the service files below with your actual Pi username if it is different. This includes the `User=` line and every file path.

### 7.1 Create face-agent.service

This handles servo and LED — no display needed:

```bash
sudo nano /etc/systemd/system/face-agent.service
```

Paste exactly (replacing `rachel` with your Pi username if different):

```ini
[Unit]
Description=Rachel Hardware Agent (servo + LED)
After=network.target

[Service]
Type=simple
User=rachel
WorkingDirectory=/home/rachel/RACHEL
ExecStart=/home/rachel/RACHEL/venv/bin/python /home/rachel/RACHEL/face_agent.py \
    --broker 10.0.0.1 --port 1883 \
    --venv /home/rachel/RACHEL/venv --base /home/rachel/RACHEL
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

### 7.2 Create face-neutral.service

This runs the Pygame face display. The `Environment=` lines fix Bookworm's Wayland block:

```bash
sudo nano /etc/systemd/system/face-neutral.service
```

Paste exactly (replacing `rachel` with your Pi username if different):

```ini
[Unit]
Description=Rachel Face Renderer (Pygame display)
After=graphical-session.target network.target
Wants=graphical-session.target

[Service]
Type=simple
User=rachel
WorkingDirectory=/home/rachel/RACHEL
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/rachel/.Xauthority
Environment=SDL_VIDEODRIVER=x11
Environment=FACE_BROKER=10.0.0.1
Environment=FACE_BROKER_PORT=1883
ExecStartPre=/bin/sleep 15
ExecStart=/home/rachel/RACHEL/venv/bin/python /home/rachel/RACHEL/faceScript.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical.target
```

> The `ExecStartPre=/bin/sleep 15` gives the desktop session time to fully start before Pygame tries to open a window.

### 7.3 Enable and Start Both Services

```bash
sudo systemctl daemon-reload
sudo systemctl enable face-agent.service face-neutral.service
sudo systemctl start face-agent.service face-neutral.service
```

### 7.4 Verify Both Services are Running

```bash
systemctl status face-agent.service face-neutral.service --no-pager
journalctl -u face-agent.service -u face-neutral.service -n 50 --no-pager
```

Both should show `active (running)`.

### 7.5 Confirm Auto-Start is Registered

```bash
systemctl is-enabled face-agent.service face-neutral.service
```

Both must say `enabled`. Only after this is it safe to reboot — the face will return automatically after every boot.

---

## 8. Display Customisation

### 8.1 Hide the Mouse Cursor on Boot

The mouse cursor appears over the face display by default. These steps make it permanently invisible.

**Step 1 — Install unclutter**

```bash
sudo apt install -y unclutter
```

**Step 2 — Create the user service**

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/hide-cursor.service
```

Paste (replacing `/home/rachel` with your actual home directory if different):

```ini
[Unit]
Description=Hide mouse cursor
After=graphical-session.target

[Service]
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/rachel/.Xauthority
ExecStartPre=/bin/sleep 20
ExecStart=unclutter -idle 0 -root -display :0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

> ⚠️ The `XAUTHORITY` path must match your Pi username. If your username is `RACHEL`, the path is `/home/RACHEL/.Xauthority`.

**Step 3 — Enable and start**

```bash
systemctl --user enable hide-cursor.service
systemctl --user start hide-cursor.service
```

**Step 4 — Allow the service to start at boot without manual login**

> ⚠️ Replace `rachel` below with your actual Pi username.

```bash
sudo loginctl enable-linger rachel
```

**Step 5 — Reboot and verify**

```bash
sudo reboot
```

After reconnecting via SSH:

```bash
systemctl --user status hide-cursor.service
```

Should show `active (running)`. The cursor will be invisible on the face display from this point on after every boot.

> **Why the `sleep 20`?** On Bookworm, the desktop session takes a moment to fully initialise after boot. Without the delay, `unclutter` starts before X11 is ready, fails, retries too fast, and gives up permanently.

---

### 8.2 Invert the Screen (if your display is mounted upside-down)

If the LCD is physically mounted upside-down in the enclosure, use this to rotate it 180°.

**Step 1 — Check your display output name**

First confirm which HDMI output your screen is connected to:

```bash
wlr-randr
```

Look for the output that shows your screen resolution — it will be named something like `HDMI-A-1` or `HDMI-A-2`. Note the exact name.

> ⚠️ If you reassemble the hardware or reconnect the display to a different port, this name may change. Always re-run `wlr-randr` first if rotation stops working after hardware changes.

**Step 2 — Create the autostart folder**

```bash
mkdir -p ~/.config/autostart
```

**Step 3 — Create the autostart file**

```bash
nano ~/.config/autostart/rotate-screen.desktop
```

Paste (replacing `HDMI-A-2` with your actual output name from Step 1):

```
[Desktop Entry]
Type=Application
Name=Rotate Screen
Exec=wlr-randr --output HDMI-A-2 --transform 180
NoDisplay=true
```

**Step 4 — Save the file**

Press `Ctrl+O`, then `Enter`, then `Ctrl+X`.

**Step 5 — Reboot**

```bash
sudo reboot
```

The screen will now be rotated 180° on every boot. If it stops working after hardware changes, re-run `wlr-randr` to confirm the output name hasn't changed and update the file if needed.

---

## 9. Face Expressions Setup

Rachel's face display works by matching the `mode` name sent from the PC to a PNG filename in `/home/rachel/RACHEL/facialExpressions/`. If a PNG for a mode is missing, the display falls back to the default (neutral) face.

### 9.1 Copy All Expression PNGs from the PC

The full set of expression PNGs lives in the repository on the PC at:

```
C:\unityproject\Voice_Agent\Firmware\LCD Output\facialExpressions\
```

Copy all of them to the Pi (replace `rachel` with your Pi username if different):

```powershell
scp "C:\unityproject\Voice_Agent\Firmware\LCD Output\facialExpressions\*.png" rachel@10.0.0.2:/home/rachel/RACHEL/facialExpressions/
```

Verify the count on the Pi:

```bash
ls /home/rachel/RACHEL/facialExpressions/ | wc -l
```

A complete installation should show **45 files**. If it shows fewer, some expressions will fall back to neutral when triggered.

### 9.2 Fix Missing Basic Emotion PNGs

The basic emotions panel on the localhost (`confused`, `concerned`, `upset`) look for PNGs named exactly that. If those files are missing, create symlinks pointing them to the Avatar A versions:

```bash
cd /home/rachel/RACHEL/facialExpressions/
# Replace /home/rachel with your actual home directory if different

# Only run each line if that file does not already exist
[ -f confused.png ]  || ln -s AConfused.png confused.png
[ -f concerned.png ] || ln -s AConcerned.png concerned.png
[ -f upset.png ]     || ln -s AUpset.png upset.png
```

### 9.3 Set the Default (Idle) Face

The script picks the default face by searching for any PNG whose name contains the string set in `neutral_name`. By default this is `"neutral"`, which can match `ANeutral`, `BNeutral`, `CNeutral`, `DNeutral`, and `neutral` — the result is unpredictable.

To pin the default face to a specific expression (e.g. `ANeutral`), make two changes in `faceScript.py`:

```bash
nano /home/rachel/RACHEL/faceScript.py
# Replace /home/rachel with your actual home directory if different
```

**Change 1** — find:
```python
neutral_name = "neutral"  # 文件名包含该子串的 PNG 为默认表情
```
Change to (use whichever face you want as default):
```python
neutral_name = "ANeutral"
```

**Change 2** — find:
```python
neutral_file = pick_image("neutral", files)
```
Change to:
```python
neutral_file = pick_image(neutral_name, files)
```

Save with `Ctrl+O`, `Enter`, `Ctrl+X`, then restart the service:

```bash
sudo systemctl restart face-neutral.service
```

### 9.4 How Face Mode Names Map to PNG Files

The `pick_image` function does a **substring match** — so the mode name just needs to be contained in the PNG filename. The full expression set and their mode names are:

| Localhost button | Mode name sent | PNG file matched |
|---|---|---|
| neutral | `neutral` | `neutral.png` |
| happy | `happy` | `happy.png` |
| excited | `excited` | `excited.png` |
| sad | `sad` | `sad.png` |
| verySad | `verySad` | `verySad.png` |
| confused | `confused` | `confused.png` → `AConfused.png` |
| concerned | `concerned` | `concerned.png` → `AConcerned.png` |
| upset | `upset` | `upset.png` → `AUpset.png` |
| Avatar A — Neutral | `ANeutral` | `ANeutral.png` |
| Avatar A — Happy | `AHappy` | `AHappy.png` |
| Avatar A — Concerned | `AConcerned` | `AConcerned.png` |
| Avatar A — Confused | `AConfused` | `AConfused.png` |
| Avatar A — Upset | `AUpset` | `AUpset.png` |
| Avatar B — Neutral | `BNeutral` | `BNeutral.png` |
| Avatar B — Happy | `BHappy` | `BHappy.png` |
| Avatar B — Concerned | `BConcerned` | `BConcerned.png` |
| Avatar B — Confused | `BConfused` | `BConfused.png` |
| Avatar B — Upset | `BUpset` | `BUpset.png` |
| Avatar C — Neutral | `CNeutral` | `CNeutral.png` |
| Avatar C — Happy | `CHappy` | `CHappy.png` |
| Avatar C — Concerned | `CConcerned` | `CConcerned.png` |
| Avatar C — Confused | `CConfused` | `CConfused.png` |
| Avatar C — Upset | `CUpset` | `CUpset.png` |
| Avatar D — Neutral | `DNeutral` | `DNeutral.png` |
| Avatar D — Happy | `DHappy` | `DHappy.png` |
| Avatar D — Concerned | `DConcerned` | `DConcerned.png` |
| Avatar D — Confused | `DConfused` | `DConfused.png` |
| Avatar D — Upset | `DUpset` | `DUpset.png` |

You can also trigger any expression via the API directly from the PC:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/face `
  -ContentType "application/json" `
  -Body '{"mode":"AHappy","seconds":5}'
```

### 9.5 Transfer PNGs Between Two Separate Stations

If your working station and new station are not on the same network, the easiest transfer method is:

1. On the working Pi, zip the expressions folder:
   ```bash
   cd /home/RACHEL/RACHEL
   # Replace /home/RACHEL with your working Pi's home directory
   zip facialExpressions.zip facialExpressions/*.png
   ```
2. Copy the zip to the working PC (via `scp` or USB) and email it to yourself
3. On the new PC, download and extract the zip
4. Copy the PNGs to the new Pi:
   ```powershell
   scp "C:\Users\Admin\Downloads\facialExpressions\*.png" rachel@10.0.0.2:/home/rachel/RACHEL/facialExpressions/
   # Replace C:\Users\Admin\Downloads with your actual download path
   # Replace rachel with your new Pi's username
   ```
5. Restart the face service on the new Pi:
   ```bash
   sudo systemctl restart face-neutral.service
   ```

---

## 10. End-to-End Test

With `helper.bat` running on the PC:

### Test 1 — Watch MQTT Traffic on the Pi

On the Pi:

```bash
mosquitto_sub -h 10.0.0.1 -p 1883 -t 'robot/pi/#' -v
```

### Test 2 — Send a Face Command from the PC

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/face `
  -ContentType "application/json" `
  -Body '{"mode":"happy","seconds":5}'
```

The Pi terminal should show the incoming MQTT message and the display should change to the happy expression.

### Test 3 — LED Control

Open `http://127.0.0.1:8787/controls.html#controller` on the PC and use the LED controls. The NeoPixel strip should respond immediately.

### Test 4 — Flower / Servo Control

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/flower `
  -ContentType "application/json" `
  -Body '{"action":"open"}'
```

The servo should move. If it does not, see [Section 11](#11-servo-fix-for-raspberry-pi-5).

### Test 5 — Voice

Test listening and speech from the main console at `http://127.0.0.1:8787`.

---

## 11. Servo Fix for Raspberry Pi 5

`RPi.GPIO` does not support the Pi 5 and will crash with `RuntimeError: Cannot determine SOC peripheral base address`. This section replaces it with `lgpio`.

> If you followed Section 6.3 and already ran `sudo apt install -y python3-lgpio` and linked it into the venv, skip to Step 2.

### Step 1 — Install lgpio

```bash
sudo apt update
sudo apt install -y python3-lgpio
ln -s /usr/lib/python3/dist-packages/lgpio.py /home/rachel/RACHEL/venv/lib/python3*/site-packages/ 2>/dev/null || true
ln -s /usr/lib/python3/dist-packages/lgpio*.so /home/rachel/RACHEL/venv/lib/python3*/site-packages/ 2>/dev/null || true
# Replace /home/rachel with your actual home directory if different
./venv/bin/python -c "import lgpio; print('lgpio OK')"
```

### Step 2 — Back Up the Original Script

```bash
sudo systemctl stop face-agent.service
cp /home/rachel/RACHEL/servoScript.py /home/rachel/RACHEL/servoScript.py.bak
# Replace /home/rachel with your actual home directory if different
```

### Step 3 — Write the Pi 5 Compatible servoScript.py

```bash
cat > /home/rachel/RACHEL/servoScript.py << 'EOF'
#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import time

import lgpio

SERVO_GPIO = 17          # BCM GPIO17 = physical pin 11
PWM_FREQUENCY = 50
OPEN_ANGLE = 0.0
CLOSE_ANGLE = 180.0
CENTER_ANGLE = 90.0
DEFAULT_DURATION = 2.0
DEFAULT_SETTLE = 0.45
FAST_STEP_DEGREES = 4.0
SLOW_STEP_DEGREES = 4.0
FAST_STEP_DELAY = 0.01
SLOW_STEP_DELAY = 0.03
MIN_SPEED_PERCENT = 5.0
MAX_SPEED_PERCENT = 100.0
ANGLE_EPSILON = 2.0
STATE_PATH = Path(__file__).with_name("servo_state.json")

CHIP = lgpio.gpiochip_open(0)
lgpio.gpio_claim_output(CHIP, SERVO_GPIO)


def _duty_from_angle(angle: float) -> float:
    return 2.0 + (angle / 18.0)


def _apply_angle(angle: float) -> None:
    lgpio.tx_pwm(CHIP, SERVO_GPIO, PWM_FREQUENCY, _duty_from_angle(angle))


def _release() -> None:
    lgpio.tx_pwm(CHIP, SERVO_GPIO, 0, 0)


def _load_state_angle() -> float | None:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        angle = float(data.get("angle"))
        if 0.0 <= angle <= 180.0:
            return angle
    except Exception:
        return None
    return None


def _save_state_angle(angle: float) -> None:
    try:
        STATE_PATH.write_text(json.dumps({"angle": round(float(angle), 3)}), encoding="utf-8")
    except Exception:
        pass


def _speed_to_delay(speed_percent: float | None, force_slow: bool) -> float:
    if speed_percent is None:
        return SLOW_STEP_DELAY if force_slow else FAST_STEP_DELAY
    clamped = max(MIN_SPEED_PERCENT, min(MAX_SPEED_PERCENT, float(speed_percent)))
    span = SLOW_STEP_DELAY - FAST_STEP_DELAY
    return SLOW_STEP_DELAY - ((clamped - MIN_SPEED_PERCENT) / (MAX_SPEED_PERCENT - MIN_SPEED_PERCENT)) * span


def _move_to(target_angle: float, *, speed_percent: float | None, force_slow: bool, start_hint: float | None) -> None:
    if speed_percent is None and not force_slow:
        _apply_angle(target_angle)
        time.sleep(DEFAULT_SETTLE)
        _save_state_angle(target_angle)
        return
    current = target_angle if start_hint is None else start_hint
    step_delay = _speed_to_delay(speed_percent, force_slow)
    step_degrees = SLOW_STEP_DEGREES if force_slow else FAST_STEP_DEGREES
    _apply_angle(current)
    _save_state_angle(current)
    while abs(target_angle - current) > 0.001:
        step = min(step_degrees, abs(target_angle - current))
        current += step if target_angle >= current else -step
        _apply_angle(current)
        _save_state_angle(current)
        time.sleep(step_delay)
    time.sleep(DEFAULT_SETTLE)
    _save_state_angle(target_angle)


def _hold_until_stopped() -> None:
    try:
        while True:
            time.sleep(0.25)
    finally:
        _release()


def _parse_float(raw_value: str | None, default: float) -> float:
    if raw_value in (None, ""):
        return default
    try:
        return float(raw_value)
    except Exception:
        return default


def _parse_args(raw_mode: str, raw_duration: str | None, raw_speed: str | None) -> tuple[str, float, float | None]:
    mode = (raw_mode or "").strip().lower()
    duration = DEFAULT_DURATION
    speed_percent = None
    if ":" in mode and not raw_duration:
        mode, raw_duration = mode.split(":", 1)
        mode = mode.strip().lower()
        raw_duration = raw_duration.strip()
    duration = _parse_float(raw_duration, DEFAULT_DURATION)
    if raw_speed not in (None, ""):
        speed_percent = _parse_float(raw_speed, MAX_SPEED_PERCENT)
    return mode, duration, speed_percent


def _shutdown_pwm() -> None:
    try:
        _release()
    except Exception:
        pass
    try:
        lgpio.gpiochip_close(CHIP)
    except Exception:
        pass


def run(raw_mode: str, raw_duration: str | None = None, raw_speed: str | None = None) -> None:
    mode, duration, speed_percent = _parse_args(raw_mode, raw_duration, raw_speed)
    hold_forever = mode in {"open_hold", "close_hold", "center_hold"} or (
        mode in {"open", "close", "center"} and duration == 0.0
    )
    if mode in {"open_slow", "close_slow"} and raw_duration in (None, ""):
        duration = 0.0
    target_map = {
        "open": OPEN_ANGLE,
        "open_hold": OPEN_ANGLE,
        "open_slow": OPEN_ANGLE,
        "flower": OPEN_ANGLE,
        "open_flower": OPEN_ANGLE,
        "close": CLOSE_ANGLE,
        "close_hold": CLOSE_ANGLE,
        "close_slow": CLOSE_ANGLE,
        "close_flower": CLOSE_ANGLE,
        "shut": CLOSE_ANGLE,
        "center": CENTER_ANGLE,
        "center_hold": CENTER_ANGLE,
        "middle": CENTER_ANGLE,
    }
    start_hint_map = {
        OPEN_ANGLE: CLOSE_ANGLE,
        CLOSE_ANGLE: OPEN_ANGLE,
        CENTER_ANGLE: CENTER_ANGLE,
    }

    try:
        if mode in {"stop", "release", "idle", "off", "none"}:
            _release()
            return

        if mode in {"pulse", "breath", "breathe"}:
            end_time = time.time() + max(0.0, duration)
            while time.time() < end_time:
                _move_to(OPEN_ANGLE, speed_percent=speed_percent, force_slow=True, start_hint=CLOSE_ANGLE)
                _release()
                _move_to(CLOSE_ANGLE, speed_percent=speed_percent, force_slow=True, start_hint=OPEN_ANGLE)
                _release()
            return

        target = target_map.get(mode)
        if target is None:
            target = OPEN_ANGLE

        force_slow = mode.endswith("_slow") or speed_percent is not None
        current_angle = _load_state_angle()
        if current_angle is not None and abs(current_angle - target) <= ANGLE_EPSILON:
            _save_state_angle(target)
            return
        _move_to(
            target,
            speed_percent=speed_percent,
            force_slow=force_slow,
            start_hint=current_angle if current_angle is not None else (start_hint_map.get(target) if force_slow else None),
        )

        if hold_forever:
            _hold_until_stopped()
            return

        time.sleep(max(0.0, duration))
        _release()
    finally:
        if not hold_forever:
            _shutdown_pwm()


if __name__ == "__main__":
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    duration_arg = sys.argv[2] if len(sys.argv) > 2 else None
    speed_arg = sys.argv[3] if len(sys.argv) > 3 else None
    run(mode_arg, duration_arg, speed_arg)
EOF
```

> ⚠️ Replace `/home/rachel/RACHEL/servoScript.py` with your actual home directory path if different.

### Step 4 — Test the Servo Directly

```bash
cd /home/rachel/RACHEL
# Replace /home/rachel with your actual home directory if different
./venv/bin/python /home/rachel/RACHEL/servoScript.py center
./venv/bin/python /home/rachel/RACHEL/servoScript.py open
./venv/bin/python /home/rachel/RACHEL/servoScript.py close
```

The servo should move to each position.

### Step 5 — Restart the Service

```bash
sudo systemctl start face-agent.service
journalctl -u face-agent.service -f
```

No traceback should appear.

### Servo Wiring Reference

| Servo wire | Connects to |
|---|---|
| Signal (orange/yellow) | Pi physical pin 11 (GPIO17 BCM) |
| Power (red) | External DC-DC board positive (5V) |
| Ground (brown/black) | DC-DC board GND **and** Pi GND pin (shared ground is mandatory) |

> **Shared ground is critical.** If the Pi and the DC-DC board do not share a common ground, the PWM signal has no reference and the servo will not move.

> **Servo spins when Pi is off?** This is a floating signal pin. Add a 10 kΩ resistor between the signal wire and ground. Alternatively, always power the DC-DC board after the Pi is booted and cut it before shutting the Pi down.

---

## 12. PC Auto Power-On When Plugged In

This setting makes the PC boot automatically whenever it receives power — no button press needed.

**Step 1 — Enter BIOS**

Plug in the PC, then immediately and repeatedly press `Delete` as it starts. You have about 2 seconds from the moment the screen lights up.

> ⚠️ The BIOS key varies by PC manufacturer. For GMKtec mini PCs use `Delete`. Other brands may use `F2` or `F10`. Check your PC's manual if `Delete` does not work.

**Step 2 — Find the setting**

Navigate to: **Advanced → Auto Power On** → set to **Power On**

> The menu path varies slightly by BIOS version. If you don't see "Auto Power On", look for:
> - `Advanced → Power Management → AC Power Recovery → Power On`
> - `Chipset → South Bridge → Restore on AC Power Loss → Power On`
> - `Boot → Power On after Power Failure → Enabled`

**Step 3 — Save and exit**

Press `F10` to save and exit. The PC will reboot.

**Test it**

Shut the PC down fully (`Start → Shut Down`), then unplug the power cable for 10 seconds and plug it back in. It should boot automatically with no button press.

---

## 13. Shutdown Pi Safely from the PC Desktop

Always shut the Pi down cleanly before cutting power. Pulling the plug while the Pi is running corrupts the SD card over time. This section creates a desktop shortcut on the PC that SSHes into the Pi and shuts it down gracefully with one click.

### Step 1 — Generate an SSH Key on the PC

Open PowerShell and run. Press **Enter twice** when asked for a passphrase (leave it blank):

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\rachel_pi"
```

### Step 2 — Copy the Key to the Pi

> ⚠️ Replace `rachel` and `10.0.0.2` with your Pi username and IP if different.

```powershell
type "$env:USERPROFILE\.ssh\rachel_pi.pub" | ssh rachel@10.0.0.2 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Test that it works — this must connect **without asking for a password**:

```powershell
ssh -i "$env:USERPROFILE\.ssh\rachel_pi" rachel@10.0.0.2 "echo connected"
```

### Step 3 — Create ShutdownRachel.ps1 on the Desktop

Open Notepad, paste the script below, and save as `ShutdownRachel.ps1` on the Desktop.

> ⚠️ Replace `rachel` in `"rachel@$PI_ETH"` with your Pi username if different.

```powershell
# Rachel Robot - Safe Shutdown
$PI_ETH  = "10.0.0.2"
$SSH_KEY = "$env:USERPROFILE\.ssh\rachel_pi"

Add-Type -AssemblyName PresentationFramework
$confirm = [System.Windows.MessageBox]::Show(
    "This will safely shut down the Rachel robot.`n`nWait 30 seconds before unplugging.",
    "Shutdown Rachel Robot",
    "YesNo",
    "Warning"
)
if ($confirm -ne "Yes") { exit }

Write-Host "Connecting to Rachel Pi..." -ForegroundColor Cyan

ssh -i $SSH_KEY `
    -o ConnectTimeout=5 `
    -o StrictHostKeyChecking=no `
    "rachel@$PI_ETH" "sudo shutdown -h now" 2>&1

if ($LASTEXITCODE -eq 0) {
    [System.Windows.MessageBox]::Show(
        "Rachel is shutting down.`n`nWait 30 seconds before unplugging.",
        "Done", "OK", "Information"
    )
} else {
    [System.Windows.MessageBox]::Show(
        "Could not reach Rachel Pi.`nIt may already be off, or check the connection.",
        "Warning", "OK", "Warning"
    )
}
```

### Step 4 — Create Shutdown Rachel.bat on the Desktop

Open Notepad, paste the script below, and save as `Shutdown Rachel.bat` on the Desktop:

```batch
@echo off
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\Desktop\ShutdownRachel.ps1"
```

### Step 5 — Test It

Double-click `Shutdown Rachel.bat`. A confirmation dialog appears. Click **Yes**. The Pi shuts down cleanly within a few seconds. Wait 30 seconds before unplugging.

> **Tip:** Right-click `Shutdown Rachel.bat` → Create Shortcut → right-click the shortcut → Properties → Change Icon to pick a red power icon so it is easy to find on the desktop.

---

## 14. Optional Unity or Game Setup

- Keep `enableEmbeddedHttpServer` disabled in Unity and point the client at `127.0.0.1:8787`
- Add games through **Game Library** — select the built `.exe`. The accompanying Unity `<GameName>_Data` folder must remain beside it
- The game working directory may be left blank — Rachel uses the executable's folder automatically

---

## 15. What to Send When Setup Fails

Do not report only "it does not work." Collect these outputs first.

### Windows PC

```powershell
git rev-parse --short HEAD
git status --short
Get-NetTCPConnection -LocalPort 8787,1883 -State Listen
Invoke-RestMethod http://127.0.0.1:8787/healthz
ping 10.0.0.2
```

Also send a screenshot of `http://127.0.0.1:8787/setup` and the final 50 lines from the `helper.bat` terminal. **Hide all API keys.**

### Raspberry Pi

```bash
ip -4 addr show
ping -c 4 10.0.0.1
systemctl status face-agent.service face-neutral.service --no-pager
journalctl -u face-agent.service -u face-neutral.service -n 100 --no-pager
```

### If the Servo Doesn't Move

```bash
journalctl -u face-agent.service -f
# Then send a command from the PC and paste what appears
```

Look for `RuntimeError: Cannot determine SOC peripheral base address` — that means you are still on `RPi.GPIO` and need [Section 11](#11-servo-fix-for-raspberry-pi-5).

### If the Face Doesn't Change Expression

```bash
# Check which PNGs are present
ls /home/rachel/RACHEL/facialExpressions/ | sort
# Replace /home/rachel with your actual home directory

# Watch for incoming MQTT messages while clicking a button on the PC
mosquitto_sub -h 10.0.0.1 -p 1883 -t 'robot/pi/#' -v
```

If no MQTT message appears, the problem is on the PC side (broker or network). If the message appears but the face doesn't change, the PNG filename doesn't match the mode name — see [Section 9](#9-face-expressions-setup).

---

## 16. Updating Later

Stop Rachel, pull changes, and restart:

```powershell
Set-Location C:\unityproject\Voice_Agent
git pull --ff-only
.\helper.bat
```

Do not overwrite or commit `scripts/local_services.user.json` — it stores settings specific to that machine.

For Pi firmware updates, copy changed files from `Firmware/` to the Pi using the `scp` commands in Section 6.2, then restart the affected services:

```bash
sudo systemctl restart face-agent.service face-neutral.service
```

---

## Quick Reference — Default Ports

| Component | Port |
|---|---|
| Desktop runtime / browser panel | `8787` |
| Python voice service | `8000` |
| Piper TTS wrapper | `5005` |
| Kokoro TTS wrapper | `5007` |
| Telemetry service | `8101` |
| MQTT broker | `1883` |

## Quick Reference — MQTT Topics

| Topic | Purpose |
|---|---|
| `robot/pi/face/cmd` | Face expression commands → `faceScript.py` |
| `robot/pi/servo/cmd` | Servo/flower commands → `servoScript.py` |
| `robot/pi/led/cmd` | LED commands → `ledScript.py` |
| `robot/intent` | Launch/exit game intents |
| `robot/dialog/query` | Dialog query path |
| `voiceagent/telemetry/#` | Exercise telemetry events |

## Quick Reference — Key URLs on the PC

| Page | URL |
|---|---|
| Main console | `http://127.0.0.1:8787` |
| Setup checks | `http://127.0.0.1:8787/setup` |
| Settings / runtime | `http://127.0.0.1:8787/runtime.html` |
| Hardware controls | `http://127.0.0.1:8787/controls.html#controller` |
| Memory tools | `http://127.0.0.1:8787/memory.html` |
| Game library | `http://127.0.0.1:8787/games` |
| SDK visualizer | `http://127.0.0.1:8787/sdk` |
