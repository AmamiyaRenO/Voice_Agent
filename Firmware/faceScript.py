import os
import sys
import time
import json
import threading
import pygame
import paho.mqtt.client as mqtt


Face_dir = "facialExpressions"
neutral_name = "neutral"  # 文件名包含该子串的 PNG 为默认表情
display_time = 5.0
fade_step = 8

# MQTT 配置（可用环境变量覆盖）
BROKER_HOST = os.getenv("FACE_BROKER", "10.0.0.1")
BROKER_PORT = int(os.getenv("FACE_BROKER_PORT", "1883"))
TOPIC_FACE = os.getenv("FACE_TOPIC", "robot/pi/face/cmd")


def list_faces():
    return [f for f in os.listdir(Face_dir) if f.lower().endswith('.png')]


def pick_image(name, files):
    if not name:
        for f in files:
            if neutral_name in f.lower():
                return f
        return files[0]
    key = str(name).lower()
    for f in files:
        base = os.path.splitext(f)[0].lower()
        if key == base or key in base:
            return f
    return pick_image(None, files)


def load_centered(filename, info):
    img = pygame.image.load(os.path.join(Face_dir, filename)).convert_alpha()
    img = pygame.transform.rotate(img, 180) #needed for screen replacement
    img_rect = img.get_rect(center=(info.current_w // 2, info.current_h // 2))
    return img, img_rect


def fade_to(screen, clock, img_from, rect_from, img_to, rect_to):
    for alpha in range(0, 256, fade_step):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit(0)
        screen.fill((0, 0, 0))
        img_to.set_alpha(alpha)
        screen.blit(img_from, rect_from)
        screen.blit(img_to, rect_to)
        pygame.display.flip()
        clock.tick(60)
    img_to.set_alpha(None)


def parse_value(value_str):
    if not value_str:
        return None, None
    val = str(value_str)
    if ":" in val:
        name, secs = val.split(":", 1)
        try:
            return name.strip(), float(secs)
        except Exception:
            return name.strip(), None
    return val.strip(), None


def main():
    pygame.init()

    # --- Force screen size to 480x800 ---
    SCREEN_WIDTH = 480
    SCREEN_HEIGHT = 800

    os.environ["SDL_FBDEV"] = "/dev/fb0"
    os.environ["SDL_VIDEODRIVER"] = "fbcon"

    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.FULLSCREEN)
    pygame.display.set_caption("Robot Face")

    info = pygame.display.Info()
    if info.current_w != SCREEN_WIDTH or info.current_h != SCREEN_HEIGHT:
        print(f"[WARN] Detected {info.current_w}x{info.current_h}, resetting to 480x800...")
        screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.FULLSCREEN)

    files = list_faces()
    if not files:
        return

    clock = pygame.time.Clock()
    neutral_file = pick_image("neutral", files)
    neutral_img, neutral_rect = load_centered(neutral_file, info)

    state_lock = threading.Lock()
    active_name = {"name": None, "until": 0.0}

    def set_active(name, secs):
        with state_lock:
            if not name or name.lower() in ("idle", "neutral", "off", "stop"):
                active_name["name"] = None
                active_name["until"] = 0.0
                return
            duration = float(secs) if secs and secs > 0 else display_time
            active_name["name"] = name
            active_name["until"] = time.time() + duration

    # MQTT 回调
    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe(TOPIC_FACE)

    def on_message(client, userdata, msg):
        # 忽略保留消息，避免重启重放
        if getattr(msg, "retain", False):
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        action = str(payload.get("action") or "").lower()
        if action and action != "face":
            return
        name, secs = parse_value(payload.get("value"))
        set_active(name, secs)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="face-display")
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10); client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=10)
    client.loop_start()

    # 主渲染循环：默认 neutral，收到指令时播放相应表情 N 秒后回 neutral
    last_key = "__neutral__"
    current_img, current_rect = neutral_img, neutral_rect
    while True:
        with state_lock:
            now = time.time()
            if active_name["until"] and now >= active_name["until"]:
                active_name["name"] = None
                active_name["until"] = 0.0
            key = active_name["name"] or "__neutral__"

        if key != last_key:
            if key == "__neutral__":
                next_img, next_rect = neutral_img, neutral_rect
            else:
                target_file = pick_image(key, files)
                next_img, next_rect = load_centered(target_file, info)
            fade_to(screen, clock, current_img, current_rect, next_img, next_rect)
            current_img, current_rect = next_img, next_rect
            last_key = key

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                client.loop_stop()
                pygame.quit()
                sys.exit(0)
        screen.fill((0, 0, 0))
        screen.blit(current_img, current_rect)
        pygame.display.flip()
        clock.tick(60)


if __name__ == "__main__":
    main()