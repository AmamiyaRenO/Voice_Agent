#!/usr/bin/env python3
"""
Raspberry Pi unified agent for face/servo/led:
 - 订阅以下主题（默认，可通过参数覆盖）
   * robot/pi/face/cmd  → 运行 faceScript.py <value>
   * robot/pi/servo/cmd → 运行 servoScript.py <value>
   * robot/pi/led/cmd   → 运行 ledScript.py <value>（root 可能必要）

Payload（JSON）：{"action":"face|servo|led","value":"..."}
允许三条主题分别携带各自 payload；也允许统一发布到 face_cmd，action 区分。

使用：
  python3 -m venv venv && source venv/bin/activate && pip install paho-mqtt pyyaml
  python3 pi/face_agent.py --broker 10.0.0.1 --venv ~/RACHEL/venv --base ~/RACHEL
"""
import argparse
import json
import os
import subprocess
import sys

import paho.mqtt.client as mqtt
from typing import Dict


def run_face_script(venv_dir: str, script_path: str, value: str) -> int:
    # 优先用同一 venv 的 python
    py = os.path.join(venv_dir, 'bin', 'python')
    if not os.path.exists(py):
        py = sys.executable
    try:
        proc = subprocess.Popen([py, script_path, value])
        return proc.pid
    except Exception as exc:
        print('failed to run face script:', exc)
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--broker', default='10.0.0.1', help='MQTT broker host (PC)')
    ap.add_argument('--port', type=int, default=int(os.getenv('BROKER_PORT', '1883') or '1883'))
    ap.add_argument('--topic_face', default='robot/pi/face/cmd')
    ap.add_argument('--topic_servo', default='robot/pi/servo/cmd')
    ap.add_argument('--topic_led', default='robot/pi/led/cmd')
    ap.add_argument('--venv', default=os.path.expanduser('~/RACHEL/venv'))
    ap.add_argument('--base', default=os.path.expanduser('~/RACHEL'), help='base dir containing scripts')
    ap.add_argument('--script_face', default=None)
    ap.add_argument('--script_servo', default=None)
    ap.add_argument('--script_led', default=None)
    args = ap.parse_args()

    # default script paths
    face_py = args.script_face or os.path.join(args.base, 'faceScript.py')
    servo_py = args.script_servo or os.path.join(args.base, 'servoScript.py')
    led_py = args.script_led or os.path.join(args.base, 'ledScript.py')

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='pi-unified-agent')

    running: Dict[str, subprocess.Popen] = {}

    def stop_category(cat: str, script_path: str | None = None) -> None:
        proc = running.get(cat)
        if not proc:
            print(cat, 'no running process')
        else:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            running.pop(cat, None)
            print(cat, 'stopped')
        # 兜底：若之前有遗留进程（例如早期版本双启动），按脚本名进行一次 pkill
        if script_path:
            try:
                subprocess.run(["pkill", "-f", script_path], check=False)
            except Exception:
                pass

    def on_connect(c, u, f, rc, p=None):
        print('connected, subscribing to', args.topic_face, args.topic_servo, args.topic_led)
        c.subscribe(args.topic_face)
        c.subscribe(args.topic_servo)
        c.subscribe(args.topic_led)

    def on_message(c, u, msg):
        if getattr(msg, 'retain', False):
            return
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except Exception:
            print('invalid payload')
            return

        def _float_text(raw, default):
            try:
                return f"{float(raw):g}"
            except Exception:
                return f"{default:g}"

        topic = str(msg.topic or '')
        action = str(payload.get('action') or '').strip().lower()
        value = payload.get('value', '')

        if not value and topic == args.topic_face:
            mode = str(payload.get('mode') or '').strip().lower()
            secs = payload.get('seconds')
            if mode:
                value = mode
                try:
                    secs_num = float(secs)
                    if secs_num > 0:
                        value = f"{mode}:{secs_num:g}"
                except Exception:
                    pass
            action = action or 'face'
        elif not value and topic == args.topic_servo:
            action = action or 'servo'
            value = str(payload.get('value') or payload.get('mode') or payload.get('action') or '').strip().lower()
        elif not value and topic == args.topic_led:
            action = action or 'led'
            mode = str(payload.get('mode') or '').strip().lower()
            color = str(payload.get('color') or '#00bfff').strip() or '#00bfff'
            if mode == 'off':
                value = 'off'
            elif mode == 'breathe':
                value = ':'.join([
                    'breathe',
                    color,
                    _float_text(payload.get('duration'), 60.0),
                    _float_text(payload.get('brightness'), 1.0),
                    _float_text(payload.get('period'), 1.5),
                ])
            else:
                led_mode = 'on' if mode in ('', 'solid', 'random') else mode
                value = ':'.join([
                    led_mode,
                    color,
                    _float_text(payload.get('duration'), 60.0),
                    _float_text(payload.get('brightness'), 1.0),
                ])

        if topic == args.topic_face or action == 'face':
            return
        elif topic == args.topic_servo or action == 'servo':
            raw_value = str(value or '').strip().lower()
            mode = raw_value
            duration_arg = ""
            speed_arg = ""
            if ":" in raw_value:
                mode, duration_arg = raw_value.split(":", 1)
                mode = mode.strip().lower()
                duration_arg = duration_arg.strip()
            elif payload.get('duration') is not None:
                duration_arg = _float_text(payload.get('duration'), 0.0)
            if payload.get('speed') is not None:
                speed_arg = _float_text(payload.get('speed'), 100.0)
            if mode in ('stop', 'idle', 'off', 'none'):
                stop_category('servo')
                py = os.path.join(args.venv, 'bin', 'python')
                if not os.path.exists(py):
                    py = sys.executable
                try:
                    subprocess.Popen([py, servo_py, 'stop'])
                except Exception:
                    pass
                return
            py = os.path.join(args.venv, 'bin', 'python')
            if not os.path.exists(py):
                py = sys.executable
            try:
                stop_category('servo')
                cmd = [py, servo_py, mode]
                if duration_arg or speed_arg:
                    cmd.append(duration_arg)
                if speed_arg:
                    cmd.append(speed_arg)
                proc = subprocess.Popen(cmd)
                running['servo'] = proc
                print('servo script started', ' '.join(cmd[2:]), 'pid=', proc.pid)
            except Exception as exc:
                print('failed to run servo script:', exc)
        elif topic == args.topic_led or action == 'led':
            v = str(value or '').lower()
            if v in ('stop', 'idle', 'off', 'none'):
                stop_category('led')
                return
            py = os.path.join(args.venv, 'bin', 'python')
            if not os.path.exists(py):
                py = sys.executable
            try:
                stop_category('led')
                proc = subprocess.Popen([py, led_py, v])
                running['led'] = proc
                print('led script started', v, 'pid=', proc.pid)
            except Exception as exc:
                print('failed to run led script:', exc)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=10)
    client.loop_forever()


if __name__ == '__main__':
    raise SystemExit(main())

