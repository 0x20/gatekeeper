#!/usr/bin/python3
import serial
import click
import re
import logging
import time
import threading
import io
import queue
import traceback
import paho.mqtt.client as mqtt
from datetime import datetime

import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

mqtt_client = mqtt.Client()
db_filename = None

# Configuration
GPIO_RPI_OK = 22
GPIO_GSM_OK = 23
GPIO_OPEN = 27

# Sim800 board
GPIO_GSM_PWR = 17
GPIO_GSM_RST = 18

event_queue = queue.SimpleQueue()

class Filter:
    def match(self, time, number):
        """Return whether this filter matches the incoming request"""
        return False
    def label(self):
        """Return the label for this request, or None if no label"""
        return None

class TimeFilter(Filter):
    def __init__(self, start, end):
        """Start and end are minutes from midnight on sunday. This always repeats weekly"""
        self.start = start
        self.end = end
        
    def match(self, time, number):
        now = time.weekday()
        now = now * 24 + time.hour
        now = now * 60 + time.minute
        
        if self.start > self.end:
            return now >= self.start or now <= self.end
        else:
            return now >= self.start and now <= self.end

class NumberFilter(Filter):
    def __init__(self, number, label):
        self.label_ = label
        self.number = number

    def match(self, time, number):
        return number == self.number

    def label(self):
        return self.label_


class Sim800Thread(threading.Thread):
    def __init__(self, *, name="SIM800", device="/dev/ttyAMA0"):
        super().__init__(name=name)
        self.daemon = True
        self.device_name = device
        self.raw_device = serial.Serial(
            port = self.device_name,
            baudrate = 115200,
            timeout = 1,
            inter_byte_timeout = 0.1,
        )
        self.device = self.raw_device
        # Start by trying to shut down the device, so that it can be woken up later
        self.device.write(b"AT+CPOWD=1\n")
        while self.device.readline() != b'':
            pass
        logger.info("GSM halted")
        
        
    def run(self):
        # Wait for connection

        connected = False
        while not connected:
            self.device.write(b"AT\n")
            for line in self.device:
                logger.debug("GSM: %s", repr(line))
                if line == b"OK\r\n":
                    connected = True
                    break
        logger.info("GSM active")
        # Set up the line
        # Wait until the device is done booting
        time.sleep(5)
        self.raw_device.timeout = 10
        #self.device.write(b"ATQ0V1E1+CREG=1;+CLIP=1;+CPIN=1111\n")
        self.device.write(b"ATQ0V1E1+CREG=1;+CLIP=1\n")
        logger.info("GSM initialized")
        # It's a PITA to analyze the results, so just drop into the wait loop
        while True:
            line = self.device.readline()
            logger.debug("GSM: %s", repr(line))
            if line == b"":
                # Toggle LED
                self.device.write(b"AT\n")
                continue
            if line == b"OK\r\n":
                event_queue.put(("GSM_OK", []))
                continue
            m = re.match(br"\+CREG: *(\d+)\r\n", line)
            if m:
                event_queue.put(("CREG", [int(m.group(1))]))
                continue
            m = re.match(br"\+CLIP: *([^\r\n]+)\r\n", line)
            if m:
                # Parse CLIP. Format is
                # num:str,type:int,subnum:str,subtype,pbentry:str,valid:int
                # We only really care about the first field
                num = re.match(br'"([^"]*)",.*', m.group(1))
                if num is not None:
                    event_queue.put(("RING", [num.group(1)]))
                continue
        logger.fatal("GSM ended")

class TickThread(threading.Thread):
    def __init__(self, rate=0.1):
        super().__init__(name="Tick")
        self.daemon = True
        self.rate = rate

    def run(self):
        while True:
            time.sleep(self.rate)
            event_queue.put(("HEARTBEAT", []))

class OpenerThread(threading.Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon = True
        self.semaphore = threading.Semaphore(value=0)

    def run(self):
        while True:
            self.semaphore.acquire()
            logger.info("Opening")
            GPIO.output(GPIO_OPEN, True)
            time.sleep(1)
            GPIO.output(GPIO_OPEN, False)

opener = OpenerThread(name="Opener")

def init():
    global cached_db
    cached_db = load_database()
    
    GPIO.setup([
        GPIO_RPI_OK,
        GPIO_GSM_OK,
        GPIO_OPEN,
        GPIO_GSM_PWR
    ], GPIO.OUT, initial = GPIO.LOW)

    GPIO.output(GPIO_RPI_OK, True)

    # Spawn SIM800 listener
    Sim800Thread().start()
    
    # Start up sim800
    GPIO.output(GPIO_GSM_PWR, True)
    time.sleep(1.5)
    GPIO.output(GPIO_GSM_PWR, False)

    # Start timer
    TickThread().start()
    opener.start()
    
class Heartbeat:
    PAT_HEARTBEAT = [
        (1, True),
        (1, False),
        (1, True),
        (7, False),
    ]

    PAT_SLOW = [
        (5, True),
        (5, False),
    ]

    PAT_FAST = [
        (2, True),
        (2, False),
    ]

    PAT_VSLOW = [
        (8, True),
        (2, False),
    ]

    PAT_OFF = [
        (1, False),
    ]

    PAT_ON = [
        (1, True),
    ]

    PAT_SOS = [(2, True), (2, False)] * 3 + [(6, True), (2, False)] * 3 + [(2,True), (2, False)] * 3 + [(6, False)]
    
    def __init__(self, pin, active_low=False):
        self.pin = pin
        self.active_low = active_low
        GPIO.setup(pin, GPIO.OUT)
        self.pattern = None
        self.set_mode(self.PAT_OFF)

    def set_mode(self, pattern):
        if self.pattern is pattern:
            # Don't change the pattern if it would be to the current state
            return
        self.pattern = pattern
        self.pos = -1
        self.delay = 0

    def pulse(self):
        self.delay = self.delay - 1
        if self.delay < 0:
            self.pos = (self.pos + 1) % len(self.pattern)
            self.delay, state = self.pattern[self.pos]
            GPIO.output(self.pin, state ^ self.active_low)

gsm_ok = Heartbeat(GPIO_GSM_OK)
rpi_ok = Heartbeat(GPIO_RPI_OK)

rpi_ok.set_mode(Heartbeat.PAT_HEARTBEAT)
            
def clock_now():
    return time.clock_gettime(time.CLOCK_MONOTONIC)
            
def loop():
    last_gsm_ok = clock_now()
    regstate = 0
    while True:
        event, args = event_queue.get()
        #print(event, repr(args))
        if event == "GSM_OK":
            last_gsm_ok = clock_now()
        elif event == "CREG":
            logger.info("Registration state: %d", args[0])
            regstate = args[0]
        elif event == "RING":
            handle_ring(args[0])
        elif event == "HEARTBEAT":
            # Update GSM_OK state
            if clock_now() - last_gsm_ok > 30:
                # GSM is out to lunch
                gsm_ok.set_mode(Heartbeat.PAT_OFF)
            elif regstate == 0:
                # Not registered, not searching
                gsm_ok.set_mode(Heartbeat.PAT_OFF)
            elif regstate == 1:
                # Registered, home network
                gsm_ok.set_mode(Heartbeat.PAT_SLOW)
            elif regstate == 2:
                # Not registered, searching
                gsm_ok.set_mode(Heartbeat.PAT_FAST)
            elif regstate == 3:
                # Registration denied
                gsm_ok.set_mode(Heartbeat.PAT_SOS)
            elif regstate == 5:
                # Roaming
                gsm_ok.set_mode(Heartbeat.PAT_VSLOW)
            else:
                gsm_ok.set_mode(Heartbeat.PAT_OFF)

            gsm_ok.pulse()
            rpi_ok.pulse()


def handle_ring(number):
    global cached_db
    mqtt.publish("hsg/gatekeeper/ring", 1)
    logger.info("Received call from %s", number)
    try:
        number = number.decode("ascii")
    except UnicodeDecodeError:
        return
    # Load the database

    try:
        db = load_database()
    except Exception as e:
        logger.exception("Failed to load config")
        db = cached_db
    else:
        cached_db = db

    # TODO: fill this in
    now = datetime.now()
    accept = False
    label = None
    for filt in db:
        if filt.match(now, number):
            accept = True
            if label == None:
                label = filt.label()
    if accept:
        # Open door
        mqtt.publish("hsg/gatekeeper/open", label or "anon")
        logger.info("Door opened for: %s", label)
        opener.semaphore.release()
        # TODO: Publish on MQTT


def handle_mqtt_cmd(client, userdata, msg):
    logger.info("Received MQTT command '%s' on topic '%s'", str(msg.payload), str(msg.topic))
    if msg.topic == 'hsg/gatekeeper/cmd':
        if lower(str(msg.payload)) == 'open':
            logger.info('Opening gate from MQTT command')
            opener.semaphore.release()


def handle_mqtt_connect(client, userdata, flags, rc):
    logger.info("Connected to MQTT server")
    client.subscribe("hsg/gatekeeper/cmd")
        
    
    
def load_database():
    # TODO: fill this in
    with open(db_filename, "rt") as f:
        filters = []
        for rawline in f:
            line = rawline.strip().split('#')[0].split()
            
            if len(line) == 0:
                continue
            elif line[0] == "*":
                # Date pattern
                daystart = int(line[1]) * 60 * 24
                stime = parse_time(line[2]) + daystart
                etime = parse_time(line[3]) + daystart
                filters.append(TimeFilter(stime, etime))
            elif line[0].startswith("+"):
                num = line[0][1:]
                if len(line) > 1:
                    label = " ".join(line[1:])
                else:
                    label = None
                filters.append(NumberFilter(num, label))
            else:
                logger.warning("DB: Don't know what to do with line %r", rawline)
            
        return filters
    
def configure_log(use_journald, verbosity):
    global logger
    levels = [
        logging.WARN,
        logging.INFO,
        logging.DEBUG
    ]

    if verbosity >= len(levels):
        verbosity = -1

    handlers = []
    if use_journald:
        import systemd.journal
        handlers.append(systemd.journal.JournalHandler())
    else:
        handlers.append(logging.StreamHandler())

    # TODO: File handler
        
    logging.basicConfig(level = levels[verbosity], handlers=handlers)
    logger = logging.getLogger("main")
    

@click.command()
@click.option("--journald/--no-journald", default=False)
@click.option("-v", '--verbose', count=True)
@click.option("-d", "--database", required=True)
@click.option("-m", "--mqtt")
def main(journald, verbose, database, mqtt):
    global db_filename
    db_filename = database
    configure_log(journald, verbose)
    if mqtt:
        mqtt_client.connect(mqtt)
        mqtt_client.on_connect = handle_mqtt_connect
        mqtt_client.on_message = handle_mqtt_cmd
        mqtt_client.loop_start()
    
    init()
    loop()
    
if __name__ == "__main__":
    main()

