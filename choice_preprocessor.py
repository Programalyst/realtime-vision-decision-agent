"""Pure geometry/motion preprocessing. Coordinates are normalized; times are seconds.

Assumptions to calibrate: constant downward speed, mouth-only collision box, linear
horizontal drags, and no invulnerability. No learned policy or phone I/O here.
"""
from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class PlannerConfig:
    horizon: float = 1.0
    track_ttl: float = 0.2
    fallback_fall_speed: float = 0.35
    drag_speed: float = 3.0  # screen widths per second
    input_delay: float = 0.08
    margin: float = 0.015
    vertical_margin: float = 0.004  # Separate bomb padding; do not inflate the shallow mouth.
    catch_center_fraction: float = 0.25  # Droplet center must enter central 25% of mouth width.
    min_drag: float = 0.08
    catch_settle: float = 0.05  # wait after predicted mouth contact
    replan_allowance: float = 0.12  # inference/control turnaround before escape

    # Fractions within YOLO's whole-can box, from the user-marked screenshot.
    mouth_left: float = 0.33
    mouth_top: float = 0.06
    mouth_right: float = 0.76
    mouth_bottom: float = 0.18

    def __post_init__(self):
        for value in vars(self).values():
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Planner settings must be positive finite numbers')
        if not (0 <= self.mouth_left < self.mouth_right <= 1 and
                0 <= self.mouth_top < self.mouth_bottom <= 1):
            raise ValueError('Mouth fractions must form a box inside the can')
        if self.catch_center_fraction > 1:
            raise ValueError('catch_center_fraction must not exceed 1')


@dataclass
class Track:
    id: int
    kind: str
    box: list
    seen: float
    speed: float
    samples: int = 1


@dataclass(frozen=True)
class Candidate:
    id: str
    target_x: float
    duration: float
    distance: float
    collision_at: float | None
    catch_at: float | None

    escape_target_x: float | None = None
    escape_at: float | None = None  # seconds from this snapshot to escape motion
    escape_duration: float = 0.0
    catch_count: int = 0
    catch_track_id: int | None = None

    @property
    def safe(self):
        return self.collision_at is None


@dataclass
class Choices:
    timestamp: float
    can: dict | None
    candidates: list[Candidate] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    mouth_box: list[float] | None = None


def mouth_box(can, config):
    """Return the mouth in the same coordinate system as the can box."""
    x1, y1, x2, y2 = can['box']
    w, h = x2-x1, y2-y1
    return [x1+config.mouth_left*w, y1+config.mouth_top*h,
            x1+config.mouth_right*w, y1+config.mouth_bottom*h]


def overlap_interval(position, velocity, radius, start, end):
    """Times in [start,end] when abs(position + velocity*t) <= radius."""
    if abs(velocity) < 1e-9:
        return (start, end) if abs(position) <= radius else None
    a, b = sorted(((-radius-position)/velocity, (radius-position)/velocity))
    a, b = max(start, a), min(end, b)
    return (a, b) if a <= b else None


class ChoicePreprocessor:
    def __init__(self, config=None):
        self.config = config or PlannerConfig()
        self.reset()

    def reset(self):
        self.tracks = []
        self.next_id = 1
        self.last_time = None

    def _track(self, objects, timestamp):
        cfg = self.config
        old = [t for t in self.tracks if timestamp - t.seen <= cfg.track_ttl]
        used = set()
        for obj in objects:
            if obj['class'] not in ('bomb', 'droplet'):
                continue
            box = obj['box']
            cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
            matches = []
            for t in old:
                if t.id in used or t.kind != obj['class']:
                    continue
                dt = timestamp-t.seen
                tx, ty = (t.box[0]+t.box[2])/2, (t.box[1]+t.box[3])/2
                error = abs(cx-tx) + abs(cy-(ty+t.speed*dt))
                if abs(cx-tx) < .04 and error < .10:
                    matches.append((error, t.id, t, ty, dt))
            if matches:
                _, _, t, ty, dt = min(matches)
                if dt > 0:
                    measured = max(0.0, min(2.0, (cy-ty)/dt))
                    t.speed = measured if t.samples == 1 else .6*t.speed + .4*measured
                    t.samples += 1
                t.box, t.seen = list(box), timestamp
                used.add(t.id)
            else:
                t = Track(self.next_id, obj['class'], list(box), timestamp, cfg.fallback_fall_speed)
                self.next_id += 1
                self.tracks.append(t)
                used.add(t.id)
        self.tracks = [t for t in self.tracks if timestamp-t.seen <= cfg.track_ttl]

    def update(self, state, timestamp, extra_targets=()):
        if not math.isfinite(timestamp):
            raise ValueError('Timestamp must be finite')
        if self.last_time is not None and timestamp <= self.last_time:
            self.reset()
        self.last_time = timestamp
        self._track(state['objects'], timestamp)
        cans = [o for o in state['objects'] if o['class'] in ('watering_can', 'disabled_watering_can')]
        can = max(cans, key=lambda o:o['confidence']) if cans else None
        choices = Choices(timestamp, can, tracks=list(self.tracks))
        if can is None:
            return choices
        cfg = self.config
        x = can['center'][0]
        body_half = (can['box'][2]-can['box'][0])/2
        mouth = mouth_box(can, cfg)
        choices.mouth_box = mouth
        half = (mouth[2]-mouth[0])/2
        mouth_offset = (mouth[0]+mouth[2])/2-x
        low, high = body_half+cfg.margin, 1-body_half-cfg.margin
        if low > high:
            return choices
        targets = [('hold', x), ('evade_left', low), ('evade_right', high)]
        for t in self.tracks:
            tx = (t.box[0]+t.box[2])/2
            if t.kind == 'droplet' and t.seen == timestamp:
                targets.append((f'catch_{t.id}', max(low, min(high, tx-mouth_offset))))
            elif t.kind == 'bomb':
                targets.extend([(f'left_of_{t.id}', max(low, min(high, t.box[0]-half-2*cfg.margin-mouth_offset))),
                                (f'right_of_{t.id}', max(low, min(high, t.box[2]+half+2*cfg.margin-mouth_offset)))])
        targets.extend((name, max(low, min(high, target))) for name, target in extra_targets)
        seen_targets = set()
        unique_targets = []
        for name, target in targets:
            if round(target, 4) not in seen_targets:
                unique_targets.append((name, target))
                seen_targets.add(round(target, 4))
        def duration_for(distance):
            return max(cfg.min_drag, distance/cfg.drag_speed) if distance > .003 else 0.0

        def evaluate(segments):
            collision, catches = None, {}
            for t in self.tracks:
                age = timestamp-t.seen
                box = t.box
                tx = (box[0]+box[2])/2
                top, bottom = box[1]+t.speed*age, box[3]+t.speed*age
                for start, stop, intercept, vx in segments:
                    stop = min(stop, cfg.horizon)
                    if start > stop:
                        continue
                    padding = cfg.vertical_margin if t.kind == 'bomb' else 0.0
                    yi = overlap_interval((top+bottom-mouth[1]-mouth[3])/2,
                                          t.speed, (mouth[3]-mouth[1]+bottom-top)/2+padding,
                                          start, stop)
                    # Bombs use full box overlap. Catches require center alignment,
                    # independent of sprite width (single or double droplet).
                    radius_x = (half+(box[2]-box[0])/2+cfg.margin if t.kind == 'bomb'
                                else half*cfg.catch_center_fraction)
                    xi = overlap_interval(tx-intercept-mouth_offset, -vx,
                                          radius_x, start, stop)
                    if yi and xi and max(yi[0],xi[0]) <= min(yi[1],xi[1]):
                        contact = max(yi[0],xi[0])
                        if t.kind == 'bomb':
                            collision = contact if collision is None else min(collision, contact)
                        elif t.seen == timestamp and t.speed > .01:
                            catches[t.id] = min(catches.get(t.id, math.inf), contact)
            return collision, catches

        for name, target in unique_targets:
            distance = abs(target-x)
            duration = duration_for(distance)
            delay, end = cfg.input_delay, cfg.input_delay+duration
            approach = [(0, delay, x, 0)]
            if duration:
                velocity = (target-x)/duration
                approach.append((delay, end, x-velocity*delay, velocity))
            segments = approach + [(end, cfg.horizon, target, 0)]
            collision, catches = evaluate(segments)
            catch_id = min(catches, key=catches.get) if catches else None
            catch = catches.get(catch_id)
            choices.candidates.append(Candidate(name,target,duration,distance,collision,catch,
                                                catch_count=len(catches), catch_track_id=catch_id))
            if catch is None or (collision is not None and collision <= catch):
                continue
            # Complete the first drag and allow a catch + fresh observation before escape.
            escape_at = max(end, catch+cfg.catch_settle) + cfg.replan_allowance + cfg.input_delay
            if escape_at >= cfg.horizon:
                continue
            for escape_name, escape_target in unique_targets:
                escape_distance = abs(escape_target-target)
                if escape_distance < .003:
                    continue
                escape_duration = duration_for(escape_distance)
                escape_end = escape_at+escape_duration
                if escape_end > cfg.horizon:
                    continue
                vx = (escape_target-target)/escape_duration
                sequence = approach + [(end, escape_at, target, 0),
                    (escape_at, escape_end, target-vx*escape_at, vx),
                    (escape_end, cfg.horizon, escape_target, 0)]
                sequence_hit, sequence_catches = evaluate(sequence)
                if sequence_hit is not None or catch_id not in sequence_catches:
                    continue
                choices.candidates.append(Candidate(
                    f'{name}_then_{escape_name}', target, duration, distance,
                    None, sequence_catches[catch_id], escape_target, escape_at,
                    escape_duration, len(sequence_catches), catch_id))
        return choices
