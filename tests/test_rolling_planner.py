from dataclasses import replace
import unittest
from choice_preprocessor import ChoicePreprocessor,PlannerConfig,mouth_box,evaluate_trajectory
from rolling_planner import RollingPlanner,ScheduleBuilder
from deterministic_controller import DeterministicController


def obj(kind,x,y,w=.04,h=.04):
    return {'class':kind,'confidence':.95,'box':[x-w/2,y-h/2,x+w/2,y+h/2],'center':[x,y]}


def initial(*objects):
    return ChoicePreprocessor().update({'objects':[obj('watering_can',.5,.85,.16,.12),*objects]},10,row_mode=True)


def observed(base,dt,x=.5,missing=()):
    can={**base.can,'box':list(base.can['box']),'center':[x,.85]}
    dx=x-base.can['center'][0];can['box'][0]+=dx;can['box'][2]+=dx
    tracks=[replace(t,box=[t.box[0],t.box[1]+t.speed*dt,t.box[2],t.box[3]+t.speed*dt],
                    seen=base.timestamp+dt)for t in base.tracks if t.id not in missing]
    return replace(base,timestamp=base.timestamp+dt,can=can,tracks=tracks,mouth_box=mouth_box(can,PlannerConfig()))


class RollingTests(unittest.TestCase):
    def test_intervening_droplets_are_part_of_schedule_before_later_bomb(self):
        c=initial(obj('droplet',.5072,.74),obj('droplet',.65,.60),
                  obj('droplet',.3,.46),obj('bomb',.5072,.32))
        plans=ScheduleBuilder(PlannerConfig()).build_schedules(c)
        self.assertTrue(plans)
        self.assertEqual([(s.track_id,s.kind)for s in plans[0].steps],
                         [(1,'collect'),(2,'collect'),(3,'collect'),(4,'dodge')])
        hit,_=evaluate_trajectory(c,PlannerConfig(),plans[0].segments,plans[0].horizon)
        self.assertIsNone(hit)

    def test_bomb_clearance_precedes_return_to_same_column(self):
        c=initial(obj('bomb',.5072,.70),obj('droplet',.5072,.50))
        builder=ScheduleBuilder(PlannerConfig());plan=builder.build_schedules(c)[0]
        dodge,collect=plan.steps
        self.assertEqual((dodge.kind,collect.kind),('dodge','collect'))
        self.assertAlmostEqual(collect.command_at,10+builder.bomb_clearance(c.tracks[0],c)+.005)
        self.assertLess(collect.command_at,10+builder.window(c.tracks[0],c)[1])
        self.assertIsNone(evaluate_trajectory(c,PlannerConfig(),plan.segments,plan.horizon)[0])
        self.assertGreater(abs(dodge.target_x-.5),.05)
        self.assertAlmostEqual(collect.target_x,.5)

    def test_unpadded_clearance_recovers_projected_detector_edge(self):
        c=initial(obj('bomb',.5072,.70))
        bomb=c.tracks[0]
        pad=.025
        expanded=replace(bomb,box=[bomb.box[0],bomb.box[1]-pad,
                                   bomb.box[2],bomb.box[3]+pad],timing_padding=pad)
        builder=ScheduleBuilder(PlannerConfig())
        expected=(c.mouth_box[3]-bomb.box[1])/bomb.speed
        self.assertAlmostEqual(builder.bomb_clearance(expanded,c),expected)
        self.assertAlmostEqual(builder.bomb_clearance(expanded,replace(c,timestamp=10.1)),expected-.1)
        self.assertGreater(builder.window(expanded,c)[1],expected)
        plans=builder.build_schedules(replace(c,tracks=[expanded]))
        self.assertTrue(plans)
        self.assertAlmostEqual(plans[0].steps[0].release_at,10+expected+.005)

    def test_earlier_release_does_not_allow_crossing_padded_bomb(self):
        c=initial(obj('bomb',.5072,.70))
        bomb=c.tracks[0];pad=.10
        expanded=replace(bomb,box=[bomb.box[0],bomb.box[1]-pad,
                                   bomb.box[2],bomb.box[3]+pad],timing_padding=pad)
        c=replace(c,tracks=[expanded])
        builder=ScheduleBuilder(PlannerConfig())
        release=builder.bomb_clearance(expanded,c)+.005
        # Staying to the left is safe, but an immediate return through the bomb
        # remains forbidden during the uncertainty window after nominal clearance.
        self.assertTrue(builder.safe(c,[(release,release+.2,.2,0.)],release+.2))
        movement,end=builder.movement(.2,.5,release)
        self.assertFalse(builder.safe(c,movement,end))
        later=builder.window(expanded,c)[1]+.005
        movement,end=builder.movement(.2,.5,later)
        self.assertTrue(builder.safe(c,movement,end))

    def test_completed_catch_advances_to_next_drop_not_distant_bomb(self):
        c=initial(obj('droplet',.5072,.74),obj('droplet',.65,.60),
                  obj('droplet',.3,.46),obj('bomb',.5072,.32))
        planner=RollingPlanner(PlannerConfig());planner.update(c)
        planner.update(observed(c,.12))
        decision=planner.update(observed(c,.18))
        self.assertIn(1,planner.done)
        self.assertEqual(decision.id,'schedule_collect_2')
        self.assertGreater(decision.target_x,.6)

    def test_latency_prefix_is_shared_and_checked(self):
        c=initial(obj('droplet',.5072,.74),obj('droplet',.65,.60),
                  obj('droplet',.3,.46),obj('droplet',.7,.32),obj('droplet',.4,.18))
        planner=RollingPlanner(PlannerConfig());planner.update(c)
        plans=planner.builder.build_schedules(c,start_delay=.65,prefix=planner.active)
        self.assertTrue(plans)
        for plan in plans:
            for dt in [.05,.2,.4,.64]:
                self.assertAlmostEqual(plan.position_at(10+dt,.5),planner.active.position_at(10+dt,.5))
            self.assertIsNone(evaluate_trajectory(c,PlannerConfig(),plan.segments,plan.horizon)[0])

    def test_new_hazard_can_invalidate_jev_plan_without_executing_old_command(self):
        c=initial(obj('droplet',.7,.6),obj('droplet',.3,.4))
        p=RollingPlanner(PlannerConfig());p.update(c);p.active.source='jev'
        new=observed(c,.03)
        bomb=replace(new.tracks[0],id=99,kind='bomb',box=[.48,.79,.52,.83])
        d=p.update(replace(new,tracks=new.tracks+[bomb]))
        self.assertNotEqual(p.source,'jev')
        self.assertTrue(any(e['type']=='plan_invalidated'for e in p.events))
        self.assertTrue(d.id.startswith('safety_'))

    def test_missing_hazard_remains_in_path_checks(self):
        c=initial(obj('bomb',.5072,.70),obj('droplet',.5072,.50))
        p=RollingPlanner(PlannerConfig());p.update(c)
        d=p.update(observed(c,.2,.4156,missing=(1,)))
        self.assertIn(1,p.hazards)
        self.assertNotEqual(d.id,'schedule_collect_2')

    def test_all_expired_remote_objectives_are_not_adopted(self):
        c=initial(obj('droplet',.7,.6))
        p=RollingPlanner(PlannerConfig());p.update(c);old=p.active
        self.assertFalse(p.adopt(old,observed(c,3)))

    def test_ideal_closed_loop_collect_dodge_return(self):
        # A small known-physics fixture, not a prediction of real game score.
        controller=DeterministicController();controller.set_enabled(True)
        cfg=PlannerConfig();x=.5;command=None;caught=set();hits=[]
        objects=[('droplet',.5072,.74),('bomb',.5072,.55),
                 ('droplet',.5072,.35),('droplet',.75,.15)]
        for frame in range(130):
            t=frame*.02
            if command:
                source,target,begin,end,available=command
                if t>=end:x=target
                elif t>=begin:x=source+(target-source)*(t-begin)/(end-begin)
            can=obj('watering_can',x,.85,.16,.12)
            mouth=mouth_box(can,cfg)
            visible=[]
            for index,(kind,px,py) in enumerate(objects):
                if index in caught:continue
                current=obj(kind,px,py+.35*t)
                b=current['box']
                if b[3]>=mouth[1] and b[1]<=mouth[3]:
                    if kind=='droplet' and abs(px-(mouth[0]+mouth[2])/2)<=(mouth[2]-mouth[0])/2*cfg.catch_center_fraction:
                        caught.add(index);continue
                    if kind=='bomb' and b[2]>=mouth[0] and b[0]<=mouth[2]:hits.append(index)
                visible.append(current)
            ready=command is None or t>=command[4]
            controller.update({'objects':[can,*visible]},100+t,movement_ready=ready)
            d=controller.decision
            if ready and d and d.duration>0:
                begin=t+cfg.input_delay;end=begin+d.duration
                command=(x,d.target_x,begin,end,end+cfg.replan_allowance)
        self.assertEqual(caught,{0,2,3})
        self.assertEqual(hits,[])

    def test_default_deterministic_controller_uses_rolling_queue(self):
        controller=DeterministicController();controller.set_enabled(True)
        s={'objects':[obj('watering_can',.5,.85,.16,.12),obj('droplet',.5072,.74),
                      obj('droplet',.65,.6),obj('droplet',.3,.46),obj('bomb',.5,.32)]}
        controller.update(s,10)
        self.assertIsInstance(controller.sequence,RollingPlanner)
        self.assertEqual(controller.sequence.active.catches,3)
        controller.update(s,10.03,movement_ready=False)
        self.assertIsNone(controller.decision)
        controller.set_enabled(False)
        self.assertIsNone(controller.sequence.active)


if __name__=='__main__':unittest.main()
