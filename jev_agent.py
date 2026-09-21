"""Nonblocking Jev decisions from YOLO detections; no device control."""
from concurrent.futures import ThreadPoolExecutor
import math
import os
import time

from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient


def detection_state(result):
    """Use normalized coordinates so capture resolution does not change the state."""
    height, width = result.orig_shape
    objects = []
    for box in result.boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        objects.append({
            "class": result.names[int(box.cls.item())],
            "confidence": round(float(box.conf.item()), 4),
            "box": [round(x1 / width, 4), round(y1 / height, 4),
                    round(x2 / width, 4), round(y2 / height, 4)],
            "center": [round((x1 + x2) / (2 * width), 4),
                       round((y1 + y2) / (2 * height), 4)],
        })
    return {"coordinates": "Normalized 0..1; x increases right, y increases downward.",
            "objects": objects}


class JevAgent:
    def __init__(self, interval=0.5, max_age=1.5, client=None):
        if not all(math.isfinite(v) and v > 0 for v in (interval, max_age)):
            raise ValueError("Jev interval and max age must be positive finite seconds")
        self.client = client or TypeSafeClient(
            api_key=os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY"),
            model="jev-latest", timeout=3.0, retry=RetryPolicy(max_retries=0)
        )
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.interval, self.max_age = interval, max_age
        self.future = None
        self.next_request = 0.0
        self.sent_at = 0.0
        self.decision = None
        self.enabled = True
        self.enabled_at = 0.0
        self.status = "Jev: waiting for can"
        self.question = Choice(
            instructions=(
                "Choose the watering can's immediate horizontal movement in a falling-object game. "
                "Collect droplets (single and double sprites are the same class) and avoid bombs. "
                "Objects fall downward. Prioritize nearby objects above the can, especially bombs "
                "near its path. Ignore objects already below the can. Compare bounding boxes as "
                "well as centers. Choose hold when aligned with a safe droplet, no can is visible, "
                "or movement is not justified. A disabled can is flashing after a bomb hit; do not "
                "assume it is invulnerable. This is one snapshot: velocity and future spawns are unknown."
            ),
            criteria={"left": "Move the can left.", "right": "Move the can right.",
                      "hold": "Keep the can at its current horizontal position."},
        )

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.enabled_at = time.monotonic()
        self.decision = None
        self.status = "Jev: waiting for can" if enabled else "Jev: PAUSED - press J to start"

    def update(self, state, captured_at):
        if not self.enabled:
            return self.status
        now = time.monotonic()
        if self.future is not None and self.future.done():
            try:
                answer = self.future.result().choices["movement"]
                if answer.choice not in ("left", "right", "hold"):
                    raise ValueError("Unexpected movement choice")
                latency = now - self.sent_at
                if self.observed_at < self.enabled_at:
                    self.decision = None
                    self.status = "Jev: pre-pause response discarded"
                elif now - self.observed_at <= self.max_age:
                    self.decision = (answer.choice, float(answer.confidence), self.observed_at)
                    self.status = f"Jev: {answer.choice} {answer.confidence:.2f} | {latency * 1000:.0f}ms"
                else:
                    self.decision = None
                    self.status = "Jev: stale response discarded"
            except Exception as error:
                # Do not print exception bodies: they may contain request/auth details.
                self.decision = None
                self.status = f"Jev: {type(error).__name__}; retry in 5s"
                self.next_request = now + 5.0
            self.future = None
        if self.decision and now - self.decision[2] > self.max_age:
            self.decision = None
            self.status = "Jev: decision expired"
        has_can = any(o["class"] in ("watering_can", "disabled_watering_can")
                      for o in state["objects"])
        if not has_can:
            self.decision = None
            self.status = "Jev: waiting for can"
        if (has_can and self.future is None and now >= self.next_request
                and captured_at >= self.enabled_at and now - captured_at <= self.max_age):
            self.sent_at, self.observed_at = now, captured_at
            self.future = self.executor.submit(
                self.client.system_one, state=state, questions={"movement": self.question}
            )
            self.next_request = now + self.interval
        return self.status

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.client.close()


class DragController:
    """Apply each fresh recommendation once, using a short ADB drag."""

    def __init__(self, device, step=0.08, duration=0.12):
        self.device = device
        self.width, self.height = device.window_size()
        self.step, self.duration = step, duration
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.last_observation = None
        self.motion_status = None
        self.previous_motion = None
        self.completed_at = None
        self.ready_cache = None

    def ready_for_target(self, state, captured_at):
        """Gate local planning on command completion and a subsequent observation."""
        if self.ready_cache is not None and self.ready_cache[0] == captured_at:
            return self.ready_cache[1]
        if self.future is not None:
            if not self.future.done():
                return False
            self.completed_at = self.future.result()
            self.future = None
            if self.motion_status:
                self.motion_status['completed_at'] = self.completed_at
        if self.completed_at is None:
            return True
        if captured_at <= self.completed_at:
            return False
        cans = [o for o in state['objects'] if o['class'] in ('watering_can','disabled_watering_can')]
        if not cans:
            return False
        x = max(cans, key=lambda o:o['confidence'])['center'][0]
        self.ready_cache = (captured_at, True)
        if self.motion_status:
            self.motion_status.update(observed_x=x, remaining_error=self.motion_status['target_x']-x,
                                      observation_ready=True)
        return True

    def _target_swipe(self, *args):
        self.device.swipe(*args)
        return time.monotonic()

    def update(self, decision, state, captured_at, max_age=1.5):
        if self.future is not None:
            if not self.future.done():
                return
            self.future.result()  # Stop on control errors rather than silently continuing.
            self.future = None
        now = time.monotonic()
        if (not decision or now - captured_at > max_age or now - decision[2] > max_age
                or decision[2] == self.last_observation):
            return
        self.last_observation = decision[2]
        direction = decision[0]
        cans = [o for o in state["objects"]
                if o["class"] in ("watering_can", "disabled_watering_can")]
        if not cans or direction == "hold":
            return
        if direction not in ("left", "right"):
            raise ValueError("Unsupported movement")
        can = max(cans, key=lambda o: o["confidence"])
        x, y = can["center"]
        half_width = (can["box"][2] - can["box"][0]) / 2
        target = max(half_width, min(1 - half_width,
                     x + (self.step if direction == "right" else -self.step)))
        sx = round(x * (self.width - 1))
        sy = round(y * (self.height - 1))
        ex = round(target * (self.width - 1))
        self.future = self.executor.submit(self.device.swipe, sx, sy, ex, sy, self.duration)

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def update_target(self, candidate, state, captured_at):
        """Execute a preprocessed destination, with no queued or stale movements."""
        if not self.ready_for_target(state, captured_at):
            return
        if self.future is not None:
            if not self.future.done():
                return
            self.completed_at = self.future.result()
            self.future = None
            if self.motion_status:
                self.motion_status['completed_at'] = self.completed_at
        # Only a frame obtained after completion can tell us where the can arrived.
        if self.completed_at is not None and captured_at <= self.completed_at:
            return
        if candidate is None or time.monotonic()-captured_at > .25:
            return
        cans = [o for o in state['objects'] if o['class'] in ('watering_can', 'disabled_watering_can')]
        if not cans:
            return
        can = max(cans, key=lambda o:o['confidence'])
        x, y = can['center']
        if self.motion_status:
            self.motion_status['observed_x'] = x
            self.motion_status['remaining_error'] = self.motion_status['target_x']-x
        if candidate.duration == 0 or captured_at == self.last_observation:
            return
        target = max(0, min(1, candidate.target_x))
        if abs(target-x) < .003:
            return
        self.last_observation = captured_at
        self.previous_motion = self.motion_status
        self.ready_cache = None
        self.motion_status = {'started_at': time.monotonic(), 'source_x': x,
                              'target_x': target, 'duration': candidate.duration,
                              'candidate': candidate.id}
        self.future = self.executor.submit(
            self._target_swipe, round(x*(self.width-1)), round(y*(self.height-1)),
            round(target*(self.width-1)), round(y*(self.height-1)), candidate.duration)

    def pause(self):
        # A swipe already executing on Android must finish; cancel queued work.
        if self.future is not None and self.future.cancel():
            self.future = None
