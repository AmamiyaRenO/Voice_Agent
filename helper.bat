@echo off
cd /d D:\unityproject\Robot_opr
start "" "C:\Users\tianj\AppData\Local\Programs\Python\Python313\python.exe" "D:\unityproject\Voice_Agent\scripts\start_local_services.py"
start "" "C:\Program Files\mosquitto\mosquitto.exe" -c "D:\unityproject\Robot_opr\config\mosquitto.conf" -v
