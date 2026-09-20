import unittest
from choice_preprocessor import ChoicePreprocessor, Candidate, Choices, PlannerConfig, mouth_box
from deterministic_controller import select_candidate, DeterministicController


def obj(kind,x,y,w=.04,h=.04):
    return {'class':kind,'confidence':.9,'center':[x,y], 'box':[x-w/2,y-h/2,x+w/2,y+h/2]}


def state(*others):
    return {'objects':[obj('watering_can',.5,.85,.16,.12),*others]}


class PlannerTests(unittest.TestCase):
    def test_safe_catch_and_hold(self):
        planner=ChoicePreprocessor()
        choice=select_candidate(planner.update(state(obj('droplet',.65,.6)),1))
        self.assertIsNotNone(choice.catch_at)
        self.assertAlmostEqual(choice.target_x + .0072, .65)  # Mouth is right of body center.
        self.assertEqual(select_candidate(planner.update(state(),2)).id,'hold')

    def test_bomb_on_path_rejects_otherwise_good_catch(self):
        planner=ChoicePreprocessor()
        choices=planner.update(state(obj('droplet',.8,.6),obj('bomb',.65,.74)),1)
        catch=next(c for c in choices.candidates if c.id.startswith('catch'))
        self.assertFalse(catch.safe)
        self.assertNotEqual(select_candidate(choices).id,catch.id)

    def test_missing_can_and_disabled_can(self):
        planner=ChoicePreprocessor()
        self.assertIsNone(select_candidate(planner.update({'objects':[]},1)))
        s=state(obj('bomb',.5,.72));s['objects'][0]['class']='disabled_watering_can'
        choices=planner.update(s,2)
        self.assertFalse(next(c for c in choices.candidates if c.id=='hold').safe)

    def test_tracking_velocity_and_short_gap(self):
        planner=ChoicePreprocessor()
        planner.update(state(obj('bomb',.5,.5)),1)
        planner.update(state(obj('bomb',.5,.55)),1.1)
        self.assertEqual(len(planner.tracks),1)
        self.assertAlmostEqual(planner.tracks[0].speed,.5)
        planner.update(state(),1.2)
        self.assertEqual(len(planner.tracks),1)
        planner.update(state(),1.4)
        self.assertEqual(len(planner.tracks),0)

    def test_all_unsafe_delays_collision(self):
        choices=Choices(1,None,[Candidate('a',.4,.1,.1,.1,None),Candidate('b',.6,.1,.1,.3,None)])
        self.assertEqual(select_candidate(choices).id,'b')

    def test_bomb_below_mouth_can_overlap_body(self):
        choices = ChoicePreprocessor().update(state(obj('bomb', .5, .88)), 1)
        self.assertTrue(next(c for c in choices.candidates if c.id == 'hold').safe)

    def test_bomb_overlapping_mouth_is_hazard(self):
        choices = ChoicePreprocessor().update(state(obj('bomb', .5072, .81)), 1)
        self.assertFalse(next(c for c in choices.candidates if c.id == 'hold').safe)

    def test_droplet_past_mouth_is_not_a_catch(self):
        choices = ChoicePreprocessor().update(state(obj('droplet', .5, .88)), 1)
        self.assertTrue(all(c.catch_at is None for c in choices.candidates))

    def test_reference_mouth_geometry(self):
        can = {'box': [282.30, 1887.43, 747.89, 2218.44]}
        actual = mouth_box(can, PlannerConfig(mouth_bottom=.30))
        for got, expected in zip(actual, [436, 1908, 636, 1988]):
            self.assertAlmostEqual(got, expected, delta=3)

    def test_mouth_height_halved_with_top_fixed(self):
        can = state()['objects'][0]
        old = mouth_box(can, PlannerConfig(mouth_bottom=.30))
        new = mouth_box(can, PlannerConfig())
        self.assertEqual(old[:3], new[:3])
        self.assertAlmostEqual(new[3]-new[1], (old[3]-old[1])/2)

    def test_edge_overlap_does_not_count_as_catch(self):
        for width in (.04, .08):  # Both single and double sprites need their centers aligned.
            choices = ChoicePreprocessor().update(state(obj('droplet', .55, .68, width)), 1)
            hold = next(c for c in choices.candidates if c.id == 'hold')
            self.assertIsNone(hold.catch_at)
            selected = select_candidate(choices)
            self.assertIsNotNone(selected.catch_at)
            self.assertAlmostEqual(selected.target_x+.0072, .55)

    def test_short_move_is_corrected_from_observed_position(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .65, .68)), 1)
        # The phone moved partway, despite the prior command targeting .6428.
        s = {'objects': [obj('watering_can', .60, .85, .16, .12),
                         obj('droplet', .65, .715)]}
        policy.update(s, 1.1)
        self.assertGreater(policy.decision.duration, 0)
        self.assertAlmostEqual(policy.decision.target_x, .6428)

    def test_aligned_can_can_hold_for_droplet(self):
        choices = ChoicePreprocessor().update(state(obj('droplet', .5072, .68)), 1)
        self.assertEqual(select_candidate(choices).id, 'hold')

    def test_full_width_catch_not_capped(self):
        s = {'objects': [obj('watering_can', .12, .85, .16, .12),
                         obj('droplet', .87, .62)]}
        choice = select_candidate(ChoicePreprocessor().update(s, 1))
        self.assertGreater(choice.distance, .7)
        self.assertAlmostEqual(choice.target_x+.0072, .87)
        self.assertLess(choice.duration, .3)
        self.assertIsNotNone(choice.catch_at)

    def test_catch_escape_then_second_catch(self):
        choices = ChoicePreprocessor().update(state(
            obj('droplet', .65, .68), obj('bomb', .65, .48),
            obj('droplet', .3, .50)), 1)
        direct = next(c for c in choices.candidates if c.id == 'catch_1')
        self.assertFalse(direct.safe)  # Staying at the catch position is dangerous.
        choice = select_candidate(choices)
        self.assertTrue(choice.safe)
        self.assertEqual(choice.catch_count, 2)
        self.assertLess(choice.escape_target_x, choice.target_x)
        self.assertGreater(choice.escape_at, choice.catch_at)

    def test_too_close_bomb_rejects_catch_escape(self):
        choices = ChoicePreprocessor().update(state(
            obj('droplet', .65, .68), obj('bomb', .65, .67)), 1)
        self.assertFalse(any(c.safe and c.catch_track_id == 1 for c in choices.candidates))

    def test_escape_path_bomb_blocks_second_catch(self):
        choices = ChoicePreprocessor().update(state(
            obj('droplet', .65, .68), obj('bomb', .65, .48),
            obj('droplet', .3, .50), obj('bomb', .45, .60)), 1)
        self.assertFalse(any(c.id == 'catch_1_then_catch_3' for c in choices.candidates))

    def test_escape_deadline_does_not_slide_and_is_revalidated(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        s = state(obj('droplet', .65, .68), obj('bomb', .65, .48),
                  obj('droplet', .3, .50))
        policy.update(s, 1)
        pending = dict(policy.pending_escape)
        s_next = state(obj('droplet', .65, .6835), obj('bomb', .65, .4835),
                       obj('droplet', .3, .5035))
        policy.update(s_next, 1.01)
        self.assertEqual(policy.pending_escape['command_at'], pending['command_at'])
        # At the deadline, new evidence puts a bomb directly on the mouth:
        # the cached escape is no longer predicted safe and must be abandoned.
        s2 = {'objects': [obj('watering_can', .6428, .85, .16, .12),
                          obj('bomb', .65, .81)]}
        policy.update(s2, pending['command_at']+.01)
        self.assertIsNone(policy.pending_escape)
        self.assertFalse(policy.decision.safe)

    def test_pause_resets_tracks(self):
        policy=DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('bomb',.5,.5)),1)
        policy.set_enabled(False)
        self.assertIsNone(policy.decision)
        self.assertEqual(policy.preprocessor.tracks,[])

if __name__=='__main__':unittest.main()
