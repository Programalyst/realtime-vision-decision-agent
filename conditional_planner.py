"""Short, conditional movement sequences for the deterministic controller only.

Plans are rechecked from observed positions; deadlines do not imply a catch or
physical arrival. ADB execution remains single-command and nonblocking.
"""
from dataclasses import dataclass, asdict, replace
from choice_preprocessor import Candidate, evaluate_trajectory, row_position


@dataclass
class Plan:
    kind: str
    phase: str
    droplet_id: int
    bomb_ids: list[int]
    first_target: float
    second_target: float
    switch_at: float
    expires_at: float
    predicted_catch_at: float
    contact_seen: bool = False


class ConditionalPlanner:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        self.plan = None
        self.hazards = {}
        self.reason = None

    @property
    def snapshot(self):
        return {'plan': asdict(self.plan) if self.plan else None, 'reason': self.reason,
                'retained_hazards': [asdict(t) for t in self.hazards.values()]}

    def duration(self, a, b):
        distance = abs(b-a)
        return max(self.config.min_drag, distance/self.config.drag_speed) if distance > .003 else 0.

    def move(self, source, target, command_at=0.):
        start = command_at+self.config.input_delay
        duration = self.duration(source, target)
        end = start+duration
        segments = [(command_at, start, source, 0.)]
        if duration:
            speed = (target-source)/duration
            segments.append((start, end, source-speed*start, speed))
        return segments, end

    def clear_time(self, t, choices):
        top = t.box[1]+t.speed*(choices.timestamp-t.seen)
        return max(0., (choices.mouth_box[3]+self.config.vertical_margin-top)/max(t.speed,.01))

    def clear_at_destination(self, target, bomb, choices):
        mouth = choices.mouth_box
        offset = (mouth[0]+mouth[2])/2-choices.can['center'][0]
        half = (mouth[2]-mouth[0])/2
        return (target+offset+half+self.config.margin < bomb.box[0]
                or target+offset-half-self.config.margin > bomb.box[2])

    def evaluate_move(self, choices, target, horizon):
        x = choices.can['center'][0]
        segments, end = self.move(x,target)
        horizon = max(horizon, end+self.config.replan_allowance)
        hit, catches = evaluate_trajectory(choices,self.config,
                                          segments+[(end,horizon,target,0.)],horizon)
        return Candidate('plan_'+(self.plan.phase if self.plan else 'guard'), target,
                         self.duration(x,target),abs(x-target),hit,
                         min(catches.values(),default=None),catch_count=len(catches))

    def build(self, choices, droplet, bombs):
        """Enumerate dodge/wait/return or catch/escape with a complete path check."""
        cfg = self.config
        now = choices.timestamp
        x = choices.can['center'][0]
        offset = sum(choices.mouth_box[::2])/2-x
        desired = (droplet.box[0]+droplet.box[2])/2-offset
        catch_target = min(choices.candidates,key=lambda c:abs(c.target_x-desired)).target_x
        leading = [b for b in bombs if row_position(b,now)>row_position(droplet,now)]
        following = [b for b in bombs if row_position(b,now)<row_position(droplet,now)]
        blockers = leading or sorted(following,key=lambda b:-row_position(b,now))[:1]
        if not blockers:
            return None
        clear = max(self.clear_time(b,choices) for b in blockers)
        # Keep plans short. Further rows will be planned from new observations.
        if clear > 2*cfg.horizon:
            return None
        options = []
        for destination in choices.candidates:
            refuge = destination.target_x
            if not all(self.clear_at_destination(refuge,b,choices) for b in blockers):
                continue
            if leading:
                first, end = self.move(x,refuge)
                # Allow command turnaround, and require bomb clearance before return.
                switch = max(end+cfg.replan_allowance,clear+.01)
                second, finish = self.move(refuge,catch_target,switch)
                kind = 'dodge_wait_return'
                first_target, second_target = refuge, catch_target
            else:
                first, end = self.move(x,catch_target)
                hit, catches = evaluate_trajectory(choices,cfg,
                    first+[(end,2*cfg.horizon,catch_target,0.)],2*cfg.horizon)
                catch = catches.get(droplet.id)
                if catch is None or (hit is not None and hit <= catch):
                    continue
                # Earliest next command after observed/predicted contact and
                # turnaround. Once committed, this absolute deadline is retained.
                switch = max(end+cfg.replan_allowance,catch+cfg.catch_settle)
                second, finish = self.move(catch_target,refuge,switch)
                kind = 'catch_escape'
                first_target, second_target = catch_target, refuge
            horizon = max(clear+cfg.replan_allowance,finish+cfg.replan_allowance,
                          self.clear_time(droplet,choices))
            if horizon > 2*cfg.horizon:
                continue
            segments = first+[(end,switch,first_target,0.)]+second+[(finish,horizon,second_target,0.)]
            hit, catches = evaluate_trajectory(choices,cfg,segments,horizon)
            catch = catches.get(droplet.id)
            if hit is not None or catch is None:
                continue
            plan = Plan(kind,'dodge' if leading else 'catch',droplet.id,
                        [b.id for b in blockers],first_target,second_target,
                        now+switch,now+horizon+.2,now+catch)
            options.append((round(catch,4), abs(first_target-catch_target) if leading else 0.,
                            abs(x-first_target)+abs(first_target-second_target),plan))
        return min(options,key=lambda o:o[:3])[3] if options else None

    def advance(self, choices):
        plan = self.plan
        now = choices.timestamp
        tracks = {t.id:t for t in choices.tracks}
        for bid in plan.bomb_ids:
            if bid in tracks:
                self.hazards[bid] = tracks[bid]
        blockers = [self.hazards[bid] for bid in plan.bomb_ids]
        # Retain the last hazard estimate if the tracker drops it before clearance.
        # Revalidation must still include that hazard in every path check.
        known = set(tracks)
        checked = replace(choices,tracks=choices.tracks+[b for b in blockers if b.id not in known])
        if now > plan.expires_at:
            self.reason = 'plan expired; replan'
            self.plan = None
            return None
        droplet = tracks.get(plan.droplet_id)
        mouth = choices.mouth_box
        x = choices.can['center'][0]
        if plan.kind == 'catch_escape' and plan.phase == 'catch':
            if droplet and droplet.seen == now:
                center = (droplet.box[0]+droplet.box[2])/2
                aligned = abs(center-(mouth[0]+mouth[2])/2) <= (mouth[2]-mouth[0])/2*self.config.catch_center_fraction
                overlap = droplet.box[3]>=mouth[1] and droplet.box[1]<=mouth[3]
                plan.contact_seen |= aligned and overlap
            missing = droplet is None or droplet.seen != now
            if now >= plan.switch_at or (plan.contact_seen and missing):
                plan.phase = 'escape'  # Escape on deadline even if catch was missed.
        elif plan.kind == 'dodge_wait_return' and plan.phase in ('dodge','wait'):
            if all(self.clear_time(b,checked) == 0 for b in blockers):
                plan.phase = 'return'
            elif abs(x-plan.first_target)<.015:
                plan.phase = 'wait'
        if plan.phase == 'escape' and all(self.clear_time(b,checked)==0 for b in blockers):
            self.plan = None
            self.reason = 'hazard cleared; replan'
            return None
        if plan.phase in ('return','catch') and (droplet is None or droplet.seen != now):
            # A vanished target is not a reason to cross into a bomb.
            if plan.kind == 'catch_escape':
                plan.phase = 'escape'
            else:
                self.plan = None
                self.reason = 'return droplet lost; replan'
                return None
        if plan.phase == 'return' and droplet and self.clear_time(droplet,choices)==0:
            self.plan = None
            self.reason = 'return row passed; replan'
            return None
        target = plan.second_target if plan.phase in ('return','escape') else plan.first_target
        if plan.phase == 'catch':
            # Recheck the remaining two-leg plan, rather than falsely requiring
            # the catch destination to remain safe after the reserved escape.
            first,end = self.move(x,target)
            switch = max(plan.switch_at-now,end+self.config.replan_allowance)
            second,finish = self.move(target,plan.second_target,switch)
            horizon = max(finish+self.config.replan_allowance,
                          max(self.clear_time(b,checked) for b in blockers))
            hit,catches = evaluate_trajectory(checked,self.config,
                first+[(end,switch,target,0.)]+second+[(finish,horizon,plan.second_target,0.)],horizon)
            result = Candidate('plan_catch',target,self.duration(x,target),abs(x-target),hit,
                               catches.get(plan.droplet_id),catch_track_id=plan.droplet_id)
        else:
            horizon = (max(self.clear_time(b,checked) for b in blockers)+self.config.replan_allowance
                       if plan.phase in ('dodge','wait','escape') else self.config.horizon)
            result = self.evaluate_move(checked,target,horizon)
        if not result.safe:
            self.plan = None
            self.reason = 'plan path unsafe; replan'
            return None
        self.reason = plan.kind+': '+plan.phase
        return result

    def update(self, choices, allow_new=True):
        if not choices.can or not choices.candidates:
            self.reset()
            return None
        current_ids = {t.id for t in choices.tracks}
        self.hazards = {key:t for key,t in self.hazards.items()
                        if self.clear_time(t,choices)>0 or self.plan and key in self.plan.bomb_ids}
        choices = replace(choices, tracks=choices.tracks+[
            t for key,t in self.hazards.items() if key not in current_ids])
        if self.plan:
            result = self.advance(choices)
            if result is not None:
                return result
        if not allow_new:
            return None
        now = choices.timestamp
        bombs = [t for t in choices.tracks if t.kind=='bomb' and 0 < self.clear_time(t,choices) <= 2*self.config.horizon]
        drops = sorted((t for t in choices.tracks if t.kind=='droplet' and t.seen==now
                        and self.clear_time(t,choices)>0),key=lambda t:-row_position(t,now))
        for drop in drops[:2]:
            plan = self.build(choices,drop,bombs)
            if plan:
                self.plan = plan
                self.hazards = {b.id:b for b in bombs if b.id in plan.bomb_ids}
                result = self.advance(choices)
                if result is not None:
                    return result
        if bombs:
            # If a sequence cannot be completed safely, wait/dodge rather than
            # pre-align under a later bomb outside the old short row window.
            horizon = min(2*self.config.horizon,
                          max(self.clear_time(b,choices) for b in bombs)+self.config.replan_allowance)
            guarded = [self.evaluate_move(choices,c.target_x,horizon) for c in choices.candidates]
            safe = [c for c in guarded if c.safe]
            self.reason = 'no safe sequence; clear hazards'
            return min(safe,key=lambda c:c.distance) if safe else max(guarded,key=lambda c:(c.collision_at,-c.distance))
        self.reason = 'no bomb sequence needed'
        return None
