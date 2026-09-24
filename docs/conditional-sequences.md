# Conditional deterministic sequences

Historical implementation after the 324 ml validation run. Its live trial
collected 228 ml and revealed excessively long escape commitments. It is now
superseded by [rolling schedules](rolling-schedules.md). The validation section
below records its original pre-trial state. The policy lives in `conditional_planner.py`, separate from detection,
Android control, and the Jev request builder.

## What a plan contains

A plan retains object IDs, two destinations, a phase, an escape/return timing
estimate, and an expiry time. Destinations are body-center coordinates adjusted
for mouth alignment. It is a short plan with repeated safety checks, not a
pre-recorded series of swipes executed regardless of the scene.

| Situation | Sequence | Condition for advancing |
| --- | --- | --- |
| Bomb(s) before a droplet | Dodge → wait → return to droplet | Every leading bomb in the plan must clear the mouth, including padding. |
| Droplet before a bomb | Approach/catch → escape | Reserved escape deadline, or observed mouth contact followed by droplet disappearance. |
| No relevant bomb sequence | Existing row policy | Continue selecting the next actionable droplet. |

The builder considers the first two visible remaining droplets and candidate
refuges from the existing geometry. It checks complete approach/wait/return or
catch/escape trajectories against all tracked bombs. Plans are bounded to twice
the configured horizon (two seconds by default). Where safe, the initial refuge
can already align with the following droplet; waiting does not require moving
away from an otherwise safe destination.

## Execution and replanning

The existing single-drag executor remains unchanged. The planner continues to
observe an existing plan while the executor is busy, but does not issue another
command or create a new plan during that drag.

On every observation, the next path is recalculated from the observed can
position. An unsafe path invalidates the plan. Missing can detections, pause,
and timestamp resets clear plan state. Expired plans and passed/lost return
targets trigger replanning. A bomb disappearing from detection does not mean it
has passed: the plan retains its last position/speed and extrapolates until
estimated clearance. This remains an estimate when visual confirmation is absent.

A catch-and-escape deadline is absolute once committed; repeatedly processing
frames does not push it into the future. If the can arrives late or misses the
droplet, it still abandons the catch at that deadline and tries to escape.
Predicted contact is not logged as a confirmed collection. The game's water
counter remains the outcome measurement.

When no complete safe sequence is available, the controller chooses a short
safe hold/dodge checked across the relevant hazard window, rather than lining
up beneath a subsequent bomb outside the old row-clearance window. If all
candidates are unsafe, the existing best-effort principle remains: delay the
earliest predicted collision. It cannot guarantee recovery from a late state.

## Geometry, timing, and logs

Sequence checks and the original candidate generator share
`evaluate_trajectory` in `choice_preprocessor.py`, so mouth geometry, margins,
and swept collision calculations are consistent. Existing estimated drag speed,
input delay, and replanning allowance are unchanged. Actual capture delay and
Android motion can still differ from those assumptions.

Deterministic `decisions.jsonl` now includes a `sequence` object containing the
plan, phase, reason, and retained hazard estimates. The selected immediate
candidate is included in the candidate list. The overlay displays the sequence
phase. Shadow replay logs also preserve this plan state.

## Verification and next test

57 offline tests pass, including bomb-before-droplet return, clearance of two
leading bombs, temporary hazard disappearance, catch/escape deadlines, delayed
approach, new hazards, busy execution, pause, expiry, and missing-can handling.
The geometry refactor also retains all existing Jev and deterministic tests.

Replaying the logged observations from `20260922-112020-980938` changes the
selection at 30.978 s and 31.211 s from `catch_84` to a retained escape destination
near body-center x=0.456. This demonstrates a different policy decision at the
reviewed failure, not that the alternative would have avoided the live hit.
The replay still feeds back the original can positions and command availability.

A new shadow video on the perfect human reference is under
`runs/replay/conditional-sequences-20260922/`. It is a visualization of proposals,
not a simulation of catches or score. It flagged 26 unsafe-proposal frames
versus 10 in the earlier row-only replay. The longer hazard window and different
retained plans make that count non-equivalent; it does not establish an
improvement. Conservative waiting remains a possible collection tradeoff.

Run the deterministic controller first:

```bash
uv run python testYolo.py --deterministic --control
```

Use J to start/pause and record the water total and bomb hits. Review plan phases
around failures before changing timing assumptions. Jev remains on its dynamic
single-move policy for now; it does not yet receive these sequences. Comparing
it directly to this controller would therefore compare different planning
capabilities. Sequence exposure to Jev is a separate next step after validation.
