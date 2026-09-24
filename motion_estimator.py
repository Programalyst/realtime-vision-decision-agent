"""Estimate present state from unique delayed images and issued command history.

decode_delay is an explicit calibration assumption, NOT measured capture age.
All command/state times use host monotonic seconds. Raw observations remain in
the diagnostic snapshot; projected track timestamps never claim new detections.
"""
from collections import defaultdict, deque
from dataclasses import replace
from statistics import median
import math

from choice_preprocessor import ChoicePreprocessor, Choices, mouth_box
from timed_executor import displacement


class MotionEstimator:
    def __init__(self, config, decode_delay=.12, timing_uncertainty=.02):
        if not all(math.isfinite(x) and x >= 0 for x in (decode_delay, timing_uncertainty)):
            raise ValueError('Delay and uncertainty must be finite nonnegative seconds')
        self.config = config
        self.decode_delay = decode_delay
        self.timing_uncertainty = timing_uncertainty
        self.reset()

    def reset(self):
        self.preprocessor = ChoicePreprocessor(self.config)
        self.sequence = -1
        self.decoded_at = -math.inf
        self.samples = defaultdict(lambda: deque(maxlen=16))
        self.cached = {}
        self.last_scroll = None
        self.scroll_started = None
        self.raw = None
        self.diagnostics = {}
        self.speed = self.config.fallback_fall_speed

    def update(self, state, sequence, decoded_at, now, motions=()):
        if (sequence <= self.sequence or decoded_at <= self.decoded_at
                or decoded_at > now or now-decoded_at > .20):
            return None
        self.sequence, self.decoded_at = sequence, decoded_at
        scene_time = decoded_at-self.decode_delay
        self.raw = self.preprocessor.update(state, scene_time, row_mode=True)
        slopes = []
        moved = 0
        measured = 0
        for track in self.raw.tracks:
            if track.seen != scene_time:
                continue
            samples = self.samples[track.id]
            y = (track.box[1]+track.box[3])/2
            if samples:
                measured += 1
                moved += y-samples[-1][1] > .001
            samples.append((scene_time, y))
            while samples and scene_time-samples[0][0] > .35:
                samples.popleft()
            # Pairwise slopes across at least 80 ms suppress one-frame arrival
            # jitter and box wobble. Stationary collection animations aren't rows.
            rates = [(yb-ya)/(tb-ta) for i, (ta, ya) in enumerate(samples)
                     for tb, yb in list(samples)[i+1:] if tb-ta >= .08]
            if rates:
                rate = median(rates)
                if .08 < rate < .8:
                    slopes.append(rate)
            self.cached[track.id] = track
        if measured >= 2 and moved >= max(2, measured/2):
            self.last_scroll = now
            if self.scroll_started is None:
                self.scroll_started = now
        speed = median(slopes) if slopes else self.speed
        self.speed = speed
        spread = median(abs(s-speed) for s in slopes) if slopes else .08
        can = self.raw.can
        model_retained = False
        if can is not None:
            dx = displacement(motions, scene_time, now)
            x = max(.01, min(.99, can['center'][0]+dx))
            if motions:
                last = motions[-1]
                if scene_time <= last.ends_at+self.timing_uncertainty:
                    at_scene = next((m for m in reversed(motions) if m.started_at <= scene_time), None)
                    expected_scene = (at_scene.action.source_x +
                        (at_scene.action.target_x-at_scene.action.source_x)*at_scene.fraction(scene_time)
                        if at_scene else motions[0].action.source_x)
                    rate = max(abs(m.action.target_x-m.action.source_x)/max(.001, m.action.duration)
                               for m in motions if m.ends_at >= scene_time) if last.ends_at >= scene_time else 0.
                    if abs(can['center'][0]-expected_scene) <= .02+rate*self.timing_uncertainty:
                        # The observation cannot yet confirm the endpoint. Keep
                        # the issued trajectory when its residual is explained by
                        # timing uncertainty, rather than chasing arrival jitter.
                        x = last.action.source_x + (last.action.target_x-last.action.source_x)*last.fraction(now)
                        model_retained = True
                x = max(.01, min(.99, x))
            dx = x-can['center'][0]
            can = {**can, 'center': [x, can['center'][1]],
                   'box': [can['box'][0]+dx, can['box'][1], can['box'][2]+dx, can['box'][3]]}
        tracks = []
        provenance = []
        for key, track in list(self.cached.items()):
            age = now-track.seen
            missing_for = scene_time-track.seen
            top = track.box[1]+speed*age
            # Keep lost bombs to predicted clearance. Short droplet gaps remain
            # objectives; a single missed detection is not marked as a catch.
            if top > 1.05 or (track.kind == 'droplet' and missing_for > .15):
                del self.cached[key]
                self.samples.pop(key, None)
                continue
            box = [track.box[0], top, track.box[2], track.box[3]+speed*age]
            if track.kind == 'bomb':
                pad = speed*self.timing_uncertainty + spread*min(age, .5)
                box[1] -= pad
                box[3] += pad
            else:
                # Require overlap within the narrower interval shared by early
                # and late arrival estimates, rather than leaving on an optimistic
                # contact time. An uncertain interval can be too short to target.
                pad = speed*self.timing_uncertainty
                box[1] += pad
                box[3] -= pad
                if box[1] >= box[3]:
                    continue
            tracks.append(replace(track, box=box, seen=now, speed=speed,
                                  timing_padding=pad if track.kind == 'bomb' else 0.))
            provenance.append(dict(id=key, observed_at=track.seen,
                                   missing_for=missing_for, projected=True))
        self.diagnostics = dict(frame_sequence=sequence, decoded_at=decoded_at,
                                estimated_scene_at=scene_time, estimated_now=now,
                                assumed_decode_delay=self.decode_delay,
                                timing_uncertainty=self.timing_uncertainty,
                                speed=speed, speed_spread=spread, tracks=provenance,
                                observed_can=self.raw.can, predicted_can=can,
                                retained_command_prediction=model_retained)
        return Choices(now, can, tracks=tracks,
                       mouth_box=mouth_box(can, self.config) if can else None)

    @property
    def snapshot(self):
        return self.diagnostics
