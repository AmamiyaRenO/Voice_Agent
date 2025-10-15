import RPi.GPIO as GPIO
import time

# GPIO settings
SERVO_PIN = 11 
GPIO.setmode(GPIO.BOARD)
GPIO.setup(SERVO_PIN, GPIO.OUT)

# Set up PWM
pwm = GPIO.PWM(SERVO_PIN, 50)
pwm.start(0)

def set_angle(angle):
    duty = 2 + (angle / 18)
    GPIO.output(SERVO_PIN, True)
    pwm.ChangeDutyCycle(duty)
    time.sleep(0.5)  # give servo time to move
    GPIO.output(SERVO_PIN, False)
    pwm.ChangeDutyCycle(0)


#edit any movement angles here
try:
    while True:
        print("Opening")
        set_angle(180)  # Open position
        time.sleep(2)
        print("Closing")
        set_angle(0)    # Closed position
        time.sleep(2)

except KeyboardInterrupt:
    print("Exiting")

finally:
    pwm.stop()
    GPIO.cleanup()
