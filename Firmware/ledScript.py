import board
import neopixel
import time

LED_COUNT = 39           
LED_PIN = board.D18      
LED_BRIGHTNESS = 1.0     # 0.0 to 1.0
COLOR = (225, 255, 0)      # Change this to your desired color (R, G, B)
DURATION = 60            # Seconds to keep the color on

pixels = neopixel.NeoPixel(
    LED_PIN, LED_COUNT, brightness=LED_BRIGHTNESS, auto_write=True
)

pixels.fill(COLOR)

print(f"LEDs set to {COLOR} for {DURATION} seconds...")
time.sleep(DURATION)

pixels.fill((0, 0, 0))
print("LEDs turned off.")


#command to run: sudo ~/RACHEL/venv/bin/python ledScript.py