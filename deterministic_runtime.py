"""Deterministic route retention and handoff to a separate timed executor.

The historical controller remains available for replay comparison. Live rules
mode uses this runtime: observations validate a retained schedule; only changed
or expired routes trigger a search. No Jev calls are involved.
"""
from dataclasses import asdict
import math
import time

from choice_preprocessor import PlannerConfig, Candidate, evaluate_trajectory
from rolling_planner import ScheduleBuilder, Schedule, Step
from motion_estimator import MotionEstimator
from timed_executor import Action


def remaining_segments(plan, now):
    offset = now-plan.created_at
    return [(max(0., a-offset), b-offset, intercept+velocity*offset, velocity)
            for a, b, intercept, velocity in plan.segments if b >= offset]


class DeterministicRuntime:
    def __init__(self, executor, config=None, *, decode_delay=.12,
                 timing_uncertainty=.02, search_interval=.15, lease=.20,
                 clock=time.monotonic, control=True):
        if not all(math.isfinite(v) and v > 0 for v in (search_interval, lease)):
            raise ValueError('Runtime timing settings must be positive finite seconds')
        self.clock = clock
        self.executor = executor
        self.config = config or PlannerConfig()
        self.builder = ScheduleBuilder(self.config)
        self.estimator = MotionEstimator(self.config, decode_delay, timing_uncertainty)
        self.search_interval, self.lease = search_interval, lease
        self.control = control
        self.enabled = False
        self.version = 0
        self.active = None
        self.actions = ()
        self.done = set()
        self.next_search = 0.
        self.events = []
        self.choices = self.decision = None
        self.active_track = None
        self.reason = 'paused'

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.executor.set_enabled(enabled and self.control)
        self.estimator.reset()
        self.active = None
        self.actions = ()
        self.done.clear()
        self.next_search = 0.
        self.decision = None
        self.reason = 'waiting for scrolling game' if enabled else 'paused'

    @property
    def snapshot(self):
        return dict(plan=asdict(self.active) if self.active else None,
                    actions=[asdict(a) for a in self.actions], version=self.version,
                    reason=self.reason, source='deterministic_timed',
                    completed_rows=sorted(self.done), control=self.control)

    def _invalidate(self, reason, cancel_current=False):
        if self.active or self.actions or (cancel_current and self.reason != reason):
            self.executor.invalidate(reason, cancel_current=cancel_current)
        self.active = None
        self.actions = ()
        self.reason = reason

    def _valid(self, choices, now):
        if self.active is None or now >= self.active.created_at+self.active.horizon:
            return False
        segments = remaining_segments(self.active, now)
        if not segments:
            return False
        expected_x = self.active.position_at(now, choices.can['center'][0])
        catch_half = (choices.mouth_box[2]-choices.mouth_box[0])/2*self.config.catch_center_fraction
        if abs(choices.can['center'][0]-expected_x) > max(.008, min(.025, catch_half*.65)):
            return False
        hit, catches = evaluate_trajectory(choices, self.config, segments,
                                           self.active.created_at+self.active.horizon-now)
        if hit is not None:
            return False
        visible = {t.id for t in choices.tracks}
        return all(s.track_id in catches for s in self.active.steps
                   if s.kind == 'collect' and s.track_id not in self.done
                   and s.track_id in visible and s.event_at > now+.05)

    def _escape(self, choices, now):
        """Short checked escape when a full multi-row route cannot be built."""
        x = choices.can['center'][0]
        low, high, offset = self.builder.bounds(choices)
        half = (choices.mouth_box[2]-choices.mouth_box[0])/2
        targets = [x, low, high]
        for track in choices.tracks:
            if track.kind == 'bomb':
                targets += [track.box[0]-half-2*self.config.margin-offset,
                            track.box[2]+half+2*self.config.margin-offset]
        safe = []
        for target in set(max(low, min(high, t)) for t in targets):
            segments, end = self.builder.movement(x, target, 0.)
            horizon = max(.35, end+self.config.replan_allowance)
            segments += [(end, horizon, target, 0.)]
            hit, _ = evaluate_trajectory(choices, self.config, segments, horizon)
            if hit is None:
                safe.append((abs(target-x), target, segments, horizon))
        if not safe:
            return None
        distance, target, segments, horizon = min(safe, key=lambda s: s[0])
        # Synthetic ID prevents a short escape from marking a real bomb passed.
        step = Step(-self.version-1, 'dodge', target, now, now+horizon, now+horizon)
        return Schedule('escape', [step], segments, now, horizon, distance, 'deterministic_safety')

    def _can_replace_fallback_wait(self, execution, now):
        """A checked waiting horizon is not a mandatory pause after input finishes."""
        waiting = [a for a in self.actions if a.row < 0
                   and a.id in execution['consumed'] and a.release_at > now]
        if not waiting or execution['active'] is not None:
            return False
        for action in waiting:
            if action.duration:
                motion = next((m for m in reversed(execution['history'])
                               if m.action.id == action.id), None)
                # A completed transport call may precede physical arrival. Keep
                # the estimated movement and existing turnaround allowance too.
                if motion is None or now < motion.ends_at+self.config.replan_allowance:
                    return False
        return True

    def _actions(self, plan, old_actions, now):
        result = []
        x = self.choices.can['center'][0]
        old = {a.row: a for a in old_actions}
        for step in plan.steps:
            if step.kind == 'skip' or step.track_id in self.done:
                continue
            # The builder copied a committed prefix: preserve its command ID so
            # replacing the future schedule cannot resend that movement.
            prior = old.get(step.track_id)
            if prior and abs(prior.start_at-step.command_at) < 1e-5:
                action = prior
            else:
                duration = self.builder.duration(x, step.target_x)
                action = Action(f'{self.executor.generation}:{self.version}:{step.track_id}',
                                step.track_id, step.kind, x, step.target_x,
                                self.choices.can['center'][1], step.command_at,
                                step.command_at+.035, duration, step.release_at)
            result.append(action)
            x = step.target_x
        return tuple(result)

    def update(self, state, sequence, decoded_at, *, now=None):
        now = self.clock() if now is None else now
        self.events = self.executor.drain_events()
        self.decision = None
        execution = self.executor.snapshot()
        choices = self.estimator.update(state, sequence, decoded_at, now, execution['history'])
        if choices is None:
            self.reason = 'duplicate, stale, or out-of-order observation'
            return 'Rules: '+self.reason
        self.choices = choices
        if not self.enabled:
            return 'Rules: PAUSED - press J to start'
        if execution['error'] or not choices.can:
            reason = 'input transport error' if execution['error'] else 'can missing'
            self._invalidate(reason, cancel_current=True)
            return 'Rules: '+reason
        if self.estimator.last_scroll is None or now-self.estimator.last_scroll > .7:
            self._invalidate('waiting for scrolling game', cancel_current=True)
            return 'Rules: '+self.reason
        for step in self.active.steps if self.active else ():
            action = next((a for a in self.actions if a.row == step.track_id), None)
            if action and action.id in execution['consumed'] and now >= step.release_at:
                self.done.add(step.track_id)  # predicted step completion, not game score
        for track in choices.tracks:
            if track.box[1] > choices.mouth_box[3]+self.config.vertical_margin:
                self.done.add(track.id)
        valid = self._valid(choices, now)
        expired = any(e['type'] == 'expired' for e in self.events)
        valid = valid and not expired
        old_plan, old_actions = self.active, self.actions
        if execution['active'] and old_plan is None:
            self.reason = 'waiting for already dispatched movement to finish'
            return 'Rules: '+self.reason
        replace_fallback = self._can_replace_fallback_wait(execution, now)
        if not valid or now >= self.next_search or replace_fallback:
            # A dispatched action and its hold interval remain committed even if
            # a delayed observation disagrees. Only completed fallback waits are
            # interruptible; collection and real bomb-row commitments stay intact.
            committed = [a for a in old_actions if a.id in execution['consumed'] and a.release_at > now
                         and not (replace_fallback and a.row < 0)]
            cutoff = max((a.release_at-now for a in committed), default=0.)
            if execution['active']:
                cutoff = max(cutoff, execution['active'].ends_at+self.config.replan_allowance-now)
            routes = self.builder.build(choices, done=self.done, start_delay=max(0., cutoff),
                                        prefix=old_plan if cutoff else None, limit=1)
            self.next_search = now+self.search_interval
            if routes:
                proposed = routes[0]
                old_catches = sum(s.kind == 'collect' and s.track_id not in self.done
                                  for s in old_plan.steps) if old_plan else -1
                new_catches = sum(s.kind == 'collect' and s.track_id not in self.done
                                  for s in proposed.steps)
                # Equal catch counts can still benefit from leaving a fallback
                # wait earlier. Keep the old safe plan if search finds no route
                # or only a valid route with fewer predicted catches.
                replace_wait = replace_fallback and new_catches >= old_catches
                if not valid or new_catches > old_catches or replace_wait:
                    self.version += 1
                    self.active = proposed
                    self.active.source = 'deterministic_timed'
                    self.actions = self._actions(proposed, old_actions, now)
                    self.events.append(dict(type='plan_replaced', version=self.version,
                                            reason='invalid/expired' if not valid else
                                                   'interruptible fallback wait' if replace_wait else
                                                   'additional reachable droplet'))
                    valid = True
            if not valid:
                escape = self._escape(choices, now) if execution['active'] is None else None
                if escape:
                    self.version += 1
                    self.active = escape
                    self.actions = self._actions(escape, (), now)
                    self.events.append(dict(type='safety_plan', version=self.version))
                else:
                    self._invalidate('no validated route; waiting for a new observation')
                    return 'Rules: '+self.reason
        if self.active:
            # Publishing against the snapshot revision prevents a plan computed
            # before a concurrent dispatch from overwriting its committed prefix.
            try:
                accepted = self.executor.publish(self.actions, version=self.version,
                            generation=execution['generation'], revision=execution['revision'],
                            valid_until=now+self.lease, pose=(choices.can['center'][0], now)) if self.control else True
            except ValueError as error:
                # Never sort malformed actions: their source positions and safe
                # paths depend on their original order. Clear the pending queue
                # and rebuild on the next observation; in-flight input can finish.
                self.events.append(dict(type='schedule_rejected', version=self.version,
                                        reason=str(error)))
                self._invalidate('invalid action schedule; replanning next observation')
                self.next_search = 0.
                return 'Rules: '+self.reason
            if not accepted:
                self.active, self.actions = old_plan, old_actions
                self.reason = 'executor changed during planning; retry next frame'
                return 'Rules: '+self.reason
            upcoming = next((s for s in self.active.steps
                             if s.kind != 'skip' and s.release_at > now), None)
            self.active_track = upcoming.track_id if upcoming else None
            self.reason = f'plan {self.version}: {upcoming.kind} row {upcoming.track_id}' if upcoming else 'plan complete'
            if upcoming:
                self.decision = Candidate(f'timed_{upcoming.kind}_{upcoming.track_id}', upcoming.target_x,
                                          0., abs(upcoming.target_x-choices.can['center'][0]), None,
                                          upcoming.event_at-now if upcoming.kind == 'collect' else None)
        return 'Rules: '+self.reason

    def close(self):
        self.executor.close()
