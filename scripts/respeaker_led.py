"""
ReSpeaker USB Mic Array - LED Pipeline State Controller
Maps LED states to HRI pipeline stages:
  IDLE    -> soft white breathing glow (neutral face)
  LISTEN  -> blue DOA-directed brightness (attentive face)
  THINK   -> blue clockwise rotation (thinking face)
  SPEAK   -> green pulsing (emotion from LLM JSON)
  ERROR   -> solid red (something went wrong)

Usage:
  from respeaker_led import LEDController, PipelineState

  led = LEDController()
  led.set_state(PipelineState.IDLE)

  # During LISTEN, feed real DOA angle:
  led.set_listen(doa_angle=90)
"""

import usb.core
import usb.util
import time
import threading
import math
import sys
from enum import Enum


# ReSpeaker USB Mic Array vendor/product IDs
VENDOR_ID  = 0x2886
PRODUCT_ID = 0x0018

# Number of LEDs on the ring
NUM_LEDS = 12

# LED command codes
CMD_MONO       = 1
CMD_BRIGHTNESS = 0x20
CMD_PIXEL_RING = 6    # custom per-LED RGB control


class PipelineState(Enum):
    IDLE   = "idle"
    LISTEN = "listen"
    THINK  = "think"
    SPEAK  = "speak"
    ERROR  = "error"


class LEDController:
    states = PipelineState

    def __init__(self, brightness: int = 20):
        """
        Initialise LED controller.
        brightness: 0 to 31 (default 20)
        """
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)

        if self.dev is None:
            raise RuntimeError(
                "ReSpeaker USB Mic Array not found. "
                "Check USB connection and udev rules."
            )

        self.brightness = brightness
        self._send(CMD_BRIGHTNESS, [brightness])
        self.current_state = None

        # Background animation thread control
        self._anim_stop   = threading.Event()
        self._anim_thread = None

        print("[LEDController] Device found and ready.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Low-level USB send
    # ------------------------------------------------------------------

    def _send(self, command: int, data: list):
        """Send a USB control transfer to the LED ring."""
        self.dev.ctrl_transfer(
            usb.util.CTRL_OUT |
            usb.util.CTRL_TYPE_VENDOR |
            usb.util.CTRL_RECIPIENT_DEVICE,
            0,
            command,
            0x1C,
            data,
            timeout=1000
        )

    def _set_pixels(self, pixels: list):
        """
        Set all LEDs individually.
        pixels: list of 12 tuples [(R, G, B), ...]
        """
        data = []
        for r, g, b in pixels:
            data += [r, g, b, 0]
        self._send(CMD_PIXEL_RING, data)

    # ------------------------------------------------------------------
    # Animation thread helpers
    # ------------------------------------------------------------------

    def _stop_animation(self):
        """Stop any running background animation thread."""
        if self._anim_thread and self._anim_thread.is_alive():
            self._anim_stop.set()
            self._anim_thread.join()
        self._anim_stop.clear()

    def _start_animation(self, target):
        """Start a background animation thread."""
        self._stop_animation()
        self._anim_thread = threading.Thread(target=target, daemon=True)
        self._anim_thread.start()

    # ------------------------------------------------------------------
    # State animations
    # ------------------------------------------------------------------

    def _animate_idle(self):
        """Soft white breathing glow — robot is alive but resting."""
        while not self._anim_stop.is_set():
            for i in range(0, 628, 4):
                if self._anim_stop.is_set():
                    break
                # Sine wave between 5 and 60 brightness (subtle, not blinding)
                val = int((math.sin(i / 100.0) + 1) / 2 * 55) + 5
                pixels = [(val, val, val)] * NUM_LEDS
                self._set_pixels(pixels)
                time.sleep(0.02)

    def _animate_think(self):
        """Blue clockwise rotating dot — like a loading spinner."""
        step = 0
        while not self._anim_stop.is_set():
            pixels = [(0, 0, 0)] * NUM_LEDS

            for offset, factor in [(0, 1.0), (1, 0.5), (2, 0.2)]:
                idx = (step + offset) % NUM_LEDS
                val = int(180 * factor)
                pixels[idx] = (0, 0, val)  # blue

            self._set_pixels(pixels)
            step = (step + 1) % NUM_LEDS
            time.sleep(0.08)

    def _animate_speak(self):
        """Green pulsing — smooth fade in and out while speaking."""
        while not self._anim_stop.is_set():
            for i in range(0, 628, 5):
                if self._anim_stop.is_set():
                    break
                val = int((math.sin(i / 100.0) + 1) / 2 * 180)
                pixels = [(0, val, 0)] * NUM_LEDS
                self._set_pixels(pixels)
                time.sleep(0.02)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_state(self, state: PipelineState):
        """Set LED ring to match the current pipeline stage."""
        if state == self.current_state:
            return

        self._stop_animation()
        self.current_state = state

        if state == PipelineState.IDLE:
            # Soft white breathing in background thread
            self._start_animation(self._animate_idle)

        elif state == PipelineState.LISTEN:
            # Default blue ring — call set_listen() with DOA to update
            pixels = [(0, 0, 60)] * NUM_LEDS
            self._set_pixels(pixels)

        elif state == PipelineState.THINK:
            # Blue clockwise rotation in background thread
            self._start_animation(self._animate_think)

        elif state == PipelineState.SPEAK:
            # Green pulsing in background thread
            self._start_animation(self._animate_speak)

        elif state == PipelineState.ERROR:
            # Solid red — something went wrong
            pixels = [(180, 0, 0)] * NUM_LEDS
            self._set_pixels(pixels)

    def set_listen(self, doa_angle: float):
        """
        Update LISTEN state LEDs based on DOA angle.
        The LED closest to the voice source shines brightest (blue),
        neighbours fade out on either side.

        doa_angle: 0-359 degrees from ReSpeaker tuning API
        """
        if self.current_state != PipelineState.LISTEN:
            return

        center_led = int((doa_angle / 360.0) * NUM_LEDS) % NUM_LEDS

        pixels = []
        for i in range(NUM_LEDS):
            dist = min(
                abs(i - center_led),
                NUM_LEDS - abs(i - center_led)
            )

            if dist == 0:
                brightness = 255
            elif dist == 1:
                brightness = 80
            elif dist == 2:
                brightness = 20
            else:
                brightness = 0

            pixels.append((0, 0, brightness))  # blue

        self._set_pixels(pixels)

    def off(self):
        """Turn all LEDs off."""
        self._stop_animation()
        pixels = [(0, 0, 0)] * NUM_LEDS
        self._set_pixels(pixels)
        self.current_state = None

    def __del__(self):
        try:
            self.off()
        except Exception:
            pass
