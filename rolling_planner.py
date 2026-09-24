"""Bounded planning over visible rows and local execution of retained schedules.

Plans are estimates, not queued ADB commands. Every frame rebuilds the remaining
route from observed positions and checks all visible/retained bombs.
"""
from dataclasses import dataclass, field, asdict, replace
from choice_preprocessor import Candidate, evaluate_trajectory, row_position


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
    segments: list[tuple]      # relative to created_at
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
    x: float
    available: float
    steps: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    catches: int = 0
    travel: float = 0.


class ScheduleBuilder:
    def __init__(self, config, horizon=4., max_rows=12, beam_width=8):
        self.config = config
        # Look ahead up to 4 seconds from choices.timestamp, not 4 decision ticks.
        # This route-search horizon is separate from PlannerConfig.horizon (1 s).
        # Steps must release within it; the final safety-check tail can extend past it.
        self.horizon, self.max_rows, self.beam_width = horizon, max_rows, beam_width

    def window(self, track, choices):
        """Padded overlap window used to keep possible collision hazards in search."""
        box = track.box
        age = choices.timestamp-track.seen
        speed = max(track.speed,.01)
        padding = self.config.vertical_margin if track.kind == 'bomb' else 0.
        entry = (choices.mouth_box[1]-padding-box[3])/speed-age
        clear = (choices.mouth_box[3]+padding-box[1])/speed-age
        return max(0.,entry), clear

    def bomb_clearance(self, track, choices):
        """Seconds until the unpadded, projected detector top reaches mouth bottom.

        Undo only the estimator's expansion; any fuse/space inside the detected
        box remains. Collision checks still use the full padded box and margins.
        """
        top = track.box[1]+track.timing_padding
        age = choices.timestamp-track.seen
        return max(0., (choices.mouth_box[3]-top)/max(track.speed,.01)-age)

    def duration(self, source, target):
        return max(self.config.min_drag,abs(target-source)/self.config.drag_speed) if abs(target-source)>.003 else 0.

    def bounds(self, choices):
        mouth=choices.mouth_box
        half=(mouth[2]-mouth[0])/2
        offset=(mouth[0]+mouth[2])/2-choices.can['center'][0]
        return max(.01,half+self.config.margin-offset), min(.99,1-half-self.config.margin-offset), offset

    def target(self, track, choices):
        low,high,offset=self.bounds(choices)
        return max(low,min(high,(track.box[0]+track.box[2])/2-offset))

    def movement(self, x, target, start):
        duration=self.duration(x,target)
        move_at=start+(self.config.input_delay if duration else 0.)
        end=move_at+duration
        segments=[(start,move_at,x,0.)]
        if duration:
            velocity=(target-x)/duration
            segments.append((move_at,end,x-velocity*move_at,velocity))
        return segments,end

    def safe(self, choices, segments, end):
        return evaluate_trajectory(choices,self.config,segments,end)[0] is None

    def rows(self, choices, done=()):
        return sorted((t for t in choices.tracks if t.id not in done
                       and (t.kind=='bomb' or t.seen==choices.timestamp)
                       and 0 < self.window(t,choices)[1]
                       and self.window(t,choices)[0] <= self.horizon),
                      key=lambda t:(-row_position(t,choices.timestamp),t.id))[:self.max_rows]

    def build(self, choices, *, done=(), start_delay=0., prefix=None, preferences=None, limit=4):
        """Search row by row; future alternatives share the active plan prefix.

        preferences fixes retained collect/skip decisions and dodge destinations.
        Unplanned new rows are ignored as objectives but remain collision hazards.
        """
        if not choices.can or not choices.mouth_box:
            return []
        now=choices.timestamp
        x=choices.can['center'][0]
        start_segments=[]
        initial_steps=[]
        # Continue the already accepted route while the API is working.
        if start_delay:
            cutoff=now+start_delay
            if prefix:
                for a,b,intercept,v in prefix.segments:
                    absolute_a=prefix.created_at+a
                    absolute_b=prefix.created_at+b
                    lo=max(0.,absolute_a-now);hi=min(start_delay,absolute_b-now)
                    if hi>=lo:
                        start_segments.append((lo,hi,intercept+v*(now-prefix.created_at),v))
                x=prefix.position_at(cutoff,x)
                initial_steps=[replace(s) for s in prefix.steps
                               if s.command_at<cutoff-1e-6 and (s.kind!='skip' or s.event_at<cutoff)]
            if not start_segments:
                start_segments=[(0.,start_delay,x,0.)]
            elif start_segments[-1][1]<start_delay:
                start_segments.append((start_segments[-1][1],start_delay,x,0.))
            # Do not splice a different route into the middle of a retained step.
            if initial_steps:
                start_delay=max(start_delay,initial_steps[-1].release_at-now)
                last=initial_steps[-1]
                if start_segments[-1][1]<start_delay:
                    # Use the actual retained trajectory for the extended prefix.
                    return self.build(choices,done=done,start_delay=start_delay,
                                      prefix=prefix,preferences=preferences,limit=limit) if start_delay-(cutoff-now)>.00001 else []
        if start_delay>self.horizon or not self.safe(choices,start_segments,start_delay):
            return []
        covered={s.track_id for s in initial_steps}
        rows=self.rows(choices,done)
        beams=[Branch(x,start_delay,initial_steps,start_segments,
                      sum(s.kind=='collect' for s in initial_steps),
                      sum(abs(v)*(b-a) for a,b,_,v in start_segments))]
        for track in rows:
            if track.id in covered:
                continue
            if preferences is not None and track.id not in preferences:
                continue
            entry,clear=self.window(track,choices)
            if clear<=start_delay:
                continue
            expanded=[]
            preferred=preferences.get(track.id) if preferences is not None else None
            for branch in beams:
                if track.kind=='droplet':
                    if preferred is None or preferred.kind=='skip':
                        # Keep an alternative that does not require this catch.
                        # It consumes no time and adds no catch reward. Later path
                        # checks still enforce bomb safety. Removing this option
                        # can leave no route when a required catch is infeasible.
                        step=Step(track.id,'skip',branch.x,now+branch.available,now+entry,now+branch.available)
                        expanded.append(replace(branch,steps=branch.steps+[step]))
                    targets=[] if preferred and preferred.kind=='skip' else [self.target(track,choices)]
                else:
                    low,high,offset=self.bounds(choices)
                    half=(choices.mouth_box[2]-choices.mouth_box[0])/2
                    left=max(low,min(high,track.box[0]-half-2*self.config.margin-offset))
                    right=max(low,min(high,track.box[2]+half+2*self.config.margin-offset))
                    following=next((t for t in rows if t.kind=='droplet' and row_position(t,now)<row_position(track,now)),None)
                    targets=[preferred.target_x] if preferred else [branch.x,left,right,low,high]
                    if following and not preferred:
                        targets.append(self.target(following,choices))
                    targets=list(dict.fromkeys(round(max(low,min(high,t)),6) for t in targets))
                for target in targets:
                    movement,end=self.movement(branch.x,target,branch.available)
                    if track.kind=='droplet':
                        event=max(entry,end)
                        if event>clear or event>self.horizon:
                            continue
                        # Next command waits for both post-contact settling (20 ms)
                        # and, if we moved, movement-end turnaround (120 ms).
                        release=max(event+self.config.catch_settle,
                                    end+self.config.replan_allowance if end>branch.available else event)
                    else:
                        # Bomb: all these times are seconds relative to `now`.
                        # End the hold using the unpadded detector top, projected
                        # to now, passing the mouth bottom. Unlike `clear` above,
                        # this excludes vertical_margin and estimator expansion.
                        # Add 5 ms beyond clearance, capped at the search horizon;
                        # hitting that cap does NOT mean the bomb has really passed.
                        event=min(self.bomb_clearance(track,choices)+.005,self.horizon)
                        # `end` is completion of the dodge (input delay + drag).
                        # `release` is the earliest NEXT command, not dodge start:
                        # hold this target until clearance AND, if we moved,
                        # end + replan_allowance (120 ms by default).
                        # Releasing the hold does not remove the bomb as a hazard:
                        # the next path still has to pass padded collision checks.
                        # A second/overlapping bomb may already be clear by the
                        # time this branch is available. Even with no movement,
                        # release cannot precede `end` and rewind the timeline.
                        release=max(event,end+(self.config.replan_allowance if end>branch.available else 0.))
                    if release>self.horizon:
                        continue
                    # Check the entire movement plus hold against every bomb,
                    # including bombs in later rows; a safe endpoint is insufficient.
                    segments=branch.segments+movement+[(end,release,target,0.)]
                    if not self.safe(choices,segments,release):
                        continue
                    if track.kind=='droplet':
                        _,catches=evaluate_trajectory(choices,self.config,segments,release)
                        if track.id not in catches:
                            continue
                    step=Step(track.id,'collect' if track.kind=='droplet' else 'dodge',target,
                              now+branch.available,now+event,now+release)
                    expanded.append(Branch(target,release,branch.steps+[step],segments,
                                           branch.catches+(track.kind=='droplet'),
                                           branch.travel+abs(target-branch.x)))
            if not expanded:
                # No complete extension; don't advertise an unchecked suffix.
                return []
            # Prefer more catches, then less travel, then earlier availability.
            # Keep only beam_width alternatives: this is not an exhaustive search.
            expanded.sort(key=lambda b:(-b.catches,b.travel,b.available))
            beams=expanded[:self.beam_width]
        schedules=[];seen=set()
        for branch in beams:
            if not any(s.kind!='skip' for s in branch.steps):
                continue
            end=max(start_delay,branch.available)
            tail=end+self.config.replan_allowance
            segments=branch.segments+[(end,tail,branch.x,0.)]
            if not self.safe(choices,segments,tail):
                continue
            key=tuple((s.track_id,s.kind,round(s.target_x,3)) for s in branch.steps)
            if key in seen:continue
            seen.add(key)
            schedules.append(Schedule(f'plan_{len(schedules)+1}',branch.steps,segments,now,tail,branch.travel))
            if len(schedules)>=limit:break
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

    def observe(self, choices):
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

    def refresh(self, choices):
        if not self.active:return False
        preferences={s.track_id:s for s in self.active.steps if s.track_id not in self.done}
        if not preferences:
            self.active=None
            return False
        routes=self.builder.build(choices,done=self.done,preferences=preferences,limit=1)
        if not routes:
            self.events.append({'type':'plan_invalidated','source':self.active.source})
            self.active=None
            return False
        route=routes[0];route.source=self.active.source;route.id=self.active.id
        self.active=route
        return True

    def adopt(self, schedule, choices, source='jev'):
        """Rebase remaining intent on current geometry, independent of active row."""
        if not choices.can:
            return False
        known={t.id for t in choices.tracks}
        choices=replace(choices,tracks=choices.tracks+[
            t for i,t in self.hazards.items() if i not in known and self.builder.window(t,choices)[1]>0])
        preferences={s.track_id:s for s in schedule.steps if s.track_id not in self.done}
        routes=self.builder.build(choices,done=self.done,preferences=preferences,limit=1)
        if not routes or not any(s.kind=='collect' for s in routes[0].steps):
            return False
        self.active=routes[0];self.active.id=schedule.id;self.active.source=source
        return True

    def update(self, choices, movement_ready=True, *, local_replan=True):
        self.events=[]
        choices=self.observe(choices)
        if not choices.can:return None
        if not self.refresh(choices) and local_replan:
            routes=self.builder.build(choices,done=self.done,limit=1)
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
