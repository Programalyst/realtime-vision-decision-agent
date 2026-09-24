"""Conditional plan transitions, using explicit observed motion rather than a game-score simulation."""
from dataclasses import replace
import unittest
from choice_preprocessor import ChoicePreprocessor, PlannerConfig, mouth_box
from conditional_planner import ConditionalPlanner
from deterministic_controller import ConditionalController as DeterministicController


def obj(kind,x,y,w=.04,h=.04):
    return {'class':kind,'confidence':.95,'box':[x-w/2,y-h/2,x+w/2,y+h/2],'center':[x,y]}


def initial(*objects):
    return ChoicePreprocessor().update({'objects':[obj('watering_can',.5,.85,.16,.12),*objects]},10,row_mode=True)


def observed(base, dt, x, missing=()):
    can = {**base.can,'box':list(base.can['box']),'center':list(base.can['center'])}
    dx=x-can['center'][0]
    can['center'][0]=x
    can['box'][0]+=dx;can['box'][2]+=dx
    tracks=[replace(t,box=[t.box[0],t.box[1]+t.speed*dt,t.box[2],t.box[3]+t.speed*dt],
                    seen=base.timestamp+dt) for t in base.tracks if t.id not in missing]
    return replace(base,timestamp=base.timestamp+dt,can=can,tracks=tracks,mouth_box=mouth_box(can,PlannerConfig()))


class SequenceTests(unittest.TestCase):
    def planner(self):
        return ConditionalPlanner(PlannerConfig())

    def test_bomb_then_drop_waits_until_clear_before_return(self):
        base=initial(obj('bomb',.5072,.72),obj('droplet',.5072,.60))
        p=self.planner();first=p.update(base)
        self.assertEqual(p.plan.kind,'dodge_wait_return')
        self.assertGreater(abs(first.target_x-.5),.06)
        refuge=first.target_x
        wait=p.update(observed(base,.20,refuge))
        self.assertEqual(p.plan.phase,'wait')
        self.assertAlmostEqual(wait.target_x,refuge)
        returning=p.update(observed(base,.35,refuge))
        self.assertEqual(p.plan.phase,'return')
        self.assertAlmostEqual(returning.target_x,.5)
        self.assertTrue(returning.safe)

    def test_two_leading_bombs_must_both_clear(self):
        base=initial(obj('bomb',.5072,.72),obj('bomb',.5072,.64),obj('droplet',.5072,.49))
        p=self.planner();first=p.update(base)
        self.assertEqual(len(p.plan.bomb_ids),2)
        refuge=first.target_x
        p.update(observed(base,.40,refuge))
        self.assertEqual(p.plan.phase,'wait')
        p.update(observed(base,.60,refuge))
        self.assertEqual(p.plan.phase,'return')

    def test_missing_bomb_is_not_treated_as_cleared(self):
        base=initial(obj('bomb',.5072,.72),obj('droplet',.5072,.60))
        p=self.planner();first=p.update(base)
        p.update(observed(base,.20,first.target_x,missing=(1,)))
        self.assertEqual(p.plan.phase,'wait')
        p.update(observed(base,.35,first.target_x,missing=(1,)))
        self.assertEqual(p.plan.phase,'return')

    def test_catch_then_escape_deadline_is_not_postponed_each_frame(self):
        base=initial(obj('droplet',.65,.70),obj('bomb',.65,.47))
        p=self.planner();first=p.update(base)
        self.assertEqual(p.plan.kind,'catch_escape')
        deadline=p.plan.switch_at
        p.update(observed(base,.10,first.target_x))
        self.assertEqual(p.plan.switch_at,deadline)
        p.update(observed(base,deadline-base.timestamp+.01,first.target_x))
        self.assertEqual(p.plan.phase,'escape')
        self.assertGreater(abs(p.plan.second_target-first.target_x),.05)

    def test_new_bomb_invalidates_committed_path(self):
        base=initial(obj('bomb',.5072,.72),obj('droplet',.5072,.60))
        p=self.planner();first=p.update(base)
        changed=observed(base,.1,first.target_x)
        blocker=replace(changed.tracks[0],id=99,box=[first.target_x-.02,.79,first.target_x+.02,.83])
        changed=replace(changed,tracks=changed.tracks+[blocker])
        self.assertIsNone(p.advance(changed))
        self.assertIsNone(p.plan)
        self.assertIn('unsafe',p.reason)

    def test_busy_cannot_create_plan_or_issue_move_and_pause_clears_plan(self):
        policy=DeterministicController();policy.set_enabled(True)
        state={'objects':[obj('watering_can',.5,.85,.16,.12),obj('bomb',.5072,.72),obj('droplet',.5072,.60)]}
        policy.update(state,10,movement_ready=False)
        self.assertIsNone(policy.sequence.plan)
        self.assertIsNone(policy.decision)
        policy.update(state,10.03)
        self.assertIsNotNone(policy.sequence.plan)
        policy.set_enabled(False)
        self.assertIsNone(policy.sequence.plan)
        self.assertEqual(policy.sequence.hazards,{})

    def test_late_approach_does_not_postpone_escape_to_chase_missed_drop(self):
        base=initial(obj('droplet',.65,.70),obj('bomb',.65,.47))
        p=self.planner();p.update(base)
        deadline=p.plan.switch_at
        escape=p.plan.second_target
        # Can has not reached the catch target when the reserved deadline arrives.
        result=p.update(observed(base,deadline-base.timestamp+.01,.5))
        self.assertEqual(p.plan.phase,'escape')
        self.assertAlmostEqual(result.target_x,escape)

    def test_plan_expires_and_missing_can_cancels(self):
        base=initial(obj('bomb',.5072,.72),obj('droplet',.5072,.60))
        p=self.planner();p.update(base)
        self.assertIsNone(p.advance(observed(base,3,.5)))
        self.assertIsNone(p.plan)
        p.update(base)
        p.update(replace(base,can=None,mouth_box=None))
        self.assertIsNone(p.plan)


if __name__=='__main__':unittest.main()
