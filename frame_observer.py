"""A latest-frame mailbox fed by the decoder, never by polling its last image."""
from dataclasses import dataclass
from threading import Condition
import time


@dataclass(frozen=True)
class DecodedFrame:
    sequence: int
    decoded_at: float
    frame: object


class FrameObserver:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.condition = Condition()
        self.latest = None
        self.delivered = -1
        self.closed = False

    def publish(self, sequence, frame):
        # The adapter gives each decode a distinct frame object. Hold a reference;
        # ndarray conversion and annotation must happen outside its decode thread.
        stamp = self.clock()
        with self.condition:
            if self.closed or (self.latest and sequence <= self.latest.sequence):
                return
            self.latest = DecodedFrame(sequence, stamp, frame)
            self.condition.notify()

    def get(self, timeout=.1):
        with self.condition:
            self.condition.wait_for(lambda: self.closed or
                                    (self.latest and self.latest.sequence > self.delivered), timeout)
            if self.closed or not self.latest or self.latest.sequence <= self.delivered:
                return None
            self.delivered = self.latest.sequence
            return self.latest

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
