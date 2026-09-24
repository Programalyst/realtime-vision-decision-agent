# Jev policy: future schedule selection

The current Jev integration chooses a rolling schedule rather than an immediate
left/right/hold action or a single destination. See
[rolling schedule design](rolling-schedules.md) for the implementation, exact
lifecycle, settings, logs, tests, and limitations.

Jev calls remain independent `system_one` requests: there is no conversation
history or session ID. Python retains object tracks and an accepted plan. Each
request offers up to four feasible future schedules with a common near-term
prefix that continues executing during API inference.

The response selects a `plan` ID. Python waits for the activation boundary and
executor availability, rebuilds remaining intentions against current observations,
and either adopts the plan or keeps local control. A passed row alone no longer
invalidates the response. API requests can overlap drags; replies are not thrown
away merely because a drag is in progress.

Defaults: minimum request spacing 0.5 s, initial future lead 0.65 s (adapted from
observed latency), and outer response-age limit 2 s. Current observations must
still be no older than 250 ms. No SDK retries; API errors back off for five seconds.

**This is hybrid control.** Local startup/fallback and immediate safety handling
remain active. Inspect `sequence.source` and `jev_events` to distinguish local
routes from retained Jev selections. Queued replies and accepted plans are
separate events. The model's confidence is not a safety guarantee.

```bash
uv run python testYolo.py --jev --control
```

Use J to start/pause control and recording. The API key remains in ignored `.env`
as `JEV_API_KEY`. Omitting `--control` suppresses phone gestures, but API requests
still occur after enabling. Logs and recordings remain under `runs/jev/`.

The old immediate-choice implementation failed live with 114 ml: its 500 ms age
limit and active-row equality gate rejected most responses. That implementation
and result are historical; do not apply its scores to this new controller.
The rolling integration has only offline validation so far. No live API call
was made during its implementation.
