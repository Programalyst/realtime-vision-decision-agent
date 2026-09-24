"""Jev chooses future schedules; Python continuously executes and repairs locally.

Requests are independent, but accepted plans persist locally across frames.
This is explicitly a hybrid controller; action source is logged every frame.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from collections import deque
import math
import os
import time

from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
from choice_preprocessor import ChoicePreprocessor
from rolling_planner import RollingPlanner

INSTRUCTIONS = (
    'Choose one supplied future movement schedule for a falling-object game. '
    'Maximize reachable droplet collections while avoiding bombs; favor less travel '
    'when expected collections are equal. All alternatives share a committed near-term '
    'prefix so Python can keep playing during this request. Evaluate the future suffix. '
    'Python executes and revalidates the plan against new observations; you select a plan ID. '
    'Steps visit visible rows in order: collect aims the mouth at that droplet, '
    'skip deliberately forgoes a droplet, dodge stays clear until that bomb passes. '
    'All times in the request are seconds relative to its observation, not wall-clock timestamps. '
    'command_in is earliest planned drag start; event_in is expected collection or bomb clearance; '
    'release_in is earliest start for the next step. target_x is the can BODY center destination, '
    'mouth_target_x is the corresponding MOUTH center. Coordinates are fractions of screen width, '
    'zero left and one right. expected_catches counts sprites, not milliliters; double droplets '
    'share a class and are not scored separately. travel is total planned screen-width distance. '
    'predicted_safe covers only the supplied horizon; all physics and catches are estimates. '
    'response_lead_seconds reserves time for API latency. Rows can pass during the request; '
    'Python will keep only a still-feasible remaining plan. No conversational history is sent.'
)


def request_payload(choices, schedules, lead, active):
    offset=sum(choices.mouth_box[::2])/2-choices.can['center'][0]
    now=choices.timestamp
    criteria={}
    for plan in schedules:
        criteria[plan.id]={
            'expected_catches':plan.catches,'travel':round(plan.travel,4),
            'predicted_safe':True,'horizon_seconds':round(plan.horizon,3),
            'steps':[{'row':s.track_id,'action':s.kind,'target_x':round(s.target_x,4),
                      'mouth_target_x':round(s.target_x+offset,4),
                      'command_in':round(s.command_at-now,3),'event_in':round(s.event_at-now,3),
                      'release_in':round(s.release_at-now,3)} for s in plan.steps]}
    return {'response_lead_seconds':lead,'current_plan_source':active.source if active else None,
            'rows':[{'id':t.id,'kind':t.kind,'x':round((t.box[0]+t.box[2])/2,4)} for t in choices.tracks],
            'coordinates':'Screen fractions; times relative to this observation.'},criteria


class JevAgent:
    def __init__(self, interval=.5, max_age=2., client=None, config=None, lead=.65):
        if not all(math.isfinite(v) and v>0 for v in (interval,max_age,lead)):
            raise ValueError('Jev timing parameters must be positive finite seconds')
        self.client=client or TypeSafeClient(
            api_key=os.environ.get('JEV_API_KEY') or os.environ.get('TYPESAFE_API_KEY'),
            model='jev-latest',timeout=3.,retry=RetryPolicy(max_retries=0))
        self.preprocessor=ChoicePreprocessor(config)
        self.sequence=RollingPlanner(self.preprocessor.config)
        self.executor=ThreadPoolExecutor(max_workers=1)
        self.interval,self.max_age,self.base_lead=interval,max_age,lead
        self.latencies=deque(maxlen=12)
        self.future=self.pending=self.queued=None
        self.next_request=0.
        self.enabled=True
        self.enabled_at=0.
        self.generation=0
        self.decision=self.choices=None
        self.active_track=None
        self.events=[]
        self.status='Jev: local startup'

    def set_enabled(self, enabled):
        self.enabled=enabled
        self.enabled_at=time.monotonic()
        self.generation+=1
        self.preprocessor.reset()
        self.sequence.reset()
        self.queued=None
        self.decision=None
        self.next_request=0.
        self.status='Jev: local startup' if enabled else 'Jev: PAUSED - press J to start'

    def _request(self, state, question):
        response=self.client.system_one(state=state,questions={'plan':question})
        return response,time.monotonic()

    @property
    def lead(self):
        # Reserve a recent upper-percentile latency plus one local-frame buffer.
        if not self.latencies:return self.base_lead
        values=sorted(self.latencies)
        return min(1.5,max(self.base_lead,values[min(len(values)-1,math.ceil(.9*len(values))-1)]+.1))

    def update(self, state, captured_at, movement_ready=True):
        now=time.monotonic()
        self.events=[]
        self.decision=None
        if self.preprocessor.last_time is not None and captured_at<=self.preprocessor.last_time:
            self.sequence.reset();self.queued=None;self.generation+=1
        self.choices=self.preprocessor.update(state,captured_at,row_mode=True)
        fresh=0<=now-captured_at<=.25
        if self.enabled and fresh:
            # This continues an existing route even while an API request runs.
            self.decision=self.sequence.update(self.choices,movement_ready)
            self.events.extend(self.sequence.events)
        if self.future is not None and self.future.done():
            pending=self.pending
            try:
                response,finished=self.future.result()
                answer=response.choices['plan']
                selected=answer.choice;confidence=float(answer.confidence)
                if selected not in pending['plans'] or not math.isfinite(confidence) or not 0<=confidence<=1:
                    raise ValueError('Invalid schedule answer')
                latency=finished-pending['sent_at']
                self.latencies.append(latency)
                reason=None
                if not self.enabled or pending['generation']!=self.generation:
                    reason='pre-pause/reset response'
                elif now-pending['observed_at']>self.max_age:
                    reason='response exceeded outer age limit'
                elif not fresh or not self.choices.can:
                    reason='no fresh can observation'
                if reason is None:
                    self.queued={'schedule':pending['plans'][selected],
                                 'activate_at':pending['observed_at']+pending['lead'],
                                 'expires_at':pending['observed_at']+self.max_age}
                self.events.append({'type':'response','choice':selected,'confidence':confidence,
                    'request_observed_at':pending['observed_at'],'latency_ms':round(latency*1000,1),
                    'observation_age_ms':round((now-pending['observed_at'])*1000,1),
                    'queued':reason is None,'discard_reason':reason})
            except Exception as error:
                self.events.append({'type':'error','error_type':type(error).__name__})
                self.next_request=now+5.
            self.future=self.pending=None
        if not self.enabled:
            self.status='Jev: PAUSED - press J to start'
            return self.status
        if self.queued and fresh and movement_ready and now>=self.queued['activate_at']:
            queued,self.queued=self.queued,None
            accepted=(now<=queued['expires_at'] and self.choices.can is not None
                      and self.sequence.adopt(queued['schedule'],self.choices,source='jev'))
            self.events.append({'type':'plan_activation','choice':queued['schedule'].id,
                                'accepted':accepted,'reason':None if accepted else 'expired or remaining route infeasible'})
            if accepted:
                self.decision=self.sequence.update(self.choices,movement_ready)
                self.events.extend(self.sequence.events)
        # Network requests may overlap a drag. They concern later rows, not a
        # command to execute before the current drag has finished.
        if (fresh and self.choices.can and self.sequence.active and self.future is None
                and self.queued is None and now>=self.next_request and captured_at>=self.enabled_at):
            lead=self.lead
            schedules=self.sequence.builder.build_schedules(self.choices,done=self.sequence.done,
                        start_delay=lead,prefix=self.sequence.active,limit=4)
            # Ask only about a future collectible, not solely a committed prefix.
            schedules=[s for s in schedules if any(t.kind=='collect' and t.command_at>=captured_at+lead
                                                   for t in s.steps)]
            if schedules:
                payload,criteria=request_payload(self.choices,schedules,lead,self.sequence.active)
                self.pending={'observed_at':captured_at,'sent_at':now,'generation':self.generation,
                              'lead':lead,'plans':{p.id:p for p in schedules}}
                self.future=self.executor.submit(self._request,payload,Choice(instructions=INSTRUCTIONS,criteria=criteria))
                self.next_request=now+self.interval
                self.events.append({'type':'request','observed_at':captured_at,'sent_at':now,
                                    'lead_seconds':lead,'state':payload,'criteria':criteria})
        self.active_track=next((s.track_id for s in self.sequence.active.steps if s.kind!='skip'
                                and s.track_id not in self.sequence.done),None) if self.sequence.active else None
        if self.decision:
            self.choices.candidates.append(self.decision)
        self.status='Jev hybrid: '+self.sequence.reason
        return self.status

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
        self.client.close()
