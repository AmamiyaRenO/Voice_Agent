import sys
import time
import math
from typing import Tuple
import board
import busio
from neopixel_spi import NeoPixel_SPI


LED_COUNT = 39  # 根据灯珠数量调整


def parse_args(value: str):
    # formats:
    #  - on:#RRGGBB[:duration[:brightness]]
    #  - on:r,g,b[:duration[:brightness]]
    #  - breathe:#RRGGBB[:duration[:brightness[:period]]]
    #  - off
    if not value:
        return ("on", (225, 255, 0), 60.0, 1.0, 1.5)
    parts = value.split(":")
    mode = parts[0].lower()
    if mode == "off":
        return ("off", (0, 0, 0), 0.0, 0.0, 0.0)
    color = (225, 255, 0)
    duration = 60.0
    brightness = 1.0
    period = 1.5
    if len(parts) >= 2 and parts[1]:
        c = parts[1]
        if c.startswith("#") and len(c) == 7:
            color = (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
        elif "," in c:
            try:
                r, g, b = c.split(",")
                color = (int(r), int(g), int(b))
            except Exception:
                pass
    if len(parts) >= 3 and parts[2]:
        try:
            duration = float(parts[2])
        except Exception:
            pass
    if len(parts) >= 4 and parts[3]:
        try:
            brightness = max(0.0, min(1.0, float(parts[3])))
        except Exception:
            pass
    if len(parts) >= 5 and parts[4]:
        try:
            period = max(0.2, float(parts[4]))
        except Exception:
            pass
    return (mode, color, duration, brightness, period)


def clamp_color(rgb: Tuple[int, int, int], scale: float) -> Tuple[int, int, int]:
    r = int(max(0, min(255, rgb[0] * scale)))
    g = int(max(0, min(255, rgb[1] * scale)))
    b = int(max(0, min(255, rgb[2] * scale)))
    return (r, g, b)


def _fill_spi(pixels: "NeoPixel_SPI", rgb: Tuple[int, int, int]) -> None:
    pixels.fill(rgb)


def run(value: str) -> None:
    mode, color, duration, brightness, period = parse_args(value)
    spi = busio.SPI(board.SCLK, MOSI=board.MOSI)
    pixels = NeoPixel_SPI(spi, LED_COUNT, auto_write=True)
    try:
        if mode == "off":
            _fill_spi(pixels, (0, 0, 0))
            return
        if mode == "on":
            _fill_spi(pixels, clamp_color(color, brightness))
            time.sleep(duration)
            return
        if mode == "breathe":
            t0 = time.time()
            while time.time() - t0 < duration:
                phase = (time.time() - t0) / period * 2 * math.pi
                amp = (math.sin(phase) + 1.0) / 2.0
                level = 0.1 + 0.9 * amp
                _fill_spi(pixels, clamp_color(color, brightness * level))
                time.sleep(0.03)
            return
        # fallback → 常亮
        _fill_spi(pixels, clamp_color(color, brightness))
        time.sleep(duration)
    finally:
        _fill_spi(pixels, (0, 0, 0))


if __name__ == "__main__":
    val = sys.argv[1] if len(sys.argv) > 1 else ""
    run(val)