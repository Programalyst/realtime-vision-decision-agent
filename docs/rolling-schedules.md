# Rolling visible-row schedules

Historical shared-runner design. The latest live deterministic mode uses the
[timed executor](deterministic-execution.md); Jev and historical shadow replay
retain the runner described here. The route-search builder is still shared.

Implemented after the 22 September Jev run exposed a timing mismatch: 61 answers
returned, but only nine were accepted by the immediate-action integration.
This version is tested offline and has not been validated in a live game.

## Shared planner

`rolling_planner.py` builds ordered schedules across up to 12 visible rows within
an estimated four-second horizon. A bounded beam search retains up to eight
partial routes and returns up to four alternatives. It does not exhaustively
search every possible trajectory or guarantee an optimal score.

For each row it can collect or skip a droplet, or move clear of a bomb and wait
for its clearance. Each extension checks the complete path against all tracked
bombs. Droplet steps release the can after estimated contact and a small settling
allowance; bomb steps release after clearance. Travel, input delay, command
turnaround allowance, mouth geometry, and screen bounds are included. A bomb
at the far horizon may remain unresolved beyond that bounded prediction window.

Unlike the previous two-object sequence policy, a later bomb does not suppress
intervening droplet objectives. A route can be collect A → collect B → dodge C →
wait for C → collect D. Ranking favors predicted sprite collections, then shorter
travel; it does not estimate milliliters or distinguish single/double droplet value.

`DeterministicController` uses this planner directly. The former row/conditional
implementation remains as `ConditionalController` for historical tests, and
`conditional_planner.py` is no longer the default live policy.

## Retained intent, continuous execution checks

A schedule stores track IDs, collect/skip/dodge intents, destinations, and timing
estimates. The local runner rebuilds the remaining route from each fresh observed
can position. It advances after estimated observed mouth contact, object passage,
or disappearance of a droplet; these are not confirmed game-score increments.

Bombs missing from detection retain their last estimated position and velocity
until projected clearance. New hazards can invalidate a route even if they were
not present when it was selected. Missing can detections suppress control.
Pause/resume or a non-increasing observation timestamp invalidates local plan
state and pending remote generations. Only one ADB drag executes at a time.

A plan is not a queue of Android commands. At most the first current movement is
issued; subsequent movements wait for observations and executor availability.
Rebuilding adjusts timing, and may reject a route if the can falls behind. A
local replacement is then chosen, or a short best-effort safety move if no full
schedule is feasible. An already unavoidable collision cannot be guaranteed away.

## Jev chooses a future schedule

Jev receives a `Choice` question named `plan`, with dynamic keys such as `plan_1`.
Each criterion describes the full schedule: collect/skip/dodge steps, row IDs,
body and mouth destinations, relative command/contact/clearance/release times,
expected catches, travel, and checked horizon. Instructions define every field.
Requests are independent; no conversation history or prior model messages are
sent. The accepted route and tracking history are retained in Python.

Alternatives share a committed near-term prefix of the current route. The
initial response lead is **650 ms**; an upper-percentile estimate of recent API
latency plus 100 ms can increase it, capped at 1.5 s. If the cutoff falls within
a step, the prefix extends through that step rather than splicing a new route
into an unfinished action. Predicted catch/travel totals include that common
prefix. A request is sent only if an alternative includes a later collectible.

Current local execution continues while a request is in flight. Requests can
overlap an Android drag because they concern later actions. A reply received
during a drag is kept for activation when the executor is available. The minimum
request spacing remains 0.5 s. A two-second outer response-age limit is retained,
but increasing that limit alone is not the mechanism enabling delayed replies.

Once the lead time has elapsed and a fresh observation is available, the selected
remaining intentions are rebuilt against the latest scene. Passed rows may be
removed without rejecting the entire answer. The active row need not match the
request's original row. A remaining route that is unsafe, expired, or has no
remaining collectible is rejected; the existing local execution continues.
Accepted plans persist across multiple frames and movements without another API
answer. There is still at most one request in flight, no SDK retry of stale
state, and a five-second backoff after an API error.

## This is a hybrid controller

Jev does not own every move. Local startup planning prevents a frozen can while
waiting for the first answer. Local repairs and safety fallback prevent a rejected
answer from leaving the entire controller without an action. Logs distinguish:

- `jev`: executing a retained Jev-selected route, revalidated locally.
- `local_fallback`: locally selected route because no valid retained route exists.
- `local_safety`: short safety action because no full route was feasible.
- `deterministic`: route selected in deterministic mode.

`sequence.source` and `sequence.reason` describe current local execution.
`jev_events` records requests, replies queued for consideration, plan activations,
and local repairs. A queued reply is not an accepted/executed plan; check
`plan_activation.accepted`. Do not attribute fallback catches to Jev or present
this as a pure model-versus-rules benchmark.

## Running and offline verification

```bash
uv run python testYolo.py --deterministic --control
uv run python testYolo.py --jev --control --jev-lead 0.65 --jev-max-age 2
```

Both modes start paused. Press J on the game screen and pause again when play
ends. The key remains in ignored `.env`. Advice-only Jev still makes API requests
once enabled. No live API calls or phone gestures were performed to validate
this change.

63 tests pass across current and retained historical implementations. New tests
cover intervening droplets, bomb clearance before returning, shared latency
prefixes, retained execution during 417 ms replies, changed rows, replies arriving
during a drag, invalidation by new hazards, pause, stale observations, SDK request
serialization, and a small ideal-motion collect/dodge/return fixture. The fixture
is not a calibrated game simulator.

A repeatable timing-only replay is available:

```bash
uv run python replayJevTiming.py \
  runs/jev/20260922-131550-168356/decisions.jsonl \
  --latencies-from runs/jev/20260922-131550-168356/decisions.jsonl
```

It replaces Jev with a synthetic selector choosing the highest predicted catch
count, uses the recorded API delays, and feeds back recorded can positions and
drag availability. The initial run activated 17 of 30 returned schedules; two
replies were discarded before activation and 11 failed remaining-route
activation. It also logged many local repairs. This checks integration behavior,
not Jev quality or hypothetical live scores. Output is under
`runs/replay/jev-rolling-recorded-latency/`.

The perfect-human shadow replay is under `runs/replay/rolling-schedules-20260922/`.
It processed 915 frames and flagged ten unsafe-proposal frames. Neither replay
establishes that this controller will collect more water or avoid more bombs.
Physical drag/capture delay, inaccurate hitboxes, missed detections, and future
unseen spawns remain important limits.
