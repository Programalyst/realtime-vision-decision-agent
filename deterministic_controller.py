"""Deterministic choice policy, independent of YOLO, SDKs, and Android."""
from conditional_planner import ConditionalPlanner
from choice_preprocessor import ChoicePreprocessor, row_position, actionable_rows


def select_candidate(choices):
    if not choices.candidates:
        return None
    safe = [c for c in choices.candidates if c.safe]
    catches = [c for c in safe if c.catch_at is not None]
    if catches:
        return min(catches, key=lambda c: (round(c.catch_at, 3), -c.catch_count, c.distance + (abs(c.escape_target_x-c.target_x) if c.escape_target_x is not None else 0), c.id))
    if safe:
        return min(safe, key=lambda c: (c.distance, c.id))
    # No predicted-safe candidate: delay the earliest collision as much as possible.
    return max(choices.candidates, key=lambda c: (c.collision_at, -c.distance))


class ConditionalController:
    """Conditional short sequences with row-based fallback and continuous safety checks."""

    def __init__(self, config=None):
        self.preprocessor = ChoicePreprocessor(config)
        self.sequence = ConditionalPlanner(self.preprocessor.config)
        self.enabled = False
        self.decision = None
        self.choices = None
        self.active_track = None
        self.target_x = None

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.decision = None
        self.preprocessor.reset()
        self.sequence.reset()
        self.active_track = self.target_x = None

    def update(self, state, captured_at, movement_ready=True):
        if self.preprocessor.last_time is not None and captured_at <= self.preprocessor.last_time:
            self.sequence.reset()
            self.active_track = self.target_x = None
        extras = [('row_target', self.target_x)] if self.target_x is not None else []
        self.choices = self.preprocessor.update(state, captured_at, extras,
                                               row_mode=True, active_track=self.active_track)
        self.decision = None
        if not self.enabled:
            self.sequence.reset()
            return 'Rules: PAUSED - press J to start'
        if not self.choices.can:
            self.sequence.reset()
            return 'Rules: waiting for can'
        cfg = self.preprocessor.config
        mouth = self.choices.mouth_box
        upcoming = actionable_rows(self.choices.tracks, captured_at,
                                   mouth, cfg)
        planned = self.sequence.update(self.choices, allow_new=movement_ready)
        if planned is not None:
            self.active_track = (max(upcoming,key=lambda t:row_position(t,captured_at)).id
                                 if upcoming else None)
            if not movement_ready:
                return f'Rules: {self.sequence.reason} - waiting for drag/fresh frame'
            self.decision = planned
            self.target_x = planned.target_x
            self.choices.candidates.append(planned)
            risk = 'clear' if planned.safe else 'NO SAFE MOVE'
            return f'Rules: {self.sequence.reason} x={planned.target_x:.2f} {risk}'
        if not upcoming:
            self.active_track = self.target_x = None
            return 'Rules: waiting for next row'
        # Rows cannot overtake one another. Velocity estimates must not reorder
        # them, including when a previously missed lower object reappears.
        current = max(upcoming, key=lambda t: (row_position(t, captured_at), -t.id))
        if current.id != self.active_track:
            self.active_track, self.target_x = current.id, None
        if not movement_ready:
            return f'Rules: row {current.id} {current.kind} - waiting for drag/fresh frame'
        candidates = self.choices.candidates
        safe = [c for c in candidates if c.safe]
        x = self.choices.can['center'][0]
        offset = (mouth[0]+mouth[2])/2-x
        if current.kind == 'droplet':
            desired = (current.box[0]+current.box[2])/2-offset
            # Align with this row even before it enters the catch horizon.
            if safe:
                best = min(safe, key=lambda c:(abs(c.target_x-desired),c.distance))
            else:
                best = select_candidate(self.choices)
        else:
            following = [t for t in upcoming if t.kind == 'droplet' and row_position(t, captured_at) < row_position(current, captured_at)]
            next_drop = max(following, key=lambda t: row_position(t, captured_at)) if following else None
            preferred = ((next_drop.box[0]+next_drop.box[2])/2-offset) if next_drop else x
            # Clear the current bomb while preparing for the next reachable drop.
            half = (mouth[2]-mouth[0])/2
            def clear(c):
                center = c.target_x+offset
                return (center+half+cfg.margin < current.box[0]
                        or center-half-cfg.margin > current.box[2])
            escapes = [c for c in safe if clear(c)]
            # Pre-align with the next droplet whenever the entire move is
            # safe. Holding/minimal dodges used to waste this available time.
            if next_drop is not None and escapes:
                best = min(escapes, key=lambda c: (abs(c.target_x-preferred), c.distance))
            else:
                best = min(escapes, key=lambda c: c.distance, default=None)
            if best is None:
                best = select_candidate(self.choices)
        self.decision = best
        if best is None:
            return 'Rules: no candidate'
        self.target_x = best.target_x
        risk = 'clear' if best.safe else 'NO SAFE MOVE'
        return f'Rules: row {current.id} {current.kind} x={best.target_x:.2f} {risk}'

    def close(self):
        pass


class DeterministicController:
    """Select the best rolling visible-row schedule and execute it locally."""

    def __init__(self, config=None):
        from rolling_planner import RollingPlanner
        self.preprocessor = ChoicePreprocessor(config)
        self.sequence = RollingPlanner(self.preprocessor.config, source="deterministic")
        self.enabled = False
        self.decision = self.choices = None
        self.active_track = None

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.preprocessor.reset()
        self.sequence.reset()
        self.decision = None
        self.active_track = None

    def update(self, state, captured_at, movement_ready=True):
        if self.preprocessor.last_time is not None and captured_at <= self.preprocessor.last_time:
            self.sequence.reset()
        self.choices = self.preprocessor.update(state,captured_at,row_mode=True)
        self.decision = None
        if not self.enabled:
            return 'Rules: PAUSED - press J to start'
        self.decision = self.sequence.update(self.choices,movement_ready)
        self.active_track = next((s.track_id for s in self.sequence.active.steps
                                  if s.kind!='skip' and s.track_id not in self.sequence.done),None) if self.sequence.active else None
        if self.decision:
            self.choices.candidates.append(self.decision)
        return 'Rules: '+self.sequence.reason+(' - drag busy' if not movement_ready else '')

    def close(self):
        pass
