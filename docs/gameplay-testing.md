# Gameplay testing and results

The latest implementation is the [independent timed deterministic runtime](deterministic-execution.md).
Its predecessor recorded **342 ml** with one clear bomb hit around 28.7 s in
`runs/deterministic/20260922-142630-082328/annotated.mp4`. The new runtime has
82 passing offline tests and an 18-scenario synthetic timing sweep, but no live
result yet. An exploratory pre-hit fit found about 195 ms combined input/feedback
lag; the new defaults split this into an assumed 80 ms input delay and 120 ms
feedback delay. That split still needs device calibration. Current work focuses
on deterministic control; Jev's policy/executor have not been migrated.

Use this log for the next available daily attempts and for a subsequent public
results post. Preserve the distinction between measured scores, manual
observations, and behavior only covered by offline tests.

This is a chronological log: earlier sections describe the implementation at
that time. For the current controller and validation status, see
[rolling schedules](#rolling-schedules-and-latency-aware-jev-integration) below
and the [current design](rolling-schedules.md).

## Observations so far

These are user-reported observations from separate rounds. Exact water totals,
miss counts, and comparable spawn sequences have not been recorded here.

| Controller version | Bomb hits reported | Observation |
| --- | ---: | --- |
| Initial Jev snapshot policy | 3 | Basic left/right/hold decisions with fixed-distance drags |
| Initial deterministic planner | 1 | Better than the first Jev run, but missed some droplets |
| Mouth-only collision geometry | 0 | Avoided bombs in that round, but still missed droplets |
| Full-width moves and catch-and-escape planning | 1 | Collected more droplets; fewer misses than before |
| Shallower mouth + centered catches (current) | Not yet tested live | 27 offline tests pass; awaiting refreshed attempts |

The zero-hit run does not establish that the earlier controller was better:
rounds and controller settings differed, and collection totals are unavailable.

## Changes to assess next

The latest deterministic implementation:

- Keeps the mouth's top at 6% of the whole-can box height and moves its bottom
  from 30% to 18%, halving the mouth height.
- Uses a separate vertical bomb margin of 0.004 screen heights, retaining the
  horizontal margin of 0.015 screen widths.
- Counts a predicted catch only when the droplet center enters the central 25%
  of mouth width while the sprite vertically overlaps the mouth.
- Aims at droplet centers and replans from observed can positions to correct
  movements that stop short.

Full-width moves and catch-and-escape sequences remain enabled. A deliberately
scheduled **wait for a leading bomb to pass, then cross** maneuver is not yet an
explicit candidate type. Do not attribute that capability to this version.

## Next-session procedure

1. Record the commit being tested with `git rev-parse --short HEAD`, and note any
   uncommitted changes with `git status --short`. Confirm the model is v3.
2. Connect the phone, keep its orientation fixed, and open the game. Keep the
   model, confidence threshold, device, and capture setup consistent across runs.
3. Run the deterministic controller first:

   ```bash
   uv run python testYolo.py --deterministic --control
   ```

   Defaults are estimated drag speed 3 screen widths/second and input delay
   0.08 seconds. Record any overrides. These values are not measured physics.
4. Focus the YOLO preview and press **J** at the game screen. Check the red mouth
   rectangle and yellow center-alignment band. Play to the result screen, note
   the final water amount, then press **J** to stop and finalize the recording.
5. Save the printed session path. Deterministic outputs are
   `runs/deterministic/<timestamp>/annotated.mp4` and `decisions.jsonl`.
6. With another attempt available, run the existing Jev controller:

   ```bash
   uv run python testYolo.py --jev --control
   ```

   It reads `JEV_API_KEY` from ignored `.env`. Use J in the same way. Recordings
   are saved under `runs/jev/<timestamp>/annotated.mp4`; Jev mode now also writes
   `decisions.jsonl`, including API request/response events and discard reasons.
7. Review the videos before changing parameters. Mark the timestamps of bomb
   hits, missed droplets, delayed escapes, and movements that stop short.

Q or Ctrl+C also finalizes an active recording. No new API calls or drags are
submitted while paused; an already executing drag can finish. Recordings contain
processed annotated frames, not every phone frame, and have no audio.

## Results template

Copy one row per round; leave unknown values blank rather than estimating them.
A double-droplet sprite is one collectible, but its water value differs, so the
final in-game water total is the primary collection measure.

| Run / session path | Commit + local changes | Mode | Settings overrides | Final water (ml) | Bomb hits | Missed collectibles (manual) | Notes |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| | | Deterministic | None | | | | |
| | | Jev | None | | | | |

For each noteworthy event:

| Video time | What happened | Detection present? | Mouth/center alignment | Chosen action / observed movement | Hypothesis |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

The deterministic log contains proposed choices and predicted outcomes, not
confirmed Android commands or catches. It does not measure actual drag completion
times. Use the video to distinguish what was proposed from what visibly happened.
Model confidence and predicted safety are not game outcomes.

## Interpreting the comparison

Both modes share YOLO v3, but currently have different decision inputs and controls:

| | Deterministic | Current Jev |
| --- | --- | --- |
| Input | Tracked detections, motion estimates, candidate trajectories | One snapshot of normalized detections |
| Movement | Variable-distance targets and scheduled escape | Left/right/hold with 8%-width steps |
| Decision cadence | Each processed detection frame; one drag at a time | Minimum 0.5 seconds between requests; one request in flight |
| Collision geometry | Calibrated mouth approximation and safety margins | Snapshot policy instructions; no candidate-path collision calculation |

This is a comparison of complete pipelines, not evidence isolating the quality of
Jev's decision-making. To isolate that later, give Jev and the deterministic policy
the same candidate set and execution layer. Additional runs are needed because
spawn patterns and detection/control delays can vary.

## Preparing a public results post

Include the model version and dataset size (130 annotated images), the tested
code revision, per-round water totals and bomb hits, and short timestamped clips
showing both successful maneuvers and remaining misses. Describe the progression
from snapshot decisions to motion-aware planning and mouth-centered geometry.

Clearly label the number of trials and the differing controller inputs. Separate
YOLO validation metrics from live gameplay performance. Claim only observed
results; the current changes await live testing. Keep API keys out of screenshots
and shared notebook/code outputs. Videos and session logs remain local under the
Git-ignored `runs/` directory; publish selected artifacts separately if desired.

## Row-based refinement after the 20260921-092451-330568 run

Review found two bomb hits, around 12.5 s and 21.3 s. Decision-log rows arrived
at a median interval of about 39 ms; requested swipes had a median duration of
80 ms, while submission-to-observed-completion was about 227 ms. These logs
included polling and do not establish exact physical travel time.

The new policy retains the next incoming row: align for droplets; for bombs,
stay clear or dodge toward the next droplet. It replaces live multi-step escape
scheduling. Tracking continues during drags, but new decisions wait for command
completion and two fresh, sufficiently stable can-position observations. Logs
now capture busy/settling frames as well as proposals. `completed_at` for new
runs is measured when the ADB call returns, unlike older polling timestamps.

This version passed 33 offline tests but regressed in the next live run below.
The former rows above remain historical results, not results for this policy.


## Regression review: 20260921-094536-204705

The user reported more bomb hits and missed droplets. At 4.744 s, the log
selected bomb track 7 above droplet track 5. Independent velocity estimates
incorrectly let a higher row preempt the lower one. Footage around 5 s shows
the resulting premature dodge away from the droplet. Median command
submission-to-ADB-return time was about 191 ms; the additional two-observation
settling gate further delayed opportunities to correct or dodge.

Corrections order rows by vertical center, use a common median measured falling
speed for row-mode predictions, and allow replanning on the first observation
after ADB completion. Tracking and safety checks continue on every processed
frame. Reacquired lower objects take priority. Jev's policy remains unchanged.
Replaying the recorded detections at 4.744–5.131 s now selects droplet track 5
and its center-aligned target instead of bomb track 7. This validates that
selection change, not counterfactual gameplay outcomes.

**Next action:** test the corrected deterministic controller before changing Jev.
Device capture latency and physical drag arrival time remain unmeasured.


## Screenshot review: images/troubleshoot_1

Reviewed only the five supplied screenshots, not the recording. They show a
bomb collision and consecutive missed droplets. Still frames cannot establish
exact command latency, velocity, or the complete sequence of requested moves.
The cyan destination lines in screenshots 1–2 lie left of the detected can.

Code review exposed three additional policy defects: bomb handling preferred a
minimal dodge/hold over safe preparation for a following droplet; missing
caught/missed droplets could retain priority for the tracking TTL; safety checks
could end at row clearance while a drag was still in progress.

The controller now prefers safe alignment toward the next reachable droplet,
excludes missing and kinematically unreachable droplets from row objectives,
and checks collisions through the entire drag plus the replanning allowance.
Bombs retain their tracking grace period. A missing droplet can become an
objective again if redetected and reachable. Velocity and tick-rate settings
were not changed. Forty offline tests pass, including approximate screenshot-1
geometry, missing/unreachable droplet handoff, and a collision after the previous
safety cutoff. These are policy regressions, not a simulation of the actual run.
Live validation remains pending; Jev's policy is unchanged.


## Offline perfect-run reference

`replayDeterministic.py` replays the matching cached detections alongside the
human video, with no phone or Jev connection. Initial reference:
`runs/video/20260919-235017-767116/annotated-h264.mp4`, source 5–35.5 s.
Output: `runs/replay/perfect-run-reference/` (ignored by Git).

915 frames were processed, all with a detected can. Ten frames had no
predicted-safe choice, grouped at 8.541 s, 15.702–15.803 s, and
16.447–16.565 s. These are useful collision/latency calibration cases against
the user's successful run, not ten measured or simulated hits. The planner
assumes an initially stationary can during input delay; the human can may
already be moving. Detector boxes and conservative collision margins also need
checking before interpreting these flags as controller mistakes.

The shadow video displays actual human mouth geometry and proposed targets;
JSONL captures the full candidate states, and CSV lists row/safety transitions.
The replay does not feed proposed positions back into gameplay, and cannot
measure the controller's hypothetical score or verify ADB responsiveness.
Forty-two tests pass, including missing-frame timestamp interpolation and
rejection of invalid timestamp order. No live policy tuning was done from this
reference run alone.


## Dynamic Jev integration (implemented after offline reference review)

Jev now receives the shared row-mode candidate geometry and uses the same target
drag executor. The old fixed-step Jev descriptions above are historical.
Requests remain independent: no conversation history is sent. Python tracking
supplies estimated motion in each request. The API selects one dynamic ID;
Python rechecks its current path and rejects stale, changed-row, or unsafe
answers without a deterministic fallback. Default request spacing and maximum
request-observation age are both 0.5 s and can be configured separately.

Before interpreting a live result, inspect `jev_events` in `decisions.jsonl`:
request latency, accepted answers, and discard reasons may explain missed moves.
Run deterministic validation first, then Jev. Do not attribute old fixed-step
results to this integration. SDK serialization and lifecycle behavior are
covered offline; no live API call or phone run was made during implementation.

## Live deterministic validation: 20260922-112020-980938

User-reported result: **324 ml, two bomb hits**, with improved performance.
The user estimates a perfect run always yields at least 420 ml; this round is
therefore at least 96 ml below that reference, not a measured count of missed
collectibles. Spawn randomness prevents a direct score comparison across rounds.
Reviewed the annotated video and its matching decision log. Controller code and
settings were not changed during this review, preserving the Jev comparison.

- The log contains 928 observations, with a median spacing of about 38.8 ms.
  Across completed logged commands, median submission-to-ADB-return time was
  about 186 ms. This measures command turnaround, not exact physical movement
  or capture latency.
- At 30.978 s and 31.211 s, the active row was bomb 82, but the selected candidate
  was `catch_84`: align with droplet 84, behind intervening bomb 83. The candidate
  was marked safe within its evaluation window. At 31.447 s the next bomb was
  predicted to collide in about 84 ms and the fallback selected `hold`; it
  continued holding as collision became immediate. An explosion is visible
  around 31.7 s, followed by disabled-can detections. This supports premature
  alignment toward a droplet behind a bomb and insufficient preparation for the
  following hazard, rather than simply a low observation frequency.
- A second explosion is visible around 35.0 s, after the “TIME'S UP” overlay is
  already displayed. The score remains 324 ml. Distinguish this post-timeout
  visible hit from the first hit during the collection window in public results.
- Some proposals have tight timing: at roughly 7.14 s, `catch_9` predicts contact
  in about 150 ms. The measured command turnaround suggests little timing slack,
  but does not by itself prove when the can physically reached that target.
- Collision-box overlaps near 29.6 s and 33.6 s do not alone establish additional
  bomb hits. Do not count geometric overlaps or repeated disabled detections as
  separate collisions.

Working hypotheses remain: the short row-based safety window can approve early
alignment beneath a subsequent bomb; estimated input/travel timing may be too
optimistic; close droplet/bomb pairs may require explicit catch-then-escape
planning. Do not treat those hypotheses as calibrated physics. Next step is the
planned dynamic Jev run with unchanged shared movement settings, inspecting API
latency and response rejections alongside actual gameplay.


## Conditional sequence implementation after the 22 September review

Before spending an attempt on Jev, the user requested conditional sequence
planning in deterministic control. Implemented in `conditional_planner.py`:
dodge/wait/return for leading bombs, catch/escape for following bombs, retained
phases and hazard estimates, fixed escape deadlines, and full-path revalidation.
See [sequence design and validation](conditional-sequences.md). 57 offline tests
pass. A logged-observation replay changes the reviewed pre-hit `catch_84` choices
to an escape destination; it does not establish counterfactual live outcomes.

**Next:** test this deterministic version first. Jev still receives dynamic
single-move choices and has not been upgraded to sequence choices. Input timing
and drag-speed assumptions are unchanged; the new live result remains pending.

## First conditional-sequence live run: 20260922-121715-284925

Reviewed the annotated footage and decision log without modifying controller
code. The result screen at about 35.9 s confirms **228 ml**, compared with 324 ml
in the preceding row-policy run. The user described this run as seeming better;
movement can look calmer, but this trial collected 96 ml less. Spawn randomness
still prevents attributing the entire score difference to the code change.

One visible in-play explosion occurs around 25.6–26.1 s. Disabled-can detections
span 25.95–27.65 s and briefly 28.42–28.57 s; the later labels do not establish a
second hit. The previous run had one confirmed in-play explosion and another
after TIME'S UP, so these recordings do not establish improved in-play bomb
avoidance from the sequence policy.

| Source time | Observation | Interpretation |
| --- | --- | --- |
| 3.47–5.32 s | Controller waits for the leading bombs; a return command is logged at 5.202 s and the counter reaches 6 ml by 5.32 s. | The wait/return transition is functioning; this alone does not validate every timing estimate. |
| 6.09–7.93 s | Escape plan for droplet 8/bomb 12 persists while droplets 9, 10, and 11 pass in other columns. The score stays at 18 ml across the inspected frames. | The builder pairs a catch with a later bomb despite intervening droplets, and the escape phase prevents reconsidering those opportunities. First and escape destinations are identical in this plan. |
| 14.10–15.49 s | Escape plan for droplet 32/bomb 36 persists at a right-side destination while droplets 34 and 35 approach/pass on the left. | Another example of retaining a safe refuge too long rather than planning the next collectible. |
| 25.371 s | A dodge/return plan selects x≈0.620 from observed body-center x≈0.158 and marks it safe. | Large alignment move is permitted under the current timing model. |
| 25.678 s | ADB has returned after about 264 ms; observed x≈0.352 is still short of x≈0.620. An explosion follows/is visible in this interval. | Command completion does not prove physical arrival; current path/timing/geometry estimates did not prevent this hit. The footage does not isolate which timing component is responsible. |
| 35.936 s | Result screen shows 228 ml. | Confirmed final collection total. |

The log contains 940 observations with median spacing about **38.8 ms** and 145
unique command submissions. Median completed-command turnaround is about
**186.6 ms**, nearly unchanged from the preceding run. It is not a measurement
of exact physical movement duration. Long escape phases are therefore a policy
issue visible independently of any change in observation frequency.

Recommended next refinement, **not implemented in this review**: avoid treating
a distant following bomb as an immediate catch/escape pair when collectibles
intervene; finish or interrupt an escape commitment once the can is clear and a
new safe collectible opportunity exists, rechecking the full path and the bomb
clearance constraints. Preserve the working wait-before-crossing behavior.
Revisit movement timing separately using command and observed-position evidence.
Do not describe this trial as a demonstrated collection improvement or copy the
current long-lived escape commitment into Jev without that qualification.

## Dynamic Jev live validation: 20260922-131550-168356

Result screen confirms **114 ml**. Reviewed footage and request/response events;
no controller code was changed. The planned showcase edit/post should wait for
an integration fix rather than present selected successful moments as a fair
comparison.

The complete 39.08 s session contains 1,009 logged observations, 62 requests,
61 returned responses, nine accepted responses, and nine logged command
submissions. Counts include pre-game/result-screen time; one acceptance at
38.62 s occurs on the result screen, so nine commands is not nine useful
in-game actions. There were no logged API errors.

| Response outcome | Count |
| --- | ---: |
| Accepted | 9 |
| Active row changed | 27 |
| Originating observation older than 500 ms | 24 |
| Drag in progress | 1 |

All 27 combined `can missing or active row changed` rejections had a visible can:
the active-row condition was responsible. Fourteen selected candidate IDs still
existed at response time, and seven were still marked safe within their current
evaluation window. This does not prove those seven remained useful/reachable,
but shows that active-row identity is an overly coarse validity proxy.

Median SDK-call elapsed time was **416.7 ms**, with a range of **322.4–1,985.4 ms**.
Median observation age when responses were consumed was **449.6 ms**. Only 13
SDK calls exceeded 500 ms; the age check also includes observation/request and
main-loop handling delays. The 500 ms age budget therefore rejected more than
just calls taking over 500 ms. Request spacing was also at least 500 ms.

Primary finding: the implemented control loop was starved of executable
responses by latency, row-change invalidation, and the lack of a fallback.
This result does not isolate Jev's choice quality, and reducing the problem to
insufficient model reasoning would not be supported by these logs. The
integration should have been tested against realistic response delays offline
before consuming a live attempt.

Recommended next work, not implemented in this review: evaluate retained
object/plan relevance rather than requiring unchanged active-row identity;
request decisions far enough ahead for measured API delay; keep immediate
execution and hazard handling local. If a deterministic safety fallback is
introduced, label the resulting controller as hybrid and log which component
selected each action. Merely increasing the age limit would permit outdated
commands and is not an adequate fix. Validate latency and invalidation behavior
with delayed mocked responses or recorded states before another live attempt.


## Rolling schedules and latency-aware Jev integration

Implemented a shared visible-row schedule builder and local runner. The default
deterministic policy now visits intervening droplets instead of committing to
one catch and a distant bomb. Jev chooses future schedules; a local near-term
prefix continues during requests, and remaining intentions are revalidated after
reply. Active-row changes alone no longer discard responses. Busy replies wait
for activation instead of being lost. This is explicitly **hybrid control** with
local startup/repair/safety source attribution.

63 tests pass, including current rolling behavior, delayed mocked API replies,
SDK serialization, an ideal-motion closed-loop fixture, and retained historical
regressions. No live API or phone action was performed. A timing-only replay
using the failed run's recorded delays and a synthetic best-catch selector
returned 30 replies: 17 activated schedules, 11 failed activation, and two were
discarded earlier. It logged 70 local invalidations, consistent with the mismatch
between hypothetical routes and recorded can motion; it is not a score simulation
or evidence of Jev quality. Details and commands are in
[rolling schedule design](rolling-schedules.md).

A perfect-human shadow replay processed 915 frames with ten unsafe-proposal
frames. It cannot establish alternate gameplay results. Defaults reserve 650 ms
of lead for API latency, adapt that lead using recent latency, retain a two-second
outer response-age cap, and preserve the 250 ms current-frame freshness check.
Next live trial remains pending. Report `sequence.source` and plan-activation
logs when comparing the hybrid run to deterministic control.
