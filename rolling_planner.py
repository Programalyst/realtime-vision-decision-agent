"""Bounded route search shared by live control and the historical rolling planner.

ScheduleBuilder predicts collect/skip/dodge routes; its callers decide when to
replan. Live command execution is handled separately by TimedExecutor. The
historical RollingPlanner below retains its observation-driven behavior.
"""
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, asdict, replace
from choice_preprocessor import Choices, Track, PlannerConfig, Candidate, evaluate_trajectory, row_position


# (start time, end time, x intercept, horizontal velocity), relative to the plan.
Segment = tuple[float, float, float, float]


@dataclass
class Step:
    track_id: int
    kind: str                 # collect, skip, or dodge
    target_x: float
    command_at: float         # absolute monotonic observation time
    event_at: float           # contact for collect; clearance for dodge
    release_at: float         # next command may begin after this time


@dataclass
class Schedule:
    id: str
    steps: list[Step]
    segments: list[Segment]    # relative to created_at
    created_at: float
    horizon: float
    travel: float
    source: str = 'local'

    @property
    def catches(self):
        return sum(s.kind == 'collect' for s in self.steps)

    def position_at(self, stamp, initial):
        t = stamp-self.created_at
        for start, stop, intercept, speed in self.segments:
            if start <= t <= stop:
                return intercept+speed*t
        return self.steps[-1].target_x if self.steps else initial


@dataclass
class Branch:
    """One partial route in the beam, plus the state needed to extend it.

    Segment times and `available` are relative to the current observation.
    Step timestamps are absolute. A segment (a, b, intercept, velocity) means
    x(t) = intercept + velocity*t for a <= t <= b.
    """
    x: float                  # Body-center position at the end of this route.
    available: float          # Earliest next command, including required holds.
    steps: list[Step] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    catches: int = 0
    travel: float = 0.


class ScheduleBuilder:
    def __init__(self, config: PlannerConfig, horizon: float = 4.,
                 max_rows: int = 12, beam_width: int = 8) -> None:
        self.config = config
        # Look ahead up to 4 seconds from choices.timestamp, not 4 decision ticks.
        # This route-search horizon is separate from PlannerConfig.horizon (1 s).
        # Steps must release within it; the final safety-check tail can extend past it.
        self.horizon, self.max_rows, self.beam_width = horizon, max_rows, beam_width

    def window(self, track: Track, choices: Choices) -> tuple[float, float]:
        """Padded overlap window used to keep possible collision hazards in search."""
        box = track.box
        age = choices.timestamp-track.seen
        speed = max(track.speed,.01)
        padding = self.config.vertical_margin if track.kind == 'bomb' else 0.
        entry = (choices.mouth_box[1]-padding-box[3])/speed-age
        clear = (choices.mouth_box[3]+padding-box[1])/speed-age
        return max(0.,entry), clear

    def bomb_clearance(self, track: Track, choices: Choices) -> float:
        """Seconds until the unpadded, projected detector top reaches mouth bottom.

        Undo only the estimator's expansion; any fuse/space inside the detected
        box remains. Collision checks still use the full padded box and margins.
        """
        top = track.box[1]+track.timing_padding
        age = choices.timestamp-track.seen
        return max(0., (choices.mouth_box[3]-top)/max(track.speed,.01)-age)

    def duration(self, source: float, target: float) -> float:
        return max(self.config.min_drag,abs(target-source)/self.config.drag_speed) if abs(target-source)>.003 else 0.

    def bounds(self, choices: Choices) -> tuple[float, float, float]:
        mouth=choices.mouth_box
        half=(mouth[2]-mouth[0])/2
        offset=(mouth[0]+mouth[2])/2-choices.can['center'][0]
        return max(.01,half+self.config.margin-offset), min(.99,1-half-self.config.margin-offset), offset

    def target(self, track: Track, choices: Choices) -> float:
        low,high,offset=self.bounds(choices)
        return max(low,min(high,(track.box[0]+track.box[2])/2-offset))

    def movement(self, x: float, target: float, start: float) -> tuple[list[Segment], float]:
        duration=self.duration(x,target)
        move_at=start+(self.config.input_delay if duration else 0.)
        end=move_at+duration
        segments=[(start,move_at,x,0.)]
        if duration:
            velocity=(target-x)/duration
            segments.append((move_at,end,x-velocity*move_at,velocity))
        return segments,end

    def safe(self, choices: Choices, segments: list[Segment], end: float) -> bool:
        return evaluate_trajectory(choices,self.config,segments,end)[0] is None

    def rows(self, choices: Choices, done: Collection[int] = ()) -> list[Track]:
        return sorted((t for t in choices.tracks if t.id not in done
                       and (t.kind=='bomb' or t.seen==choices.timestamp)
                       and 0 < self.window(t,choices)[1]
                       and self.window(t,choices)[0] <= self.horizon),
                      key=lambda t:(-row_position(t,choices.timestamp),t.id))[:self.max_rows]

    def build_schedules(self, choices: Choices, *, done: Collection[int] = (),
                        start_delay: float = 0., prefix: Schedule | None = None,
                        preferences: Mapping[int, Step] | None = None,
                        limit: int = 4) -> list[Schedule]:
        """Preserve the prefix, search upcoming rows, then finalize ranked routes.

        `start_delay` reserves time for committed input or expected API latency.
        `preferences`, when supplied, fixes collect/skip choices and dodge targets.
        Other rows are then ignored as objectives, but remain collision hazards.
        `limit` caps returned schedules; `beam_width` caps partial routes in search.
        """
        if not choices.can or not choices.mouth_box:
            return []
        initial = self._initial_branch(choices, start_delay, prefix)
        if initial is None:
            return []

        covered = {step.track_id for step in initial.steps}
        rows = self.rows(choices, done)
        beams = [initial]
        for track in rows:
            if track.id in covered:
                continue
            if preferences is not None and track.id not in preferences:
                continue
            if self.window(track, choices)[1] <= initial.available:
                continue  # This row clears before the preserved prefix ends.

            preferred = preferences.get(track.id) if preferences is not None else None
            expanded = self._expand_row(choices, beams, track, rows, preferred)
            if not expanded:
                # No complete extension; don't advertise an unchecked suffix.
                return []
            # Lexicographic ranking, not a weighted score: maximize catches,
            # then minimize travel, then prefer earlier readiness for another move.
            # Prune across ALL branches at this depth, not separately per parent.
            expanded.sort(key=lambda branch: (-branch.catches, branch.travel, branch.available))
            beams = expanded[:self.beam_width]

        return self._finalize_schedules(choices, beams, initial.available, limit)

    def _initial_branch(self, choices: Choices, start_delay: float,
                        prefix: Schedule | None) -> Branch | None:
        """Seed the search with the route that cannot yet be changed, or fail."""
        now = choices.timestamp
        x = choices.can['center'][0]
        segments = []
        steps = []
        if start_delay:
            cutoff = now + start_delay
            if prefix:
                # Clip the old trajectory to the reserved interval and rebase its
                # time origin. Preserve its actual movements, not a stationary wait.
                for a, b, intercept, velocity in prefix.segments:
                    absolute_a = prefix.created_at + a
                    absolute_b = prefix.created_at + b
                    lo = max(0., absolute_a - now)
                    hi = min(start_delay, absolute_b - now)
                    if hi >= lo:
                        segments.append((lo, hi, intercept + velocity*(now-prefix.created_at), velocity))
                x = prefix.position_at(cutoff, x)
                steps = [replace(step) for step in prefix.steps
                         if step.command_at < cutoff - 1e-6
                         and (step.kind != 'skip' or step.event_at < cutoff)]
            if not segments:
                segments = [(0., start_delay, x, 0.)]
            elif segments[-1][1] < start_delay:
                segments.append((segments[-1][1], start_delay, x, 0.))

            if steps:
                # If the cutoff lands inside a retained step, preserve it through
                # release. Rebuild the prefix because that extension can include
                # more steps. The tolerance prevents recursion without progress.
                start_delay = max(start_delay, steps[-1].release_at - now)
                if segments[-1][1] < start_delay:
                    if start_delay - (cutoff-now) > .00001:
                        return self._initial_branch(choices, start_delay, prefix)
                    return None

        if start_delay > self.horizon or not self.safe(choices, segments, start_delay):
            return None
        return Branch(x, start_delay, steps, segments,
                      sum(step.kind == 'collect' for step in steps),
                      sum(abs(velocity)*(b-a) for a, b, _, velocity in segments))

    def _expand_row(self, choices: Choices, beams: list[Branch], track: Track,
                    rows: list[Track], preferred: Step | None) -> list[Branch]:
        """Extend each partial route with this row's allowed decisions."""
        now = choices.timestamp
        entry, clear = self.window(track, choices)
        expanded = []
        for branch in beams:
            if track.kind == 'droplet':
                if preferred is None or preferred.kind == 'skip':
                    # Skipping adds no movement, delay, or catch reward. It keeps
                    # a route alive when collecting would make a later dodge fail.
                    step = Step(track.id, 'skip', branch.x, now + branch.available,
                                now + entry, now + branch.available)
                    expanded.append(replace(branch, steps=branch.steps + [step]))
                targets = [] if preferred and preferred.kind == 'skip' else [self.target(track, choices)]
            else:
                targets = self._dodge_targets(choices, branch, track, rows, preferred)

            for target in targets:
                extension = self._extend_branch(choices, branch, track, target, entry, clear)
                if extension is not None:
                    expanded.append(extension)
        return expanded

    def _dodge_targets(self, choices: Choices, branch: Branch, track: Track,
                       rows: list[Track], preferred: Step | None) -> list[float]:
        """Generate destinations; full-path collision checks decide their safety."""
        now = choices.timestamp
        low, high, offset = self.bounds(choices)
        half = (choices.mouth_box[2] - choices.mouth_box[0]) / 2
        left = max(low, min(high, track.box[0] - half - 2*self.config.margin - offset))
        right = max(low, min(high, track.box[2] + half + 2*self.config.margin - offset))
        following = next((t for t in rows if t.kind == 'droplet'
                          and row_position(t, now) < row_position(track, now)), None)
        # Try staying put, either side of the bomb, and the screen limits. A
        # following droplet also supplies a useful destination for the dodge.
        targets = [preferred.target_x] if preferred else [branch.x, left, right, low, high]
        if following and not preferred:
            targets.append(self.target(following, choices))
        # Keep insertion order: changing tie order can change which branches survive.
        return list(dict.fromkeys(round(max(low, min(high, target)), 6) for target in targets))

    def _extend_branch(self, choices: Choices, branch: Branch, track: Track,
                       target: float, entry: float, clear: float) -> Branch | None:
        """Schedule one move and hold; return a checked child branch or None."""
        movement, end = self.movement(branch.x, target, branch.available)
        if track.kind == 'droplet':
            event = max(entry, end)
            if event > clear or event > self.horizon:
                return None
            # Next command waits for both post-contact settling (20 ms) and,
            # if we moved, movement-end turnaround (120 ms).
            release = max(event + self.config.catch_settle,
                          end + self.config.replan_allowance if end > branch.available else event)
        else:
            # End the bomb hold when the unpadded detector top passes the mouth
            # bottom, plus 5 ms. Padding still applies to path collision checks.
            # Capping at the horizon does not mean the bomb has actually cleared.
            event = min(self.bomb_clearance(track, choices) + .005, self.horizon)
            # An already overlapping bomb may clear before this move finishes.
            # Never rewind the timeline: release must also cover movement and
            # its turnaround allowance. All these times are relative to `now`.
            release = max(event, end + (self.config.replan_allowance if end > branch.available else 0.))
        if release > self.horizon:
            return None

        # Test the entire accumulated route against EVERY known bomb, including
        # rows not selected as objectives. A safe destination alone is insufficient.
        segments = branch.segments + movement + [(end, release, target, 0.)]
        if not self.safe(choices, segments, release):
            return None
        if track.kind == 'droplet':
            _, catches = evaluate_trajectory(choices, self.config, segments, release)
            if track.id not in catches:
                return None

        now = choices.timestamp
        step = Step(track.id, 'collect' if track.kind == 'droplet' else 'dodge', target,
                    now + branch.available, now + event, now + release)
        return Branch(target, release, branch.steps + [step], segments,
                      branch.catches + (track.kind == 'droplet'),
                      branch.travel + abs(target - branch.x))

    def _finalize_schedules(self, choices: Choices, beams: list[Branch],
                            start_delay: float, limit: int) -> list[Schedule]:
        """Add final safety holds and remove duplicates, preserving beam ranking."""
        schedules = []
        seen = set()
        for branch in beams:
            if not any(step.kind != 'skip' for step in branch.steps):
                continue  # An all-skip route supplies no collect or dodge step.

            # Check that the last destination stays safe for one more replan
            # allowance. The appended segment is a stationary hold, not a move.
            end = max(start_delay, branch.available)
            tail = end + self.config.replan_allowance
            segments = branch.segments + [(end, tail, branch.x, 0.)]
            if not self.safe(choices, segments, tail):
                continue

            # Treat matching decisions and near-identical destinations as the
            # same option. Timing is deliberately absent from this signature;
            # the first (highest-ranked) matching route survives.
            key = tuple((step.track_id, step.kind, round(step.target_x, 3)) for step in branch.steps)
            if key in seen:
                continue
            seen.add(key)
            schedules.append(Schedule(f'plan_{len(schedules)+1}', branch.steps, segments,
                                      choices.timestamp, tail, branch.travel))
            # Live deterministic selection requests one; Jev can request four.
            # Return fewer if the final safety or duplicate checks remove routes.
            if len(schedules) >= limit:
                break
        return schedules


class RollingPlanner:
    """Persistent route plus fast local safety/repair; no API dependency."""
    def __init__(self, config, source='local_fallback'):
        self.default_source=source
        self.builder=ScheduleBuilder(config)
        self.reset()

    def reset(self):
        self.active=None
        self.done=set()
        self.contacts={}
        self.hazards={}
        self.reason='waiting for a plan'
        self.source=self.default_source
        self.events=[]

    @property
    def snapshot(self):
        return {'plan':asdict(self.active) if self.active else None,
                'reason':self.reason,'source':self.source,'completed_rows':sorted(self.done)}

    def observe(self, choices: Choices) -> Choices:
        if not choices.can:
            self.active=None
            self.reason='can missing'
            return choices
        now=choices.timestamp
        for t in choices.tracks:
            if t.kind=='bomb':self.hazards[t.id]=t
        self.hazards={i:t for i,t in self.hazards.items() if self.builder.window(t,choices)[1]>0}
        known={t.id for t in choices.tracks}
        choices=replace(choices,tracks=choices.tracks+[t for i,t in self.hazards.items() if i not in known])
        if self.active:
            tracks={t.id:t for t in choices.tracks}
            mouth=choices.mouth_box
            for step in self.active.steps:
                t=tracks.get(step.track_id)
                if step.track_id in self.done:continue
                if t is None or (t.kind=='droplet' and t.seen!=now):
                    self.done.add(step.track_id)
                    continue
                if self.builder.window(t,choices)[1]<=0:
                    self.done.add(t.id)
                    continue
                if step.kind=='skip':
                    # A skip decision is part of this route, not a permanent
                    # ban on collecting a still-upcoming object after replanning.
                    continue
                if step.kind=='collect':
                    aligned=abs((t.box[0]+t.box[2]-mouth[0]-mouth[2])/2)<=(mouth[2]-mouth[0])/2*self.builder.config.catch_center_fraction
                    overlap=t.box[3]>=mouth[1] and t.box[1]<=mouth[3]
                    if aligned and overlap:
                        self.contacts.setdefault(t.id,now)
                    if t.id in self.contacts and now>=self.contacts[t.id]+self.builder.config.catch_settle:
                        self.done.add(t.id)
        return choices

    def refresh(self, choices: Choices) -> bool:
        if not self.active:return False
        preferences={s.track_id:s for s in self.active.steps if s.track_id not in self.done}
        if not preferences:
            self.active=None
            return False
        routes=self.builder.build_schedules(choices,done=self.done,preferences=preferences,limit=1)
        if not routes:
            self.events.append({'type':'plan_invalidated','source':self.active.source})
            self.active=None
            return False
        route=routes[0];route.source=self.active.source;route.id=self.active.id
        self.active=route
        return True

    def adopt(self, schedule: Schedule, choices: Choices, source: str = 'jev') -> bool:
        """Rebase remaining intent on current geometry, independent of active row."""
        if not choices.can:
            return False
        known={t.id for t in choices.tracks}
        choices=replace(choices,tracks=choices.tracks+[
            t for i,t in self.hazards.items() if i not in known and self.builder.window(t,choices)[1]>0])
        preferences={s.track_id:s for s in schedule.steps if s.track_id not in self.done}
        routes=self.builder.build_schedules(choices,done=self.done,preferences=preferences,limit=1)
        if not routes or not any(s.kind=='collect' for s in routes[0].steps):
            return False
        self.active=routes[0];self.active.id=schedule.id;self.active.source=source
        return True

    def update(self, choices: Choices, movement_ready: bool = True, *,
               local_replan: bool = True) -> Candidate | None:
        self.events=[]
        choices=self.observe(choices)
        if not choices.can:return None
        if not self.refresh(choices) and local_replan:
            routes=self.builder.build_schedules(choices,done=self.done,limit=1)
            if routes:
                self.active=routes[0]
                self.active.source=self.default_source
                self.events.append({'type':'local_plan','reason':'no valid retained route'})
        if self.active:
            self.source=self.active.source
            step=next((s for s in self.active.steps if s.kind!='skip' and s.track_id not in self.done),None)
            if step:
                self.reason=f'{self.source}: {step.kind} row {step.track_id}'
                if not movement_ready:return None
                x=choices.can['center'][0]
                target=step.target_x
                duration=self.builder.duration(x,target)
                # Refresh checked the full route; only issue its first move.
                return Candidate(f'schedule_{step.kind}_{step.track_id}',target,duration,
                                 abs(target-x),None,step.event_at-choices.timestamp if step.kind=='collect' else None)
        self.source='local_safety'
        self.reason='no feasible schedule; local safety'
        if not movement_ready or not choices.candidates:return None
        checked=[]
        for c in choices.candidates:
            segments,end=self.builder.movement(choices.can['center'][0],c.target_x,0.)
            horizon=max(.35,end+self.builder.config.replan_allowance)
            hit,_=evaluate_trajectory(choices,self.builder.config,
                                     segments+[(end,horizon,c.target_x,0.)],horizon)
            checked.append(replace(c,id='safety_'+c.id,collision_at=hit))
        safe=[c for c in checked if c.safe]
        return min(safe,key=lambda c:c.distance) if safe else max(checked,key=lambda c:(c.collision_at,-c.distance))
