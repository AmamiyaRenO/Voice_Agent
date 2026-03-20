#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import time

import RPi.GPIO as GPIO


SERVO_PIN = 11
OPEN_ANGLE = 0.0
CLOSE_ANGLE = 180.0
CENTER_ANGLE = 90.0
PWM_FREQUENCY = 50
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


GPIO.setmode(GPIO.BOARD)
GPIO.setup(SERVO_PIN, GPIO.OUT)
pwm = GPIO.PWM(SERVO_PIN, PWM_FREQUENCY)
pwm.start(0)


def _duty_from_angle(angle: float) -> float:
    return 2 + (angle / 18.0)


def _apply_angle(angle: float) -> None:
    GPIO.output(SERVO_PIN, True)
    pwm.ChangeDutyCycle(_duty_from_angle(angle))


def _release() -> None:
    GPIO.output(SERVO_PIN, False)
    pwm.ChangeDutyCycle(0)


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
    global pwm
    try:
        _release()
    except Exception:
        pass
    if pwm is not None:
        try:
            pwm.stop()
        except Exception:
            pass
        pwm = None
    try:
        GPIO.cleanup()
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
