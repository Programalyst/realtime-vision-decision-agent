"""Jev route selection on the shared executor; all API responses are offline."""
from concurrent.futures import Future
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx2
from typesafe_sdk import TypeSafeClient, RetryPolicy
from choice_preprocessor import evaluate_trajectory
from deterministic_runtime import remaining_segments
from jev_timed_runtime import TimedJevRuntime
from timed_executor import TimedExecutor


class Clock:
    def __init__(self): self.now = 100.
    def __call__(self): return self.now


def obj(kind, x, y, w=.04, h=.04):
    return {'class': kind, 'confidence': .99, 'center': [x, y],
            'box': [x-w/2, y-h/2, x+w/2, y+h/2]}


def scene(t=0., x=.5):
    return {'objects': [obj('watering_can', x, .85, .16, .12)] +
            [obj('droplet', px, py+.35*t) for px, py in
             [(.5072, .74), (.65, .60), (.3, .46), (.7, .32), (.4, .18), (.6, .04)]]}


def answer(choice):
    return SimpleNamespace(choices={'plan': SimpleNamespace(choice=choice, confidence=.8)})


class NoNetwork:
    def system_one(self, **kwargs): raise AssertionError('Unexpected network request')
    def close(self): pass


class Harness:
    def __init__(self, test, **kwargs):
        self.clock = Clock()
        self.executor = TimedExecutor(clock=self.clock)
        self.agent = TimedJevRuntime(self.executor, client=NoNetwork(), clock=self.clock,
            decode_delay=0., timing_uncertainty=0., interval=10., **kwargs)
        test.addCleanup(self.agent.close)
        self.calls = []
        def submit(fn, state, question):
            future = Future(); future.set_running_or_notify_cancel()
            self.calls.append((future, state, question))
            return future
        self.patcher = patch.object(self.agent.api_executor, 'submit', side_effect=submit)
        self.patcher.start(); test.addCleanup(self.patcher.stop)
        self.sequence = 0
        self.x = .5

    def advance(self, t, *, observe=True, objects=None):
        target = 100+t
        while self.clock.now < target-1e-9:
            self.clock.now = min(target, self.clock.now+.005)
            if self.executor.history:
                motion = self.executor.history[-1]
                a = motion.action
                self.x = a.source_x+(a.target_x-a.source_x)*motion.fraction(self.clock.now)
                if self.executor.active and self.clock.now >= motion.ends_at:
                    self.executor.complete(motion)
            self.executor.poll()
        if observe:
            self.sequence += 1
            self.agent.update(scene(t, self.x) if objects is None else objects,
                              self.sequence, self.clock.now)
            self.executor.poll()

    def start(self):
        self.agent.set_enabled(True)
        self.advance(0.)
        self.advance(.04)

    def observe_until(self, t):
        while self.clock.now < 100+t-1e-9:
            self.advance(min(t, round(self.clock.now-100+.04, 6)))

    def reply(self, choice=None, finished=None):
        pending = self.agent.pending
        choice = choice or next(iter(pending['plans']))
        self.calls[-1][0].set_result((answer(choice), finished or self.clock.now))
        return choice


class TimedJevTests(unittest.TestCase):
    def test_paused_start_then_distinct_future_choices_on_shared_estimator(self):
        h = Harness(self)
        h.advance(0.)
        self.assertFalse(h.calls)
        h.start()
        self.assertTrue(h.calls)
        _, state, question = h.calls[0]
        self.assertGreaterEqual(len(question.criteria), 2)
        self.assertIn('response_lead_seconds', state)
        self.assertIn('timing_uncertainty_seconds', state)
        self.assertNotIn('messages', state)
        for criteria in question.criteria.values():
            self.assertTrue(criteria['predicted_safe'])
            self.assertGreater(criteria['expected_catches'], 0)
            self.assertTrue(all(s['release_in'] >= 0 for s in criteria['steps']))

    def test_delayed_response_adopts_rebased_route_and_keeps_executing(self):
        h = Harness(self); h.start()
        h.observe_until(.44)
        self.assertTrue(h.executor.history)  # Local input continues during the API call.
        selected = h.reply(finished=100.457)
        h.advance(.48)
        self.assertIsNotNone(h.agent.queued)
        self.assertTrue(next(e for e in h.agent.events if e['type'] == 'response')['queued'])
        h.observe_until(.76)
        self.assertEqual(h.agent.active.source, 'jev')
        self.assertEqual(h.agent.active.id, selected)
        self.assertTrue(any(a.source == 'jev' for a in h.agent.actions))
        self.assertIsNone(evaluate_trajectory(h.agent.choices, h.agent.config,
            remaining_segments(h.agent.active, h.clock.now),
            h.agent.active.created_at+h.agent.active.horizon-h.clock.now)[0])
        version = h.agent.version
        h.advance(.80)
        self.assertEqual(h.agent.active.source, 'jev')
        self.assertEqual(h.agent.version, version)

    def test_pause_resume_discards_old_response_without_overlapping_calls(self):
        h = Harness(self); h.start(); pending = h.agent.pending
        h.agent.set_enabled(False); h.agent.set_enabled(True)
        h.advance(.08); h.advance(.12)
        self.assertEqual(len(h.calls), 1)
        h.reply(next(iter(pending['plans'])), finished=100.13)
        h.advance(.16)
        response = next(e for e in h.agent.events if e['type'] == 'response')
        self.assertEqual(response['discard_reason'], 'pre-pause/reset response')
        self.assertIsNone(h.agent.queued)
        self.assertNotEqual(h.agent.active.source, 'jev')

    def test_invalid_choice_preserves_local_control_and_backs_off(self):
        h = Harness(self); h.start(); h.observe_until(.2)
        h.reply('invented', finished=100.2); h.advance(.24)
        self.assertTrue(any(e['type'] == 'error' for e in h.agent.events))
        self.assertIsNotNone(h.agent.active)
        self.assertIsNone(h.agent.queued)
        self.assertGreater(h.agent.next_request, 105.)

    def test_late_response_is_discarded(self):
        h = Harness(self); h.start(); h.observe_until(2.2)
        h.reply(finished=102.2); h.advance(2.24)
        response = next(e for e in h.agent.events if e['type'] == 'response')
        self.assertIn('age limit', response['discard_reason'])
        self.assertIsNone(h.agent.queued)

    def test_missing_can_and_stale_frame_cannot_activate_a_response(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                h = Harness(self); h.start(); h.reply(finished=100.05)
                if missing:
                    h.advance(.08, objects={'objects': []})
                else:
                    h.advance(.4, observe=False)
                    h.agent.update(scene(), h.sequence, 100.04)
                self.assertIsNone(h.agent.queued)
                self.assertFalse(next(e for e in h.agent.events if e['type'] == 'response')['queued'])

    def test_no_choices_means_no_api_request(self):
        h = Harness(self)
        with patch.object(h.agent, '_offer', wraps=h.agent._offer):
            h.agent.set_enabled(True); h.advance(0.)
            with patch.object(h.agent.builder, 'build_schedules', return_value=[]):
                h.advance(.04)
        self.assertFalse(h.calls)

    def test_selected_skip_is_retained_and_active_local_drag_is_preserved(self):
        h = Harness(self); h.start(); h.observe_until(.64)
        current = h.executor.active
        self.assertIsNotNone(current)
        # Deliberately choose a lower-scoring route, to verify local ranking does
        # not silently replace Jev's valid decision immediately after acceptance.
        selected = next(key for key, p in h.agent.pending['plans'].items()
                        if any(s.track_id == 4 and s.kind == 'skip' for s in p.steps))
        h.reply(selected, finished=100.64); h.advance(.68); h.advance(.70)
        self.assertEqual(h.agent.active.source, 'jev')
        self.assertEqual(h.executor.active, current)
        kept = next(a for a in h.agent.actions if a.id == current.action.id)
        self.assertEqual(kept, current.action)
        self.assertEqual(kept.source, 'deterministic_timed')
        self.assertTrue(any(s.track_id == 4 and s.kind == 'skip' for s in h.agent.active.steps))
        h.observe_until(1.12)
        self.assertEqual(h.agent.active.source, 'jev')
        self.assertFalse(any(m.action.row == 4 for m in h.executor.history))
        self.assertTrue(any(m.action.source == 'jev' for m in h.executor.history))

    def test_new_bomb_rejects_chosen_route_without_publishing_it(self):
        h = Harness(self); h.start(); chosen = next(iter(h.agent.pending['plans'].values()))
        h.observe_until(.64)
        choices = h.agent.choices
        droplet = next(t for t in choices.tracks if t.id == 4)
        bomb = replace(droplet, id=999, kind='bomb')
        h.agent.choices = replace(choices, tracks=choices.tracks+[bomb])
        old_plan, old_actions = h.agent.active, h.executor.actions
        accepted, reason = h.agent.adopt_schedule(chosen, h.clock.now)
        self.assertFalse(accepted)
        self.assertIn('infeasible', reason)
        self.assertIs(h.agent.active, old_plan)
        self.assertEqual(h.executor.actions, old_actions)

    def test_adoption_race_leaves_local_plan_intact(self):
        h = Harness(self); h.start(); chosen = next(iter(h.agent.pending['plans'].values()))
        h.observe_until(.64)
        old_plan, old_actions = h.agent.active, h.executor.actions
        with patch.object(h.executor, 'publish', return_value=False):
            accepted, reason = h.agent.adopt_schedule(chosen, h.clock.now)
        self.assertFalse(accepted)
        self.assertEqual(reason, 'executor changed during planning')
        self.assertIs(h.agent.active, old_plan)
        self.assertEqual(h.executor.actions, old_actions)

    def test_changed_destination_cannot_reuse_old_command(self):
        h = Harness(self); h.start()
        plan = h.agent.active
        step = next(s for s in plan.steps if s.track_id == 4)
        old_action = next(a for a in h.agent.actions if a.row == 4)
        changed = replace(plan, steps=[replace(s, target_x=s.target_x-.05) if s == step else s
                                       for s in plan.steps])
        actions = h.agent._actions(changed, h.agent.actions, h.clock.now, version=h.agent.version+1)
        new_action = next(a for a in actions if a.row == 4)
        self.assertNotEqual(new_action.id, old_action.id)
        self.assertAlmostEqual(new_action.target_x, old_action.target_x-.05)

    def test_hybrid_closed_loop_with_delayed_choices_and_observations(self):
        from simulateDeterministic import run_fixture
        instances = []
        class DelayedFuture(Future):
            def __init__(self, clock, due, choice):
                super().__init__(); self.clock, self.due, self.choice = clock, due, choice
                self.set_running_or_notify_cancel()
            def done(self):
                if not super().done() and self.clock() >= self.due:
                    self.set_result((answer(self.choice), self.due))
                return super().done()
        class DelayedPool:
            def __init__(self, clock, latency): self.clock, self.latency = clock, latency
            def submit(self, fn, state, question):
                selected = min(question.criteria, key=lambda k:
                    (-question.criteria[k]['expected_catches'], question.criteria[k]['travel']))
                return DelayedFuture(self.clock, self.clock()+self.latency, selected)
            def shutdown(self, **kwargs): pass
        def factory(executor, config, **kwargs):
            agent = TimedJevRuntime(executor, config, client=NoNetwork(), **kwargs)
            agent.api_executor.shutdown()
            agent.api_executor = DelayedPool(agent.clock, latency)
            self.addCleanup(agent.close); instances.append(agent)
            return agent
        accepted = 0
        for latency in (.15, .417, .9):
            for delay in (.08, .2, .3):
                with self.subTest(api_latency=latency, feedback_delay=delay), \
                     patch('simulateDeterministic.DeterministicRuntime', side_effect=factory):
                    result = run_fixture(delay=delay, jitter=.015, dropout_every=13)
                    self.assertEqual(result['bomb_hits'], [])
                    self.assertEqual(result['caught'], [0, 2, 3, 4])
                    accepted += sum(m.action.source == 'jev' for m in instances[-1].executor.history)
        self.assertGreater(accepted, 0)  # Exercise actual selections, not just local fallback.

    def test_sdk_serializes_choice_without_network(self):
        requests = []
        def respond(request):
            body = json.loads(request.content); requests.append(body)
            ids = body['questions']['plan']['criteria']; chosen = next(iter(ids))
            return httpx2.Response(200, json={'model': 'jev-latest', 'answers': {'plan': {
                'type': 'choice', 'choice': chosen, 'confidence': .8,
                'probabilities': {key: float(key == chosen) for key in ids}}},
                'usage': {'input_tokens': 100, 'output_tokens': 10}})
        clock = Clock(); executor = TimedExecutor(clock=clock)
        client = TypeSafeClient(api_key='offline-placeholder', transport=httpx2.MockTransport(respond),
                                retry=RetryPolicy(max_retries=0))
        agent = TimedJevRuntime(executor, client=client, clock=clock,
                               decode_delay=0., timing_uncertainty=0., control=False)
        self.addCleanup(agent.close)
        agent.set_enabled(True); agent.update(scene(), 1, clock.now)
        clock.now += .04; agent.update(scene(.04), 2, clock.now)
        self.assertIsNotNone(agent.future)
        agent.future.result(timeout=2.)
        clock.now += .04; agent.update(scene(.08), 3, clock.now)
        self.assertIsNotNone(agent.queued)
        self.assertFalse(executor.snapshot()['has_schedule'])
        self.assertFalse(executor.history)
        self.assertIn('mouth_target_x', next(iter(requests[0]['questions']['plan']['criteria'].values()))['steps'][0])
        self.assertNotIn('messages', requests[0])


if __name__ == '__main__': unittest.main()
