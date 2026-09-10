"""Keep one fresh watch frame while the visual model is thinking."""

from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import time

from PIL import Image, ImageChops, ImageDraw

from core.computer_platform import ComputerTransientError

WATCH_SAMPLE_SECONDS = 0.15
WATCH_SETTLE_SECONDS = 0.20


def sample(frame):
    with Image.open(io.BytesIO(base64.b64decode(frame["data"]))) as image:
        return image.convert("RGB").resize((320, 180))


def changed(previous, current, old_image, new_image):
    if previous["rect"] != current["rect"]:
        return True
    old_context, new_context = previous["context"], current["context"]
    if old_context.get("id") == new_context.get("id") and old_context.get(
        "title"
    ) != new_context.get("title"):
        return True  # A browser tab/title transition can change very few pixels.
    difference = ImageChops.difference(old_image, new_image).convert("L")
    try:
        # The popup's resizing and advice must never trigger its own next turn.
        draw = ImageDraw.Draw(difference)
        rect = current["rect"]
        for frame in (previous, current):
            mask = frame.get("indicator_rect")
            if mask:
                sx, sy = difference.width / rect["width"], difference.height / rect["height"]
                draw.rectangle(
                    (
                        (mask["x"] - rect["x"]) * sx - 2,
                        (mask["y"] - rect["y"]) * sy - 2,
                        (mask["x"] + mask["width"] - rect["x"]) * sx + 2,
                        (mask["y"] + mask["height"] - rect["y"]) * sy + 2,
                    ),
                    fill=0,
                )
        histogram = difference.histogram()
        # Ignore JPEG noise, a blinking caret and tiny clock/spinner changes.
        return sum(histogram[20:]) >= difference.width * difference.height * 0.0015
    finally:
        difference.close()


class WatchFrames:
    def __init__(self, controller, session):
        self.controller, self.session = controller, session
        self.latest = None
        self.revision = 0
        self.changed_at = 0.0
        self.error = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        previous = old_image = None
        transient_since = None
        try:
            while not self.session.cancelled.is_set():
                started = time.monotonic()
                try:
                    frame = await asyncio.to_thread(self.controller.observe, self.session.id)
                except (ComputerTransientError, subprocess.TimeoutExpired):
                    # A busy X server can overrun a probe timeout; that is not a dead session.
                    transient_since = transient_since or time.monotonic()
                    if time.monotonic() - transient_since > 5:
                        raise
                    await asyncio.sleep(WATCH_SAMPLE_SECONDS)
                    continue
                transient_since = None
                current = sample(frame)
                different = previous is None or changed(previous, frame, old_image, current)
                self.latest = frame
                if different:
                    if old_image is not None:
                        old_image.close()
                    previous, old_image = frame, current
                    self.revision += 1
                    self.changed_at = time.monotonic()
                    self.session.observation = ""
                    self.session.observation_state = "screen_changed"
                    self.controller.event(
                        "screen_changed",
                        session_id=self.session.id,
                        revision=self.revision,
                        captured_at=frame["captured_at"],
                    )
                else:
                    current.close()
                await asyncio.sleep(max(0.01, WATCH_SAMPLE_SECONDS - (time.monotonic() - started)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
        finally:
            if old_image is not None:
                old_image.close()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.latest = None
