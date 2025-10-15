import pygame
pygame.init()
screen = pygame.display.set_mode((400, 240))
screen.fill((0, 0, 0))  # Black background

# Draw eyes (cyan/blue, filled circles)
eye_color = (100, 200, 255)
background_color = (0, 0, 0)
pygame.draw.circle(screen, eye_color, (120, 100), 28)  # Left eye
pygame.draw.circle(screen, eye_color, (280, 100), 28)  # Right eye
pygame.draw.circle(screen, background_color, (120, 130), 20)  # Left cutout
pygame.draw.circle(screen, background_color, (280, 130), 20)  # Right cutout

# Draw a smile (arc)
smile_rect = pygame.Rect(150, 140, 80, 50)
pygame.draw.arc(screen, eye_color, smile_rect, 3.5, 5.9, 3)

pygame.display.flip()

while True:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            pygame.quit()
            exit()
