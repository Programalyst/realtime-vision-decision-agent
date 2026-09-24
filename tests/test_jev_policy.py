"""Latency-aware schedule selection; no network or phone required."""
from concurrent.futures import Future
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx2
from typesafe_sdk import TypeSafeClient, RetryPolicy
from jev_policy import JevAgent


def obj(kind,x,y,w=.04,h=.04):
    return {'class':kind,'confidence':.9,'box':[x-w/2,y-h/2,x+w/2,y+h/2],'center':[x,y]}


def scene(t=0.,x=.5):
    return {'objects':[obj('watering_can',x,.85,.16,.12),
        *[obj('droplet',px,py+.35*t) for px,py in
          [(.5072,.74),(.65,.60),(.3,.46),(.7,.32),(.4,.18),(.6,.04)]]]}


def answer(choice):
    return SimpleNamespace(choices={'plan':SimpleNamespace(choice=choice,confidence=.8)})


class FakeClient:
    def close(self):pass


class JevPolicyTests(unittest.TestCase):
    def agent(self):
        agent=JevAgent(client=FakeClient(),interval=10)
        self.addCleanup(agent.close)
        return agent

    def start(self, agent, busy=False):
        future=Future()
        with patch.object(agent.executor,'submit',return_value=future), patch('jev_policy.time.monotonic',return_value=100.):
            agent.update(scene(),100.,movement_ready=not busy)
        self.assertIsNotNone(agent.pending)
        route=agent.sequence.active
        return future,route

    def tick(self,agent,t,route,busy=False):
        x=route.position_at(100+t,.5)
        with patch('jev_policy.time.monotonic',return_value=100+t):
            agent.update(scene(t,x),100+t,movement_ready=not busy)

    def test_delayed_response_survives_row_changes_and_plan_persists(self):
        agent=self.agent();future,route=self.start(agent)
        selected=next(iter(agent.pending['plans']))
        # Continue observing/executing during a realistic 417 ms request.
        for i in range(1,11):self.tick(agent,i*.04,route)
        self.assertIsNotNone(agent.decision)
        future.set_result((answer(selected),100.417))
        self.tick(agent,.44,route)
        self.assertTrue(next(e for e in agent.events if e['type']=='response')['queued'])
        for i in range(12,19):self.tick(agent,i*.04,route)
        self.assertEqual(agent.sequence.active.source,'jev')
        self.assertIsNotNone(agent.decision)
        self.tick(agent,.76,route)
        self.assertEqual(agent.sequence.active.source,'jev')
        self.assertIsNotNone(agent.decision)  # No new API answer is needed for each move.

    def test_request_can_overlap_drag_but_no_command_is_issued(self):
        agent=self.agent();future,route=self.start(agent,busy=True)
        self.assertIsNone(agent.decision)
        self.assertIsNotNone(agent.pending)
        future.set_result((answer(next(iter(agent.pending['plans']))),100.4))
        for i in range(1,19):self.tick(agent,i*.04,route,busy=True)
        self.assertIsNotNone(agent.queued)
        self.assertIsNone(agent.decision)
        self.tick(agent,.76,route)
        self.assertEqual(agent.sequence.active.source,'jev')

    def test_pause_invalidates_delayed_response(self):
        agent=self.agent();future,route=self.start(agent)
        selected=next(iter(agent.pending['plans']))
        agent.set_enabled(False);agent.set_enabled(True)
        future.set_result((answer(selected),100.4))
        self.tick(agent,.44,route)
        self.assertIn('pre-pause',next(e for e in agent.events if e['type']=='response')['discard_reason'])
        self.assertIsNone(agent.queued)

    def test_outer_expiry_keeps_local_fallback_running(self):
        agent=self.agent();future,route=self.start(agent)
        selected=next(iter(agent.pending['plans']))
        for i in range(1,54):self.tick(agent,i*.04,route)
        future.set_result((answer(selected),102.2))
        self.tick(agent,2.24,route)
        event=next(e for e in agent.events if e['type']=='response')
        self.assertIn('age limit',event['discard_reason'])
        self.assertNotEqual(agent.sequence.source,'jev')

    def test_invalid_response_uses_safe_local_policy_and_backoff(self):
        agent=self.agent();future,route=self.start(agent)
        future.set_result((answer('invented_plan'),100.4))
        self.tick(agent,.44,route)
        self.assertEqual(next(e for e in agent.events if e['type']=='error')['error_type'],'ValueError')
        self.assertIsNotNone(agent.decision)
        self.assertGreater(agent.next_request,105)

    def test_missing_can_suppresses_motion_and_plan_activation(self):
        agent=self.agent();future,route=self.start(agent)
        future.set_result((answer(next(iter(agent.pending['plans']))),100.4))
        with patch('jev_policy.time.monotonic',return_value=100.44):
            agent.update({'objects':[]},100.44)
        self.assertIsNone(agent.decision)
        self.assertIsNone(agent.queued)

    def test_freshness_gate_blocks_execution_even_with_retained_plan(self):
        agent=self.agent();future,route=self.start(agent)
        with patch('jev_policy.time.monotonic',return_value=101):
            agent.update(scene(.04),100.04)
        self.assertIsNone(agent.decision)
        future.cancel()

    def test_real_sdk_serializes_schedule_choice(self):
        requests=[]
        def respond(request):
            body=json.loads(request.content);requests.append(body)
            ids=body['questions']['plan']['criteria'];chosen=next(iter(ids))
            return httpx2.Response(200,json={'model':'jev-latest','answers':{'plan':{
                'type':'choice','choice':chosen,'confidence':.8,
                'probabilities':{k:float(k==chosen)for k in ids}}},
                'usage':{'input_tokens':100,'output_tokens':10}})
        client=TypeSafeClient(api_key='offline-test-placeholder',transport=httpx2.MockTransport(respond),
                              retry=RetryPolicy(max_retries=0))
        agent=JevAgent(client=client,interval=10)
        self.addCleanup(agent.close)
        now=time.monotonic();agent.update(scene(),now);agent.future.result(timeout=1)
        agent.update(scene(.01),time.monotonic())
        self.assertIsNotNone(agent.queued)
        body=requests[0]
        self.assertGreater(body['state']['response_lead_seconds'],.5)
        plan=next(iter(body['questions']['plan']['criteria'].values()))
        self.assertGreater(len(plan['steps']),2)
        self.assertIn('command_in',plan['steps'][0])
        self.assertNotIn('messages',body)


if __name__=='__main__':unittest.main()
