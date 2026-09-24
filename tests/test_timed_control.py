from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from choice_preprocessor import Choices, Track, PlannerConfig, evaluate_trajectory
from frame_observer import FrameObserver
from motion_estimator import MotionEstimator
from timed_executor import Action, Motion, TimedExecutor, displacement
from deterministic_runtime import DeterministicRuntime
from rolling_planner import Schedule, Step


class Clock:
    def __init__(self, now=10.): self.now = now
    def __call__(self): return self.now


def obj(kind, x, y, w=.04, h=.04):
    return dict(**{'class': kind}, confidence=.99, center=[x, y],
                box=[x-w/2, y-h/2, x+w/2, y+h/2])


def state(x=.5, dt=0., missing=()):
    rows = [('droplet', .51, .64), ('bomb', .51, .46),
            ('droplet', .51, .28), ('droplet', .73, .10)]
    return {'objects': [obj('watering_can', x, .85, .16, .12)] +
            [obj(k, px, py+.3*dt) for i, (k, px, py) in enumerate(rows) if i not in missing]}


def action(key='a', start=10., source=.5, target=.7):
    return Action(key, 1, 'collect', source, target, .85, start, start+.035, .1, start+.3)


class ObserverTests(unittest.TestCase):
    def test_decode_mailbox_deduplicates_and_keeps_latest(self):
        c = Clock(); observer = FrameObserver(c)
        observer.publish(1, 'old'); observer.publish(2, 'new')
        frame = observer.get(0)
        self.assertEqual((frame.sequence, frame.frame, frame.decoded_at), (2, 'new', 10.))
        self.assertIsNone(observer.get(0))
        observer.publish(2, 'duplicate'); observer.publish(1, 'backwards')
        self.assertIsNone(observer.get(0))
        observer.close(); observer.publish(3, 'closed')
        self.assertIsNone(observer.get(0))


class EstimatorTests(unittest.TestCase):
    def test_bomb_padding_is_recorded_separately_from_projected_edge(self):
        e = MotionEstimator(PlannerConfig(), decode_delay=.12, timing_uncertainty=.04)
        for sequence, now, missing in [(1, 10., ()), (2, 10.1, (1,))]:
            c = e.update(state(dt=now-10., missing=missing), sequence, now, now)
            bomb = next(t for t in c.tracks if t.kind == 'bomb')
            observed = e.cached[bomb.id]
            expected_top = observed.box[1]+bomb.speed*(now-observed.seen)
            self.assertGreater(bomb.timing_padding, 0.)
            self.assertAlmostEqual(bomb.box[1]+bomb.timing_padding, expected_top)
            self.assertTrue(all(t.timing_padding == 0. for t in c.tracks if t.kind == 'droplet'))

    def test_delayed_image_before_move_does_not_undo_the_move(self):
        estimator = MotionEstimator(PlannerConfig(), decode_delay=.2)
        motion = Motion(action(start=10.), 10., .08)
        # At host 10.22 this frame shows scene 10.02, before movement onset.
        choices = estimator.update(state(), 1, 10.22, 10.22, [motion])
        self.assertAlmostEqual(choices.can['center'][0], .7)
        self.assertAlmostEqual(estimator.snapshot['observed_can']['center'][0], .5)
        # A later image of the endpoint adds no movement a second time.
        choices = estimator.update(state(.7), 2, 10.4, 10.4, [motion])
        self.assertAlmostEqual(choices.can['center'][0], .7)

    def test_rejects_repeated_out_of_order_and_old_decodes(self):
        e = MotionEstimator(PlannerConfig())
        self.assertIsNotNone(e.update(state(), 2, 10., 10.))
        self.assertIsNone(e.update(state(), 2, 10.03, 10.03))
        self.assertIsNone(e.update(state(), 3, 9.9, 10.03))
        self.assertIsNone(e.update(state(), 3, 10.1, 10.4))
        self.assertIsNone(e.update(state(), 4, 11., 10.5))

    def test_missing_droplet_is_retained_briefly_and_speed_uses_window(self):
        e = MotionEstimator(PlannerConfig(), decode_delay=0.)
        for i in range(8):
            e.update(state(dt=i*.04), i, 10+i*.04, 10+i*.04)
        self.assertAlmostEqual(e.snapshot['speed'], .3, places=5)
        c = e.update(state(dt=.32, missing=(0,)), 8, 10.32, 10.32)
        self.assertIn(1, [t.id for t in c.tracks])
        self.assertGreater(next(t for t in e.snapshot['tracks'] if t['id'] == 1)['missing_for'], 0)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.executor = TimedExecutor(clock=self.clock)
        self.executor.set_enabled(True)

    def publish(self, actions, version=1, until=11., pose=(.5, 10.)):
        s = self.executor.snapshot()
        return self.executor.publish(actions, version=version, generation=s['generation'],
            revision=s['revision'], valid_until=until, pose=pose)

    def test_invalid_schedule_is_rejected_before_replacing_queue(self):
        valid = action()
        self.assertTrue(self.publish([valid]))
        for invalid in ([action('later', 10.3), valid],
                        [replace(valid, release_at=valid.start_at-.01)]):
            with self.assertRaises(ValueError):
                self.publish(invalid, version=2)
            self.assertEqual(self.executor.actions, (valid,))
            self.assertEqual(self.executor.version, 1)

    def test_deadline_execution_advances_without_an_observation(self):
        first = action()
        second = action('b', 10.3, .7, .3)
        self.publish([first, second])
        m = self.executor.poll(); self.assertEqual(m.action.id, 'a')
        self.clock.now = 10.19; self.executor.complete(m)
        self.assertIsNone(self.executor.poll())
        self.clock.now = 10.3
        self.assertEqual(self.executor.poll().action.id, 'b')

    def test_replacement_keeps_dispatched_action_and_removes_old_suffix(self):
        self.publish([action(), action('obsolete', 10.3, .7, .3)])
        first = self.executor.poll()
        self.publish([action(), action('new', 10.3, .7, .8)], version=2)
        self.assertIsNone(self.executor.poll())
        self.clock.now = 10.2; self.executor.complete(first)
        self.clock.now = 10.3
        self.assertEqual(self.executor.poll().action.id, 'new')
        ids = [e['action']['id'] for e in self.executor.drain_events() if e['type'] == 'dispatch']
        self.assertEqual(ids, ['a', 'new'])

    def test_late_schedule_expires_instead_of_bursting_actions(self):
        self.publish([action(), action('b', 10.3, .7, .3)])
        self.clock.now = 10.5
        self.assertIsNone(self.executor.poll())
        self.assertFalse(self.executor.snapshot()['has_schedule'])
        self.assertFalse(self.executor.history)

    def test_expiry_pause_and_generation_prevent_old_actions(self):
        old = self.executor.snapshot()
        self.publish([action()], until=10.02)
        self.clock.now = 10.03
        self.assertIsNone(self.executor.poll())
        self.executor.set_enabled(False); self.executor.set_enabled(True)
        self.assertFalse(self.executor.publish([action()], version=2,
            generation=old['generation'], revision=old['revision'], valid_until=11., pose=(.5, 10.)))
        self.assertFalse(self.executor.history)

    def test_dispatch_race_rejects_plan_based_on_previous_executor_state(self):
        self.publish([action()]); before = self.executor.snapshot()
        self.executor.poll()
        self.assertFalse(self.executor.publish([action('new')], version=2,
            generation=before['generation'], revision=before['revision'], valid_until=11., pose=(.5, 10.)))

    def test_transport_error_prevents_following_commands(self):
        self.publish([action(), action('b', 10.3, .7, .3)])
        m = self.executor.poll(); self.executor.complete(m, error=OSError('offline'))
        self.clock.now = 10.3
        self.assertIsNone(self.executor.poll())
        self.assertEqual(self.executor.snapshot()['error'], 'OSError')

    def test_cancelled_stream_does_not_predict_full_remaining_movement(self):
        self.publish([action()])
        m = self.executor.poll()
        self.clock.now = 10.05
        self.executor.complete(m, interrupted=True)
        self.assertAlmostEqual(displacement(self.executor.history, 10., 11.), .1)

    def test_observation_lease_expires_during_a_streamed_movement(self):
        self.publish([action()], until=10.10)
        self.executor.poll()
        self.assertFalse(self.executor.is_set())
        self.clock.now = 10.11
        self.assertTrue(self.executor.is_set())

    def test_real_worker_dispatches_without_observer_polling_and_stops_on_pause(self):
        from threading import Event
        import time
        class Transport:
            def __init__(self): self.started = Event(); self.finish = Event(); self.released = Event()
            def move(self, a, cancel):
                self.started.set()
                self.finish.wait(1)
                return True
            def release(self): self.released.set()
            def close(self): pass
        transport = Transport()
        executor = TimedExecutor(transport, threaded=True)
        try:
            executor.set_enabled(True)
            now = time.monotonic()
            first = replace(action(start=now+.05), latest_start=now+.3)
            snap = executor.snapshot()
            self.assertTrue(executor.publish([first], version=1, generation=snap['generation'],
                revision=snap['revision'], valid_until=now+1., pose=(.5, now)))
            self.assertTrue(transport.started.wait(1))
            executor.set_enabled(False)
            transport.finish.set()
            self.assertTrue(transport.released.wait(1))
            self.assertEqual(len(executor.history), 1)
        finally:
            transport.finish.set()
            executor.close()


class RuntimeTests(unittest.TestCase):
    def fallback_cases(self):
        path = Path(__file__).parent/'fixtures/interruptible_fallback_waits.json'
        return json.loads(path.read_text())['cases']

    def restore_fallback_case(self, case):
        data = case['choices']
        choices = Choices(data['timestamp'], data['can'], mouth_box=data['mouth_box'],
                          tracks=[Track(**t) for t in data['tracks']])
        clock = Clock(choices.timestamp); executor = TimedExecutor(clock=clock)
        runtime = DeterministicRuntime(executor, clock=clock)
        runtime.set_enabled(True)
        runtime.choices = choices
        runtime.active = Schedule(**{**case['plan'], 'steps': [Step(**s) for s in case['plan']['steps']]})
        runtime.actions = tuple(Action(**a) for a in case['actions'])
        runtime.done = set(case['done'])
        runtime.next_search = clock.now+10.  # New observations may interrupt a wait immediately.
        executor.consumed.update(case['consumed'])
        executor.history.extend(Motion(**{**m, 'action': Action(**m['action'])}) for m in case['history'])
        snap = executor.snapshot()
        executor.publish(runtime.actions, version=0, generation=snap['generation'],
                         revision=snap['revision'], valid_until=clock.now+.2,
                         pose=(choices.can['center'][0], clock.now))
        runtime.estimator.last_scroll = clock.now
        return runtime, executor, clock, choices

    def test_saved_fallback_waits_allow_earlier_safe_catches(self):
        for case in self.fallback_cases():
            with self.subTest(session_time=case['session_time']):
                runtime, executor, clock, choices = self.restore_fallback_case(case)
                old_release = next(a.release_at for a in runtime.actions if a.row < 0)
                self.assertTrue(runtime._valid(choices, clock.now))
                with patch.object(runtime.estimator, 'update', return_value=choices):
                    runtime.update({}, 1, clock.now)
                collect = next(s for s in runtime.active.steps
                               if s.track_id == case['target'] and s.kind == 'collect')
                self.assertLess(collect.command_at, old_release)
                self.assertFalse(any(a.row < 0 for a in runtime.actions))
                self.assertIsNone(evaluate_trajectory(choices, runtime.config,
                                    runtime.active.segments, runtime.active.horizon)[0])
                self.assertTrue(executor.snapshot()['has_schedule'])

    def test_fallback_movement_still_in_transport_is_not_interrupted(self):
        runtime, executor, clock, choices = self.restore_fallback_case(self.fallback_cases()[0])
        # Even past modeled arrival, a blocking transport call must finish first.
        executor.active = executor.history[-1]
        old_plan, old_actions = runtime.active, runtime.actions
        with patch.object(runtime.estimator, 'update', return_value=choices):
            runtime.update({}, 1, clock.now)
        self.assertIs(runtime.active, old_plan)
        self.assertEqual(runtime.actions, old_actions)
        self.assertIsNotNone(executor.active)
        self.assertIsNone(executor.poll())

    def test_valid_fallback_is_retained_when_no_replacement_route_exists(self):
        runtime, executor, clock, choices = self.restore_fallback_case(self.fallback_cases()[1])
        old_plan, old_actions = runtime.active, runtime.actions
        with patch.object(runtime.estimator, 'update', return_value=choices), \
             patch.object(runtime.builder, 'build', return_value=[]) as search:
            runtime.update({}, 1, clock.now)
        search.assert_called_once()
        self.assertIs(runtime.active, old_plan)
        self.assertEqual(runtime.actions, old_actions)
        self.assertTrue(executor.snapshot()['has_schedule'])

    def test_completed_fallback_still_respects_movement_turnaround(self):
        runtime, executor, clock, choices = self.restore_fallback_case(self.fallback_cases()[0])
        motion = executor.history[-1]
        # Transport returned and physical arrival was 40 ms ago, but the existing
        # 120 ms movement-end allowance has not elapsed yet.
        executor.history[-1] = replace(motion,
            started_at=clock.now-.04-motion.input_delay-motion.action.duration)
        old_plan = runtime.active
        with patch.object(runtime.estimator, 'update', return_value=choices), \
             patch.object(runtime.builder, 'build') as search:
            runtime.update({}, 1, clock.now)
        search.assert_not_called()
        self.assertIs(runtime.active, old_plan)

    def test_real_row_hold_is_not_treated_as_interruptible_fallback(self):
        case = self.fallback_cases()[1]
        fallback = next(a for a in case['actions'] if a['row'] < 0)
        old_row = fallback['row']; fallback['row'] = 1000
        for step in case['plan']['steps']:
            if step['track_id'] == old_row:
                step['track_id'] = 1000
        runtime, executor, clock, choices = self.restore_fallback_case(case)
        old_plan = runtime.active
        with patch.object(runtime.estimator, 'update', return_value=choices), \
             patch.object(runtime.builder, 'build') as search:
            runtime.update({}, 1, clock.now)
        search.assert_not_called()
        self.assertIs(runtime.active, old_plan)

    def test_control_remains_enabled_past_35_seconds_until_toggled_off(self):
        for elapsed in (35.1, 120.):
            with self.subTest(elapsed=elapsed):
                clock = Clock(); executor = TimedExecutor(clock=clock)
                runtime = DeterministicRuntime(executor, decode_delay=0.,
                    timing_uncertainty=0., clock=clock)
                runtime.set_enabled(True)
                clock.now += elapsed
                runtime.update(state(x=.35), 1, clock.now)
                clock.now += .04
                runtime.update(state(x=.35, dt=.04), 2, clock.now)
                self.assertIsNotNone(runtime.active, runtime.reason)
                self.assertTrue(executor.snapshot()['has_schedule'])
                self.assertIsNotNone(executor.poll())
                # The second J press uses this same toggle: clear pending input
                # and prevent fresh observations from starting another action.
                runtime.set_enabled(False)
                self.assertFalse(executor.snapshot()['has_schedule'])
                self.assertTrue(executor.is_set())
                clock.now += .04
                status = runtime.update(state(x=.35, dt=.08), 3, clock.now)
                self.assertIn('PAUSED', status)
                self.assertIsNone(runtime.active)
                self.assertIsNone(executor.poll())

    def test_saved_run_overlapping_bombs_never_rewind_action_deadlines(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/bomb_queue_order.json').read_text())
        data = fixture['choices']
        choices = Choices(data['timestamp'], data['can'], mouth_box=data['mouth_box'],
                          tracks=[Track(**t) for t in data['tracks']])
        clock = Clock(); executor = TimedExecutor(clock=clock)
        runtime = DeterministicRuntime(executor, clock=clock)
        runtime.set_enabled(True); runtime.choices = choices
        plans = runtime.builder.build(choices)
        self.assertTrue(plans)
        for plan in plans:
            with self.subTest(plan=plan.id):
                for step in plan.steps:
                    self.assertGreaterEqual(step.release_at, step.command_at)
                for start, stop, _, _ in plan.segments:
                    self.assertGreaterEqual(stop, start)
                self.assertIsNone(evaluate_trajectory(choices, runtime.config,
                                                      plan.segments, plan.horizon)[0])
                actions = runtime._actions(plan, (), clock.now)
                snap = executor.snapshot()
                self.assertTrue(executor.publish(actions, version=1,
                    generation=snap['generation'], revision=snap['revision'],
                    valid_until=clock.now+.2, pose=(choices.can['center'][0], clock.now)))

    def test_rejected_schedule_replans_next_frame_without_crashing(self):
        clock = Clock(); executor = TimedExecutor(clock=clock)
        runtime = DeterministicRuntime(executor, decode_delay=0., timing_uncertainty=0., clock=clock)
        runtime.set_enabled(True)
        runtime.update(state(), 1, clock.now)
        build_actions = runtime._actions
        def unordered(*args):
            return tuple(reversed(build_actions(*args)))
        clock.now += .04
        with patch.object(runtime, '_actions', side_effect=unordered):
            runtime.update(state(dt=.04), 2, clock.now)
        self.assertTrue(any(e['type'] == 'schedule_rejected' for e in runtime.events))
        self.assertIsNone(runtime.active)
        self.assertFalse(executor.snapshot()['has_schedule'])
        self.assertIsNone(executor.poll())
        clock.now += .04
        runtime.update(state(dt=.08), 3, clock.now)
        self.assertIsNotNone(runtime.active, runtime.reason)
        self.assertTrue(executor.snapshot()['has_schedule'])

    def test_live_runtime_preserves_deadlines_and_command_ids_between_frames(self):
        clock = Clock(); executor = TimedExecutor(clock=clock)
        runtime = DeterministicRuntime(executor, decode_delay=0., timing_uncertainty=0., clock=clock)
        runtime.set_enabled(True)
        runtime.update(state(), 1, clock.now)
        clock.now += .04
        runtime.update(state(dt=.04), 2, clock.now)
        self.assertIsNotNone(runtime.active, runtime.reason)
        old = runtime.active
        motion = executor.poll()
        clock.now += .04
        x = .5+displacement(executor.history, 10., clock.now)
        runtime.update(state(x, .08), 3, clock.now)
        self.assertIs(runtime.active, old)
        if motion:
            self.assertIn(motion.action.id, [a.id for a in runtime.actions])

    def test_missing_can_invalidates_schedule_and_pause_clears_it(self):
        clock = Clock(); executor = TimedExecutor(clock=clock)
        runtime = DeterministicRuntime(executor, decode_delay=0., clock=clock)
        runtime.set_enabled(True)
        runtime.update(state(), 1, clock.now)
        clock.now += .04; runtime.update(state(dt=.04), 2, clock.now)
        self.assertTrue(runtime.actions)
        clock.now += .04; runtime.update({'objects': []}, 3, clock.now)
        self.assertFalse(executor.snapshot()['has_schedule'])
        self.assertIsNone(executor.poll())
        runtime.set_enabled(False)
        self.assertIsNone(runtime.active)

    def test_ideal_physics_with_delayed_noisy_and_missing_observations(self):
        from simulateDeterministic import run_fixture
        for fps in (15, 20, 30):
            for delay in (.08, .2, .3):
                for hz in (5, 10):
                    with self.subTest(fps=fps, delay=delay, hz=hz):
                        r = run_fixture(fps=fps, delay=delay, jitter=.015,
                                        search_hz=hz, dropout_every=13)
                        self.assertEqual(r['caught'], [0, 2, 3, 4])
                        self.assertEqual(r['bomb_hits'], [])
                        self.assertLessEqual(r['dispatches'], 10)


class ScrcpyTransportTests(unittest.TestCase):
    def test_held_touch_order_clamping_and_release(self):
        import struct
        from scrcpy_drag import ScrcpyDragTransport
        class Connection:
            def __init__(self): self.packets = []; self.closed = False
            def send(self, p): self.packets.append(struct.unpack('>BBQiiHHHII', p))
            def disconnect(self): self.closed = True
        class Cancel:
            def is_set(self): return False
        clock = Clock(); connection = Connection()
        def sleep(dt): clock.now += dt
        transport = ScrcpyDragTransport(connection=connection, size=(1000, 2000), clock=clock, sleep=sleep)
        self.assertTrue(transport.move(action(), Cancel()))
        self.assertTrue(transport.move(action('b', source=.7, target=.3), Cancel()))
        transport.close()
        actions = [p[1] for p in connection.packets]
        self.assertEqual(actions.count(0), 1)  # one DOWN, continuous touch across moves
        self.assertEqual(actions[-1], 1)  # UP
        self.assertEqual(actions.count(1), 1)
        self.assertTrue(all(0 <= p[3] < 1000 and 0 <= p[4] < 2000 for p in connection.packets))
        self.assertTrue(connection.closed)

    def test_pause_interrupts_stream_and_releases_touch(self):
        import struct
        from scrcpy_drag import ScrcpyDragTransport
        class Connection:
            def __init__(self): self.actions = []
            def send(self, p): self.actions.append(struct.unpack('>BBQiiHHHII', p)[1])
            def disconnect(self): pass
        clock = Clock(); connection = Connection()
        class Cancel:
            def is_set(self): return clock.now >= 10.05
        def sleep(dt): clock.now += dt
        transport = ScrcpyDragTransport(connection=connection, size=(1000, 2000), clock=clock, sleep=sleep)
        self.assertFalse(transport.move(action(), Cancel()))
        self.assertEqual(connection.actions[-1], 1)
        self.assertFalse(transport.held)


class TimingAnalysisTests(unittest.TestCase):
    def test_combined_lag_fit_does_not_double_count_input_delay(self):
        from analyzeControlTiming import fit
        rows = []
        for index in range(240):
            stamp = 10+index*.025
            number = int((stamp-10)//1.)
            source, target = (.3, .7) if number % 2 == 0 else (.7, .3)
            motion = dict(started_at=10+number, source_x=source, target_x=target, duration=.2)
            scene = stamp-.195
            n = max(0, int((scene-10)//1.))
            sx, tx = (.3, .7) if n % 2 == 0 else (.7, .3)
            fraction = max(0., min(1., (scene-10-n)/.2))
            rows.append(dict(session_time=stamp-10, motion=motion,
                choices={'timestamp': stamp, 'can': {'class': 'watering_can', 'center': [sx+(tx-sx)*fraction, .85]}}))
        result = fit(rows)
        self.assertAlmostEqual(result['best']['combined_lag_seconds'], .195, places=3)
        self.assertAlmostEqual(result['suggested_feedback_delay'], .115, places=3)


if __name__ == '__main__':
    unittest.main()
