import pygame
import os
import time

Face_dir = "facialExpressions"
display_time = 5
fade_time = 1

pygame.init()
info = pygame.display.Info()
screen = pygame.display.set_mode((info.current_w, info.current_h), pygame.FULLSCREEN)
pygame.display.set_caption("Robot Face")

#grabs all .png files in the facialExpressions directory
face_files = [f for f in os.listdir(Face_dir) if f.lower().endswith('.png')]

# Loads and centers the current image
def load_and_center(filename):
    img = pygame.image.load(os.path.join(Face_dir, filename)).convert_alpha()
    img_rect = img.get_rect(center=(info.current_w // 2, info.current_h // 2))
    return img, img_rect

faces = [load_and_center(f) for f in face_files]

clock = pygame.time.Clock()
while True:
    for i in range(len(faces)):
        img1, rect1 = faces[i]
        img2, rect2 = faces[(i + 1) % len(faces)]

        # Show current face
        start_time = time.time()
        while time.time() - start_time < display_time:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    exit()
            screen.fill((0, 0, 0))
            screen.blit(img1, rect1)
            pygame.display.flip()
            clock.tick(60)

        # Fade transition to next face
        for alpha in range(0, 256, 8):
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    exit()
            screen.fill((0, 0, 0))
            img2.set_alpha(alpha)
            screen.blit(img1, rect1)
            screen.blit(img2, rect2)
            pygame.display.flip()
            clock.tick(60)
        img2.set_alpha(None)