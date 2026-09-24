import unittest
from choice_preprocessor import ChoicePreprocessor, Candidate, Choices, PlannerConfig, mouth_box
from deterministic_controller import select_candidate, ConditionalController as DeterministicController


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

    def test_row_target_persists_and_busy_does_not_issue_decisions(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .65, .68), obj('droplet', .3, .50)),1)
        row = policy.active_track
        target = policy.decision.target_x
        policy.update(state(obj('droplet', .65, .69), obj('droplet', .3, .51)),1.03,
                      movement_ready=False)
        self.assertEqual(policy.active_track,row)
        self.assertIsNone(policy.decision)
        policy.update(state(obj('droplet', .65, .70), obj('droplet', .3, .52)),1.06)
        self.assertEqual(policy.active_track,row)
        self.assertAlmostEqual(policy.decision.target_x,target)

    def test_new_faster_bomb_cannot_preempt_lower_droplet(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .70, .40)), 1)
        row = policy.active_track
        # Established drop speed .20; newborn bomb fallback .35 previously
        # put the higher bomb first and sent the can away from the droplet.
        policy.update(state(obj('droplet', .70, .42), obj('bomb', .70, .27)), 1.1)
        self.assertEqual(policy.active_track, row)
        self.assertAlmostEqual(policy.decision.target_x, .70-.0072)
        self.assertTrue(policy.decision.safe)
        self.assertAlmostEqual(policy.choices.tracks[0].speed,
                               policy.choices.tracks[1].speed)

    def test_reacquired_lower_drop_preempts_higher_row(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .30, .40)), 1)
        policy.update(state(obj('droplet', .30, .42), obj('droplet', .70, .65)), 1.1)
        active = next(t for t in policy.choices.tracks if t.id == policy.active_track)
        self.assertAlmostEqual((active.box[0]+active.box[2])/2, .70)
        self.assertAlmostEqual(policy.decision.target_x, .70-.0072)

    def test_bomb_dodge_toward_following_drop(self):
        for drop_x, direction in [(.2,-1),(.8,1)]:
            policy = DeterministicController()
            policy.set_enabled(True)
            policy.update(state(obj('bomb', .5072, .70),obj('droplet',drop_x,.50)),1)
            self.assertTrue(policy.decision.safe)
            self.assertGreater((policy.decision.target_x-.5)*direction,0)
            self.assertAlmostEqual(policy.decision.target_x, drop_x-.0072)

    def test_row_advances_after_object_passes(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .65, .76),obj('droplet',.3,.60)),1)
        row = policy.active_track
        policy.update(state(obj('droplet', .65, .86),obj('droplet',.3,.68)),1.2)
        self.assertNotEqual(policy.active_track,row)
        self.assertLess(policy.decision.target_x,.5)

    def test_leading_bomb_is_not_ignored_for_droplet(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('bomb', .5072,.70),obj('droplet',.5,.60)),1)
        track = next(t for t in policy.choices.tracks if t.id==policy.active_track)
        self.assertEqual(track.kind,'bomb')
        self.assertNotEqual(policy.decision.target_x,.5)

    def test_two_bombs_allow_early_alignment_with_next_double_drop(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        # Approximate screenshot 1 geometry: can and bombs right, double left.
        scene = {'objects': [obj('watering_can', .68, .89, .43, .15),
                 obj('bomb', .73, .72, .145, .075),
                 obj('bomb', .73, .61, .145, .075),
                 obj('droplet', .35, .46, .145, .065)]}
        policy.update(scene, 1)
        self.assertTrue(policy.decision.safe)
        self.assertAlmostEqual(policy.decision.target_x, .35-.43*.045)

    def test_missing_caught_drop_does_not_block_next_drop(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .5072, .79), obj('droplet', .25, .65)), 1)
        previous = policy.active_track
        policy.update(state(obj('droplet', .25, .66)), 1.03)
        self.assertNotEqual(policy.active_track, previous)
        self.assertAlmostEqual(policy.decision.target_x, .25-.0072)
        self.assertTrue(any(t.id == previous for t in policy.choices.tracks))

    def test_too_late_drop_does_not_block_following_double_drop(self):
        policy = DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('droplet', .85, .82),
                            obj('droplet', .25, .68, .08, .04)), 1)
        self.assertAlmostEqual(policy.decision.target_x, .25-.0072)

    def test_safety_extends_past_row_clearance_through_drag(self):
        choices = ChoicePreprocessor().update(state(
            obj('droplet', .5072, .80), obj('bomb', .70, .76)), 1, row_mode=True)
        right = next(c for c in choices.candidates if c.id == 'evade_right')
        self.assertFalse(right.safe)
        self.assertGreater(right.collision_at, .10)

    def test_edge_droplets_are_not_clamped_by_body(self):
        for drop_x in (.15, .85):
            s = {'objects': [obj('watering_can', .5, .85, .43, .14),
                             obj('droplet', drop_x, .60)]}
            choice = select_candidate(ChoicePreprocessor().update(s, 1))
            self.assertAlmostEqual(choice.target_x + .43*.045, drop_x)
            self.assertIsNotNone(choice.catch_at)

    def test_clipped_can_retains_mouth_geometry(self):
        planner = ChoicePreprocessor()
        planner.update({'objects':[obj('watering_can', .5, .85, .43, .14)]},1)
        # Body centered at .13 extends to -.085; the detector only sees x >= 0.
        clipped = {'class':'watering_can','confidence':.9,
                   'box':[0,.78,.345,.92], 'center':[.1725,.85]}
        choices = planner.update({'objects':[clipped]},1.1)
        self.assertAlmostEqual(choices.can['center'][0],.13)
        self.assertAlmostEqual((choices.mouth_box[0]+choices.mouth_box[2])/2,.14935)

    def test_pause_resets_tracks(self):
        policy=DeterministicController()
        policy.set_enabled(True)
        policy.update(state(obj('bomb',.5,.5)),1)
        policy.set_enabled(False)
        self.assertIsNone(policy.decision)
        self.assertEqual(policy.preprocessor.tracks,[])

if __name__=='__main__':unittest.main()
