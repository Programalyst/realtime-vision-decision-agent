"""Deterministic choice policy, independent of YOLO, SDKs, and Android."""
from choice_preprocessor import ChoicePreprocessor


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


class DeterministicController:
    def __init__(self, config=None):
        self.preprocessor = ChoicePreprocessor(config)
        self.enabled = False
        self.decision = None
        self.choices = None
        self.pending_escape = None

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.decision = None
        self.preprocessor.reset()
        self.pending_escape = None

    def update(self, state, captured_at):
        extra = [('scheduled_escape', self.pending_escape['target'])] if self.pending_escape else []
        self.choices = self.preprocessor.update(state, captured_at, extra_targets=extra)
        self.decision = select_candidate(self.choices) if self.enabled else None
        if self.enabled and self.pending_escape and self.choices.can:
            pending = self.pending_escape
            if captured_at >= pending['command_at']:
                direct = [c for c in self.choices.candidates
                          if c.escape_target_x is None and c.safe
                          and abs(c.target_x-pending['target']) < .003]
                arrived = abs(self.choices.can['center'][0]-pending['target']) < .015
                if arrived or captured_at > pending['command_at']+.6 or not direct:
                    self.pending_escape = None
                else:
                    # Recheck the escape against new bombs before issuing it.
                    self.decision = min(direct, key=lambda c:c.distance)
            elif self.decision and self.decision.catch_track_id != pending['track']:
                # Keep a safe approach to the original catch until the escape deadline.
                same_catch = [c for c in self.choices.candidates if c.safe
                              and c.catch_track_id == pending['track']]
                if same_catch:
                    self.decision = min(same_catch, key=lambda c:(c.catch_at,c.distance))
                else:
                    self.pending_escape = None
        if (self.enabled and self.decision and self.decision.escape_target_x is not None
                and self.pending_escape is None):
            self.pending_escape = {
                'target': self.decision.escape_target_x,
                'command_at': captured_at+self.decision.escape_at-self.preprocessor.config.input_delay,
                'track': self.decision.catch_track_id,
            }
        if not self.enabled:
            return 'Rules: PAUSED - press J to start'
        if self.decision is None:
            return 'Rules: waiting for can'
        risk = 'safe' if self.decision.safe else 'NO SAFE MOVE'
        return f'Rules: {self.decision.id} x={self.decision.target_x:.2f} {risk}'

    def close(self):
        pass
