"""Versioned deadline execution, independent of detection and route search.

poll/complete form a fake-clock-testable core. The optional worker is the only
owner of transport I/O. Command return and predicted physical arrival are logged
separately; neither is described as visual confirmation.
"""
from dataclasses import dataclass, asdict, replace
from threading import Condition, Event, Thread
from collections import deque
import time


@dataclass(frozen=True)
class Action:
    id: str
    row: int
    kind: str
    source_x: float
    target_x: float
    y: float
    start_at: float
    latest_start: float
    duration: float
    release_at: float
    source: str = 'deterministic_timed'


@dataclass(frozen=True)
class Motion:
    action: Action
    started_at: float
    input_delay: float
    stopped_at: float | None = None

    @property
    def ends_at(self):
        end = self.started_at + self.input_delay + self.action.duration
        return min(end, self.stopped_at) if self.stopped_at is not None else end

    def fraction(self, stamp):
        if self.stopped_at is not None:
            stamp = min(stamp, self.stopped_at)
        if not self.action.duration:
            return float(stamp >= self.started_at + self.input_delay)
        return max(0., min(1., (stamp-self.started_at-self.input_delay)/self.action.duration))


def displacement(motions, start, end):
    return sum((m.action.target_x-m.action.source_x) *
               (m.fraction(end)-m.fraction(start)) for m in motions)


class TimedExecutor:
    def __init__(self, transport=None, *, input_delay=.08, clock=time.monotonic,
                 threaded=False):
        self.transport = transport
        self.input_delay = input_delay
        self.clock = clock
        self.condition = Condition()
        self.cancel = Event()
        self.closed = False
        self.enabled = False
        self.generation = 0
        self.revision = 0
        self.version = 0
        self.actions = ()
        self.valid_until = 0.
        self.active = None
        self.history = deque(maxlen=256)
        self.consumed = set()
        self.events = deque(maxlen=2048)
        self.pose = None
        self.error = None
        self.thread = None
        if threaded:
            if transport is None:
                raise ValueError('A threaded executor requires a transport')
            self.thread = Thread(target=self._run, name='deterministic-executor', daemon=True)
            self.thread.start()

    def _event(self, kind, **fields):
        self.events.append({'type': kind, 'at': self.clock(), **fields})

    def set_enabled(self, enabled):
        with self.condition:
            self.enabled = enabled
            self.generation += 1
            self.revision += 1
            self.actions = ()
            self.valid_until = 0.
            self.consumed.clear()
            # An old blocking ADB swipe can finish after pause. Its history must
            # survive so a quick resume cannot pretend the phone was stationary.
            if not enabled:
                self.cancel.set()
            elif self.active is None:
                self.cancel.clear()
            self._event('enabled', enabled=enabled, generation=self.generation)
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(generation=self.generation, revision=self.revision,
                        version=self.version, active=self.active, history=tuple(self.history),
                        consumed=frozenset(self.consumed), error=self.error,
                        valid_until=self.valid_until, has_schedule=bool(self.actions))

    def is_set(self):
        """Cancellation interface also checks expiry during a streamed movement."""
        with self.condition:
            return (self.cancel.is_set() or self.closed or not self.enabled
                    or self.clock() > self.valid_until)

    def drain_events(self):
        with self.condition:
            events = list(self.events)
            self.events.clear()
            return events

    def publish(self, actions, *, version, generation, revision, valid_until, pose):
        with self.condition:
            if (not self.enabled or self.error or generation != self.generation
                    or revision != self.revision or version < self.version):
                return False
            actions = tuple(actions)
            if len(actions) > 16 or len({a.id for a in actions}) != len(actions):
                raise ValueError('Require a bounded schedule with distinct action IDs')
            if any(a.start_at > b.start_at for a, b in zip(actions, actions[1:])):
                raise ValueError('Actions must be in deadline order')
            if any(a.release_at < a.start_at for a in actions):
                raise ValueError('Action release cannot precede its start')
            self.actions = actions
            self.version = version
            self.valid_until = valid_until
            self.pose = pose  # estimated body x, host monotonic time
            self.condition.notify_all()
            return True

    def invalidate(self, reason, *, cancel_current=False):
        with self.condition:
            self.actions = ()
            self.valid_until = 0.
            self.revision += 1
            if cancel_current:
                self.cancel.set()
            self._event('invalidated', reason=reason)
            self.condition.notify_all()

    def poll(self, now=None):
        now = self.clock() if now is None else now
        with self.condition:
            if not self.enabled or self.closed or self.error:
                return None
            if now > self.valid_until:
                if self.actions:
                    self.actions = ()
                    self.cancel.set()
                    self.revision += 1
                    self._event('expired', reason='observation lease expired')
                return None
            if self.active is not None:
                return None
            for action in self.actions:
                if action.id in self.consumed:
                    continue
                if now < action.start_at:
                    return None
                if now > action.latest_start:
                    self.actions = ()
                    self.revision += 1
                    self._event('expired', action_id=action.id, reason='missed dispatch deadline')
                    return None
                x, stamp = self.pose
                predicted_x = x + displacement(self.history, stamp, now)
                if abs(predicted_x-action.source_x) > .04:
                    self.actions = ()
                    self.revision += 1
                    self._event('expired', action_id=action.id, reason='handoff position disagrees')
                    return None
                self.consumed.add(action.id)
                self.revision += 1
                if not action.duration:
                    self._event('hold', action_id=action.id, row=action.row, source=action.source)
                    continue
                motion = Motion(action, now, self.input_delay)
                self.active = motion
                self.history.append(motion)
                self.cancel.clear()
                self._event('dispatch', action=asdict(action),
                            dispatch_error=now-action.start_at,
                            predicted_arrival=motion.ends_at, generation=self.generation)
                return motion
            return None

    def complete(self, motion, *, error=None, interrupted=False):
        with self.condition:
            if self.active != motion:
                return
            self.active = None
            if interrupted:
                # A release sent now also reaches the device after input delay.
                self.history[-1] = replace(motion, stopped_at=self.clock()+self.input_delay)
            self.revision += 1
            self._event('command_return', action_id=motion.action.id,
                        cancelled=self.cancel.is_set(), error_type=type(error).__name__ if error else None)
            if error:
                self.error = type(error).__name__
                self.actions = ()
                self.cancel.set()
            self.condition.notify_all()

    def _run(self):
        try:
            while not self.closed:
                motion = self.poll()
                if motion:
                    error = None
                    interrupted = False
                    try:
                        interrupted = self.transport.move(motion.action, self) is False
                    except Exception as exc:
                        error = exc
                        interrupted = True
                    self.complete(motion, error=error, interrupted=interrupted)
                else:
                    if self.is_set():
                        self.transport.release()
                    with self.condition:
                        remaining = [a.start_at-self.clock() for a in self.actions
                                     if a.id not in self.consumed]
                        wait = max(.001, min(.01, min(remaining, default=.01)))
                        self.condition.wait(wait)
        except Exception as error:
            with self.condition:
                self.error = type(error).__name__
                self.actions = ()
                self._event('transport_error', error_type=self.error)
        finally:
            try:
                self.transport.release()
            except Exception as error:
                with self.condition:
                    self.error = type(error).__name__
                    self._event('release_error', error_type=self.error)

    def close(self):
        self.set_enabled(False)
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join()
        if self.transport:
            self.transport.close()


class AdbTransport:
    """Compatibility transport. A swipe already inside Android cannot be cancelled."""
    def __init__(self, device):
        self.device = device
        self.width, self.height = device.window_size()

    def move(self, action, cancel):
        if cancel.is_set():
            return False
        y = round(action.y*(self.height-1))
        self.device.swipe(round(action.source_x*(self.width-1)), y,
                          round(action.target_x*(self.width-1)), y, action.duration)
        return True

    def release(self):
        pass

    def close(self):
        pass
