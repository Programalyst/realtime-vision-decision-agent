# Deterministic controller: strategy changes and policy design

Status as of 21 September 2026. The main change was replacing predictive
**catch-then-escape planning** with a policy focused on the **next incoming row**.
This was a change in strategy, not just a change in decision frequency.

The current implementation is an experimental alternative. Initial row-based
versions performed worse in live testing; the latest corrections have passed
offline tests but have not yet had a live test.

## What the comparison refers to

“Earlier, better-performing implementation” refers to the approach preceding the
row-based switch. In the `20260921-092451-330568` run, the user reported more
responsive movement, but the controller still hit two bombs. An earlier run
with full-width movement and catch-and-escape planning was reported to collect
more droplets while hitting one bomb.

Some intermediate changes were uncommitted. These descriptions identify stages
in the implementation, not exact reproducible Git checkpoints. Commit
`cc82516` contains the earlier scheduled-escape policy, but should not be assumed
to reproduce every detail of the later pre-switch run.

These were separate rounds without controlled spawn sequences or complete
collection totals. “Better-performing” describes the observed experience, not
a statistically established ranking. See [the testing log](gameplay-testing.md)
for the recorded observations.

## Implementation comparison

| Aspect | Earlier predictive approach | Current row-based approach |
| --- | --- | --- |
| Main objective | Select an early predicted-safe catch from candidate trajectories. | Handle the lowest actionable row: align with its droplet or avoid its bomb. |
| Planning ahead | Explicitly consider catch → escape and catch → next catch sequences. | Choose one destination at a time; inspect the following droplet when avoiding a bomb. |
| Escape timing | Keep a pending escape destination and deadline, rechecking safety before issuing it. | No explicit catch-then-escape schedule in the live policy. Replan as the scene changes. |
| Candidate ranking | Prefer safe catches, earliest catch time, then greater predicted catch count and shorter movement. | Prefer alignment with the active droplet; for a bomb, prefer safe clearance toward the next reachable droplet. |
| Safety window | Evaluate trajectories over a one-second horizon. | Evaluate through current-row clearance, capped at one second, then extend as needed to cover the entire drag plus replanning allowance. |
| Row priority | Catch opportunities compete according to predicted timing and safety. | Vertical position determines row order; independently estimated arrival times cannot reorder rows. |
| Falling speed | Independent velocity estimate for each track. | Retain independent measurements, but use a shared median measured speed for row-mode predictions, with a fallback when measurements are unavailable. |
| Missing or late droplets | No explicit reachability filter for row objectives; candidate evaluation determines predicted catches. | Missing or kinematically unreachable droplets cannot remain the active objective. Missing bombs retain a short tracking grace period. |
| Decision cadence | Replan each processed frame; execute one drag at a time. | Track each processed frame, suppress movement decisions during a drag, and resume with a post-completion observation. |
| Movement distance | Variable-distance destinations, including full-width moves. | Still variable-distance destinations. No return to fixed 8% steps. |
| Geometry | Mouth-only collision approximation and center-aligned collection. | Same basic geometry, plus can-width reconstruction when the detector box is clipped at an edge. |
| Diagnostics | Candidate decisions and gameplay recordings. | Active-row and command-timing logs, additional regression tests, and offline shadow replay. |
| Validation | Improved collection/responsiveness reported, with remaining misses and collisions. | Initial live regressions observed; final corrections have only offline validation so far. |

## What “policy” means here

A policy is the rule for choosing an action from the available information.
It does not have to be a neural network or a learned model. Both deterministic
versions use hand-written policies; YOLO supplies perception rather than the
movement decision.

The pipeline separates four responsibilities:

1. **Perception:** YOLO detects droplets, bombs, and the can.
2. **Preprocessing:** `choice_preprocessor.py` tracks objects, estimates motion,
   reconstructs the mouth, generates destinations, and predicts collisions.
3. **Policy:** `deterministic_controller.py` decides which candidate to select.
4. **Execution:** `DragController` in `jev_agent.py` translates the destination
   into an Android drag and manages command completion.

A good policy can still fail when detections, collision geometry, speed
estimates, or input timing are wrong. Passing a geometry test is not evidence
that the phone will execute a maneuver in time.

## Earlier policy: predictive catch and escape

The earlier policy compared direct moves and short movement sequences over a
one-second horizon. Its normal preference was a safe early catch. Among catches
with similar timing, it preferred more predicted collections and less travel.
Without a safe catch, it preferred a safe short movement or hold. If no candidate
was safe, it selected one predicted to delay collision longest.

For a droplet immediately followed by a bomb, it could select:

> Align with the droplet, wait for predicted collection, then move to an escape
> destination before the bomb reaches the mouth.

The controller retained the planned escape deadline and checked the destination
against new observations before executing it. The sequence generator remains
in the preprocessor for offline use, but current live row mode bypasses it.

This approach explicitly represents a maneuver the user performs well. Its
weakness is dependence on predicted timing: an incorrect catch time, delayed
command, or early target switch can invalidate the planned escape.

## Current policy: react to the next actionable row

The current policy exploits the game's one-object-per-row pattern. Its process
is:

1. Exclude objects already below the mouth. Exclude droplets that are missing
   from the current detection frame or cannot reach the catch band before
   falling past it under the motion estimate.
2. Select the remaining row with the lowest vertical center. A newly detected
   lower object can take priority immediately.
3. For a droplet, choose the predicted-safe candidate closest to center alignment
   with that droplet.
4. For a bomb, find safe candidates whose destinations clear it. Prefer the
   destination closest to alignment with the next reachable droplet. Without
   a following droplet, prefer the shortest safe clearance movement.
5. If those rules cannot select a candidate, use the fallback ranking. When all
   candidates are unsafe, this attempts to delay collision; it does not promise
   to avoid one.

“Safe” means safe under the current trajectory and hitbox estimates. Collision
checks include all tracked bombs, not just the active row, and now extend
through the full proposed drag plus the replanning allowance.

For a droplet followed by a bomb, the current policy aligns with the droplet
and later reacts to the bomb. It does **not** reserve a future dodge deadline.
That is the main capability difference from the earlier policy.

The current policy has some lookahead through candidate safety checks and the
next-droplet preference. However, it does not jointly optimize a sequence of
future actions. A one-step choice can be safe locally while leaving too little
time for the following maneuver.

## Event-driven objectives versus decision frequency

The row objective changes in response to events: a row passes, a droplet
vanishes, or a lower actionable object is detected. That does not mean the
controller should observe only once per row.

Tracking and collision evaluation run on processed frames. Only one Android
drag can execute at a time. New movement decisions wait until the command has
finished and a subsequent observation is available. This is an execution gate,
not a tick synchronized to the game's spawn interval.

The intermediate row implementation also waited for two sufficiently stable
post-drag observations. That added delay and has been removed. Stream timestamps
record frame retrieval rather than device capture, so buffered imagery can
still be older than it appears. ADB completion is also not proof that the can
reached the requested position.

## Regressions introduced and corrections made

| Problem during the transition | Correction now in place |
| --- | --- |
| A higher bomb appeared to arrive before a lower droplet because their speed estimates differed. | Order rows by vertical position and use a common falling-speed estimate for row predictions. |
| Two-observation settling requirement delayed subsequent moves. | Permit replanning on the first post-command observation containing the can. |
| Bomb handling preferred a minimal dodge or holding even when early alignment with the next droplet was safe. | Prefer a safe clearance destination aligned toward the following reachable droplet. |
| A caught or missed droplet could retain priority during the tracking grace period. | Exclude missing droplets from objectives; allow them to become objectives again if redetected and reachable. |
| A detected but already unreachable droplet delayed switching to the following one. | Apply a kinematic reachability check before selecting the active row. |
| Safety checking could stop when a row cleared, before the proposed drag finished. | Extend checking through drag completion and the replanning allowance. |

These corrections address specific failure modes. They do not establish that
the row policy outperforms the earlier sequence policy.

## How Jev fits into the policy layer

Jev is still using its original left/right/hold policy and fixed steps. It has
not yet been migrated to the deterministic candidate preprocessor.

A proposed integration would let Python calculate destinations such as “align
with next droplet,” “dodge left,” “dodge right,” and “hold,” including timing and
risk information. Jev would choose an action; Python would execute the precise
drag. Jev would not need to calculate pixels or movement percentages.

If preprocessing already leaves one clearly preferable action, this gives Jev
little substantive work and adds API latency. Its value would need to come from
better choices among meaningful tradeoffs or uncertainty. A fair comparison
would give both policies the same candidates, safety information, and execution
layer rather than comparing different movement capabilities.

## What offline replay establishes

`replayDeterministic.py` runs the current policy against cached detections from a
recording. The red rectangle marks the recorded human mouth; the cyan line
marks the proposed mouth-center destination. Every next frame still contains
the human's actual position, not the controller's hypothetical position.

The first perfect-run reference covered 915 frames from source time 5–35.5 s.
Ten frames across three short intervals had no predicted-safe choice. Those
flags are useful for investigating geometry and timing assumptions, especially
because the human successfully played the run. They are not ten bomb hits.

The replay cannot establish hypothetical score, catches, or collisions. It
also does not reproduce ADB blocking or capture latency. Forty-two offline tests
currently pass, but the latest live-policy corrections remain untested on the
phone. Further policy changes should preserve this distinction between a
plausible decision, a predicted trajectory, and a measured gameplay result.
