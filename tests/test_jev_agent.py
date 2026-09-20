import time
import unittest
from types import SimpleNamespace
from jev_agent import JevAgent, DragController
import json
import httpx2
from typesafe_sdk import TypeSafeClient, RetryPolicy
from choice_preprocessor import Candidate


STATE = {'objects': [{'class': 'watering_can', 'confidence': .9,
                     'box': [.4, .8, .6, .9], 'center': [.5, .85]}]}


class FakeClient:
    def __init__(self):
        self.calls = 0
    def system_one(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(choices={'movement': SimpleNamespace(choice='right', confidence=.8)})
    def close(self):
        pass


class FakeDevice:
    def __init__(self):
        self.calls = []
    def window_size(self):
        return (1080, 2400)
    def swipe(self, *args):
        self.calls.append(args)


class IntegrationTests(unittest.TestCase):
    def test_preprocessed_target_and_stale_rejection(self):
        device = FakeDevice()
        control = DragController(device)
        try:
            candidate = Candidate('catch_1', .7, .2, .2, None, .4)
            now = time.monotonic()
            control.update_target(candidate, STATE, now-1)
            self.assertEqual(device.calls, [])
            control.update_target(candidate, STATE, now)
            control.future.result(timeout=1)
            self.assertEqual(device.calls[0][2], round(.7*1079))
            self.assertEqual(device.calls[0][4], .2)
        finally:
            control.close()

    def test_pause_suppresses_requests_and_discards_old_response(self):
        agent = JevAgent(client=FakeClient(), interval=10)
        try:
            agent.set_enabled(False)
            agent.update(STATE, time.monotonic())
            self.assertEqual(agent.client.calls, 0)
            agent.set_enabled(True)
            agent.update(STATE, time.monotonic())
            agent.future.result(timeout=1)
            agent.set_enabled(False)
            self.assertIsNone(agent.decision)
            agent.set_enabled(True)
            agent.update(STATE, time.monotonic())
            self.assertIsNone(agent.decision)
            self.assertIn('pre-pause', agent.status)
        finally:
            agent.close()

    def test_real_sdk_request_and_response(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={
                'model': 'jev-latest',
                'answers': {'movement': {'type': 'choice', 'choice': 'left',
                            'probabilities': {'left': .9, 'right': .05, 'hold': .05},
                            'confidence': .8}},
                'usage': {'input_tokens': 100, 'output_tokens': 10},
            })
        client = TypeSafeClient(api_key='offline-test-placeholder',
                               transport=httpx2.MockTransport(respond),
                               retry=RetryPolicy(max_retries=0))
        agent = JevAgent(client=client, interval=10)
        try:
            now = time.monotonic()
            agent.update(STATE, now)
            agent.future.result(timeout=1)
            agent.update(STATE, now)
            self.assertEqual(agent.decision[0], 'left')
            self.assertEqual(requests[0]['state'], STATE)
            self.assertEqual(requests[0]['questions']['movement']['type'], 'choice')
        finally:
            agent.close()

    def test_response_and_expiration(self):
        agent = JevAgent(client=FakeClient(), interval=10)
        try:
            now = time.monotonic()
            agent.update(STATE, now)
            agent.future.result(timeout=1)
            agent.update(STATE, now)
            self.assertEqual(agent.decision[0], 'right')
            agent.decision = ('right', .8, now - 10)
            agent.update(STATE, now)
            self.assertIsNone(agent.decision)
            self.assertEqual(agent.client.calls, 1)
        finally:
            agent.close()

    def test_stale_response_and_missing_can(self):
        agent = JevAgent(client=FakeClient(), interval=10)
        try:
            now = time.monotonic()
            agent.update(STATE, now)
            agent.future.result(timeout=1)
            agent.observed_at = now - 10
            agent.update(STATE, now)
            self.assertIsNone(agent.decision)
            agent.decision = ('right', .8, now)
            agent.update({'objects': []}, now)
            self.assertIsNone(agent.decision)
        finally:
            agent.close()

    def test_drag_mapping_once_and_stale(self):
        device = FakeDevice()
        control = DragController(device)
        try:
            now = time.monotonic()
            decision = ('right', .8, now)
            control.update(decision, STATE, now)
            control.future.result(timeout=1)
            control.update(decision, STATE, now)
            self.assertEqual(len(device.calls), 1)
            sx, sy, ex, ey, duration = device.calls[0]
            self.assertGreater(ex, sx)
            self.assertEqual(sy, ey)
            self.assertTrue(0 <= sx < ex < 1080)
            control.update(('left', .8, now-10), STATE, now)
            self.assertEqual(len(device.calls), 1)
        finally:
            control.close()


if __name__ == '__main__':
    unittest.main()
