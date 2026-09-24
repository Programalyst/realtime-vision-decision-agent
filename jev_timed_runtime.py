"""Independent Jev choices over future routes, using the shared timed runtime.

The API never controls touch input. Local observation, safety repair, and timed
execution continue while a request is pending. No conversation memory is sent.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
import os

from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
from deterministic_runtime import DeterministicRuntime
from jev_policy import INSTRUCTIONS, request_payload


TIMED_INSTRUCTIONS = INSTRUCTIONS + (
    ' The near-term prefix is a forecast; local safety repairs may change it while '
    'you respond. Python rebases your remaining intentions before activation. '
    'expected_catches excludes completed rows and travel counts only the future '
    'route. Do not infer observed collection success from these estimates.'
)


class TimedJevRuntime(DeterministicRuntime):
    def __init__(self, executor, config=None, *, client=None, interval=.5,
                 max_age=2., lead=.65, **kwargs):
        if not all(math.isfinite(v) and v > 0 for v in (interval, max_age, lead)):
            raise ValueError('Jev timing settings must be positive finite seconds')
        super().__init__(executor, config, **kwargs)
        self.client = client if client is not None else TypeSafeClient(
            api_key=os.environ.get('JEV_API_KEY') or os.environ.get('TYPESAFE_API_KEY'),
            model='jev-latest', timeout=3., retry=RetryPolicy(max_retries=0))
        self.api_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jev-choice')
        self.interval, self.max_age, self.base_lead = interval, max_age, lead
        self.latencies = deque(maxlen=12)
        self.future = self.pending = self.queued = None
        self.next_request = 0.
        self.policy_generation = 0
        self.request_number = 0

    def set_enabled(self, enabled):
        super().set_enabled(enabled)
        self.policy_generation += 1
        self.queued = None
        self.next_request = 0.
        # A running call cannot be cancelled. Keep it until completion so there
        # is never more than one in-flight request, even across a quick J toggle.
        if self.future is not None:
            self.future.cancel()

    @property
    def snapshot(self):
        return {**super().snapshot, 'mode': 'jev_assisted_timed',
                'jev': {'request_id': self.pending['id'] if self.pending else None,
                        'queued_choice': self.queued['schedule'].id if self.queued else None,
                        'lead_seconds': self.lead}}

    @property
    def lead(self):
        if not self.latencies:
            return self.base_lead
        values = sorted(self.latencies)
        latency = values[min(len(values)-1, math.ceil(.9*len(values))-1)]
        return max(self.base_lead, min(1.5, latency+.1))

    def _request(self, state, question):
        response = self.client.system_one(state=state, questions={'plan': question})
        return response, self.clock()

    def _response(self, now, fresh):
        if self.future is None or not self.future.done():
            return
        pending, future = self.pending, self.future
        self.pending = self.future = None
        if not self.enabled or pending['generation'] != self.policy_generation:
            # Do not let errors from an old session impose backoff on a new one.
            self.events.append(dict(type='response', request_id=pending['id'], queued=False,
                                    discard_reason='pre-pause/reset response'))
            return
        try:
            response, finished = future.result()
            answer = response.choices['plan']
            selected, confidence = answer.choice, float(answer.confidence)
            latency = finished-pending['sent_at']
            if (selected not in pending['plans'] or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1 or not math.isfinite(latency) or latency < 0):
                raise ValueError('Invalid Jev schedule response')
            self.latencies.append(latency)
            reason = ('response exceeded outer age limit' if now-pending['observed_at'] > self.max_age
                      else 'no fresh active route' if not fresh else None)
            if reason is None:
                self.queued = dict(schedule=pending['plans'][selected], request_id=pending['id'],
                    activate_at=pending['observed_at']+pending['lead'],
                    expires_at=pending['observed_at']+self.max_age)
            self.events.append(dict(type='response', request_id=pending['id'], choice=selected,
                confidence=confidence, latency_ms=round(latency*1000, 1),
                observation_age_ms=round((now-pending['observed_at'])*1000, 1),
                queued=reason is None, discard_reason=reason))
        except Exception as error:
            self.events.append(dict(type='error', request_id=pending['id'], error_type=type(error).__name__))
            self.next_request = now+5.

    def _offer(self, now):
        execution = self.executor.snapshot()
        cutoff = self._planning_commitment(execution, now,
                    self._can_replace_fallback_wait(execution, now))
        lead = max(self.lead, cutoff)
        self.next_request = now+self.interval
        if lead >= self.max_age:
            self.events.append(dict(type='request_skipped', reason='commitment exceeds response lifetime'))
            return
        plans = self.builder.build_schedules(self.choices, done=self.done, start_delay=lead,
                                   prefix=self.active, limit=4)
        alternatives = []
        seen = set()
        for plan in plans:
            future = [s for s in plan.steps if s.track_id not in self.done
                      and s.command_at >= now+lead-1e-6]
            signature = tuple((s.track_id, s.kind, round(s.target_x, 4)) for s in future)
            if not any(s.kind == 'collect' for s in future) or signature in seen:
                continue
            seen.add(signature)
            # Remove historical prefix rows from the payload and choice score.
            # Full remaining schedules, including the shared prefix, are retained.
            alternatives.append(replace(plan, steps=[s for s in plan.steps
                if s.track_id not in self.done and s.release_at >= now],
                travel=sum(abs(v)*max(0., b-max(0., a)) for a, b, _, v in plan.segments)))
        if len(alternatives) < 2:
            self.events.append(dict(type='request_skipped', reason='fewer than two distinct future routes'))
            return
        self.request_number += 1
        request_id = f'{self.policy_generation}:{self.request_number}'
        payload, criteria = request_payload(self.choices, alternatives, lead, self.active)
        payload['request_id'] = request_id
        payload['timing_uncertainty_seconds'] = self.estimator.timing_uncertainty
        payload['assumed_feedback_delay_seconds'] = self.estimator.decode_delay
        sent_at = self.clock()
        pending = dict(id=request_id, observed_at=now, sent_at=sent_at,
                       generation=self.policy_generation, lead=lead,
                       plans={p.id: p for p in alternatives})
        try:
            future = self.api_executor.submit(self._request, payload,
                                             Choice(instructions=TIMED_INSTRUCTIONS, criteria=criteria))
        except Exception as error:
            self.events.append(dict(type='error', request_id=request_id, error_type=type(error).__name__))
            self.next_request = now+5.
            return
        self.pending, self.future = pending, future
        self.events.append(dict(type='request', request_id=request_id, observed_at=now,
                                sent_at=sent_at, lead_seconds=lead, state=payload, criteria=criteria))

    def update(self, state, sequence, decoded_at, *, now=None):
        now = self.clock() if now is None else now
        super().update(state, sequence, decoded_at, now=now)
        fresh = (self.enabled and self.choices is not None and self.choices.can is not None
                 and self.choices.timestamp == now and self.estimator.sequence == sequence
                 and self.active is not None and self.estimator.last_scroll is not None
                 and now-self.estimator.last_scroll <= .7
                 and self.reason != 'duplicate, stale, or out-of-order observation'
                 and not self.executor.snapshot()['error'])
        self._response(now, fresh)
        if self.queued:
            queued = self.queued
            if now > queued['expires_at']:
                self.events.append(dict(type='plan_activation', request_id=queued['request_id'],
                    choice=queued['schedule'].id, accepted=False, reason='response expired before activation'))
                self.queued = None
            elif fresh and now >= queued['activate_at']:
                accepted, reason = self.adopt_schedule(queued['schedule'], now)
                self.events.append(dict(type='plan_activation', request_id=queued['request_id'],
                    choice=queued['schedule'].id, accepted=accepted, reason=reason))
                if accepted or reason != 'executor changed during planning':
                    self.queued = None
        if (fresh and self.future is None and self.queued is None and now >= self.next_request):
            self._offer(now)
        source = self.active.source if self.active else 'local'
        return f'Jev hybrid [{source}]: {self.reason}' if self.enabled else 'Jev: PAUSED - press J to start'

    def close(self):
        self.set_enabled(False)
        super().close()  # Stop input before waiting for any remaining network call.
        self.api_executor.shutdown(wait=True, cancel_futures=True)
        self.client.close()
