# Fresh Rachel Station Setup

This checklist is for setting up a new Windows mini PC and a new Raspberry Pi as one Rachel station. Follow it in order. The Windows PC runs the voice services, browser console, game launcher, and MQTT broker. The Pi runs the face display, flower servo, and LEDs.

## 1. Before You Start

You need:

- A Windows 11 mini PC with internet access, a microphone, speakers, and an Ethernet port.
- A Raspberry Pi running current 64-bit Raspberry Pi OS, with SSH enabled.
- A direct Ethernet cable between the PC and Pi. Keep Wi-Fi connected for internet access.
- The Rachel hardware connected to the Pi.
- A Gemini API key if you will use Gemini conversation or Gemini Live recognition.
- A separate Google Cloud Text-to-Speech API key if you will use Google Cloud TTS. A Gemini key cannot replace this key.

Use `RACHEL` as the Pi username if possible. The checked-in service examples use `/home/RACHEL/RACHEL`. If the Pi has a different username, replace both `User=RACHEL` and every `/home/RACHEL` path in the Pi commands and systemd units.

Do not send API keys in chat and do not commit `scripts/local_services.user.json`. That file is intentionally ignored by Git.

## 2. Set Up the Windows Mini PC

### 2.1 Clone the repository

Install Git for Windows, open PowerShell, and run:

```powershell
New-Item -ItemType Directory -Force C:\unityproject | Out-Null
Set-Location C:\unityproject
git clone https://github.com/AmamiyaRenO/Voice_Agent.git
Set-Location .\Voice_Agent
git status
```

`git status` should say that the branch is up to date and the working tree is clean.

### 2.2 Start Rachel for the first time

Run:

```powershell
.\helper.bat
```

The first start can take several minutes because the launcher creates Python environments and downloads dependencies and local models. Keep the terminal open. When startup completes, open:

- Main console: `http://127.0.0.1:8787`
- Setup checks: `http://127.0.0.1:8787/setup`
- Settings: `http://127.0.0.1:8787/runtime.html`

The normal browser console address remains `http://127.0.0.1:8787`. Unity does not need to host a second web server.

### 2.3 Choose one speech-input path

Choose one option explicitly in **Settings > Advanced Configuration**:

**Option A: Gemini Live (recommended for a new cloud-connected station)**

1. Select the cloud/Gemini conversation profile.
2. Select Gemini Live as the streaming recognition mode.
3. Enter the Gemini API key in Credentials and save.
4. Select the intended microphone (for example, `Logitech C920`) instead of relying on the Windows default, then confirm the input level changes while speaking.
5. Restart `helper.bat` if the page says the managed services need a restart.

**Option B: Windows Live Captions**

This requires the separate `EnableLcMic.exe` listener; its source/build is not included in this repository. Set `LIVE_CAPTIONS_LISTENER_EXE` to the full path of that executable, enable Windows Live Captions with microphone audio (`Win + Ctrl + L`), and restart the services. See [LIVE_CAPTIONS_BRIDGE.md](LIVE_CAPTIONS_BRIDGE.md).

If the listener executable has not been supplied, use Gemini Live instead. Do not leave the default on Live Captions and expect microphone input to appear without the listener.
The Rachel microphone selector applies to direct API/Gemini capture, not to Live Captions; choose the microphone in Windows when using Live Captions. A working camera preview only verifies the video path and does not verify speech input.

### 2.4 Configure speech output

The conversation key and TTS key are separate:

- **Google Cloud TTS:** enable the Google Cloud Text-to-Speech API, enter its API key in **Settings > Credentials**, then select a compatible voice. Some Gemini-named Google voices also require a model name; current Rachel versions send it automatically.
- **Piper:** copy the Piper executable and model files onto the PC, then configure `PIPER_EXECUTABLE`, `PIPER_MODEL_PATH`, and optionally `PIPER_CONFIG_PATH` in `scripts/local_services.user.json` or through the Settings fields that expose them.

Voice identification is disabled by default. This does not disable ordinary microphone capture, speech recognition, or conversation.

### 2.5 Confirm the PC services

In a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/healthz
Get-NetTCPConnection -LocalPort 8787,1883 -State Listen
```

The health request must return successfully. Ports `8787` and `1883` must be listening before Pi integration.

## 3. Configure the Direct Ethernet Link

Use this fixed network:

| Device | Ethernet address |
|---|---|
| Windows mini PC | `10.0.0.1/24` |
| Raspberry Pi | `10.0.0.2/24` |

Keep Wi-Fi enabled and do not add a gateway to this Ethernet link.

On the PC, open PowerShell as Administrator:

```powershell
Get-NetAdapter
# Replace "Ethernet" if the adapter has a different name.
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.0.0.1 -PrefixLength 24
New-NetFirewallRule -DisplayName "Voice Agent MQTT" -Direction Inbound -Protocol TCP -LocalPort 1883 -RemoteAddress 10.0.0.2 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "Voice Agent Panel" -Direction Inbound -Protocol TCP -LocalPort 8787 -RemoteAddress 10.0.0.2 -Action Allow -Profile Any
```

On the Pi:

```bash
nmcli connection show
# Replace "Wired connection 1" with the actual connection name.
sudo nmcli connection modify "Wired connection 1" \
  ipv4.method manual ipv4.addresses 10.0.0.2/24 \
  ipv4.gateway "" ipv4.dns "" ipv4.never-default yes
sudo nmcli connection up "Wired connection 1"
ip -4 addr show
ping -c 4 10.0.0.1
```

From the PC, `ping 10.0.0.2` must also succeed.

## 4. Set Up the Raspberry Pi

### 4.1 Copy the hardware files

From the repository folder on the PC:

```powershell
ssh RACHEL@10.0.0.2 "mkdir -p /home/RACHEL/RACHEL/facialExpressions"
scp Firmware\face_agent.py Firmware\faceScript.py Firmware\servoScript.py Firmware\ledScript.py Firmware\face_agent.env RACHEL@10.0.0.2:/home/RACHEL/RACHEL/
scp "Firmware\LCD Output\facialExpressions\*.png" RACHEL@10.0.0.2:/home/RACHEL/RACHEL/facialExpressions/
```

### 4.2 Install dependencies

SSH into the Pi and run:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip mosquitto-clients
sudo raspi-config nonint do_spi 0
sudo usermod -aG gpio,spi,video,input "$USER"
cd /home/RACHEL/RACHEL
python3 -m venv venv
./venv/bin/pip install paho-mqtt pygame RPi.GPIO adafruit-blinka adafruit-circuitpython-neopixel-spi
sudo reboot
```

After reconnecting, test the subscriber directly:

```bash
cd /home/RACHEL/RACHEL
./venv/bin/python ./face_agent.py \
  --broker 10.0.0.1 --port 1883 \
  --venv /home/RACHEL/RACHEL/venv --base /home/RACHEL/RACHEL
```

Stop it with `Ctrl+C` after it connects. If it cannot connect, fix the Ethernet/firewall/MQTT step before installing services.

### 4.3 Install the two Pi services

Copy the two unit definitions from [Firmware/README.md](../Firmware/README.md#systemd-configuration) into:

- `/etc/systemd/system/face-agent.service`
- `/etc/systemd/system/face-neutral.service`

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now face-agent.service face-neutral.service
systemctl status face-agent.service face-neutral.service --no-pager
journalctl -u face-agent.service -u face-neutral.service -n 100 --no-pager
```

Both services should show `active (running)`. Only `face-neutral.service` renders the face; `face-agent.service` handles servo and LED commands.

## 5. End-to-End Test

Start `helper.bat` on the PC. On the Pi, watch incoming MQTT commands:

```bash
mosquitto_sub -h 10.0.0.1 -p 1883 -t 'robot/pi/#' -v
```

On the PC, publish a face command through Rachel:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/face `
  -ContentType "application/json" `
  -Body '{"mode":"happy","seconds":5}'
```

Success means the Pi terminal sees the command and the Rachel display changes expression. Next, test the LED and flower controls from `http://127.0.0.1:8787/controls.html#controller`, then test listening and speech from the main console.

## 6. Optional Unity or Game Setup

- For the Unity shell/package, keep `enableEmbeddedHttpServer` disabled and point the client at `127.0.0.1:8787`.
- Add games through **Game Library**. Use **Extract All** on the complete game ZIP first, then select the built `.exe` from the extracted folder. Never select or run the executable while browsing inside a ZIP. The accompanying Unity `<GameName>_Data` folder and `UnityPlayer.dll` must remain beside it.
- The game Working directory may be left blank. Rachel will use the executable's folder automatically.

## 7. What to Send Back When Setup Fails

Do not report only “it does not work.” Send these items:

**Windows PC**

```powershell
git rev-parse --short HEAD
git status --short
Get-NetTCPConnection -LocalPort 8787,1883 -State Listen
Invoke-RestMethod http://127.0.0.1:8787/healthz
ping 10.0.0.2
```

Also send a screenshot of `/setup` and the final 50 lines from the `helper.bat` terminal. Hide all API keys.

**Raspberry Pi**

```bash
ip -4 addr show
ping -c 4 10.0.0.1
systemctl status face-agent.service face-neutral.service --no-pager
journalctl -u face-agent.service -u face-neutral.service -n 100 --no-pager
```

## 8. Updating Later

Stop Rachel, then run:

```powershell
Set-Location C:\unityproject\Voice_Agent
git pull --ff-only
.\helper.bat
```

Do not overwrite or commit `scripts/local_services.user.json`; it stores settings specific to that machine.
