# FOSS-007 — first boot takes ~10 minutes loading baked GLiNER2 with ZERO progress output; docs say "~2 minutes", the wizard's own poll gives up at 4 minutes

- **Branch matrix:** es 761cf914 measured · it 9f2724ac same boot path · main c0fe9322 same
- **Severity:** Medium-High (fresh-install trust: a human watching 8 minutes of "unhealthy" with no output will kill the stack and file "install broken")
- **Discovered:** 2026-10-17, fresh-install gauntlet on an idle 20-core Linux host

## Symptom

After a clean build, the stack starts and the API spends **~9.9 minutes** before /health answers:

    containers started 15:32:30 -> /health ok 15:42:22

- The last startup line for ~7 of those minutes is "startup.gliner2_loading" — a silent,
  single-core (95% CPU) load of the baked model. No progress, no ETA.
- docker compose marks the api "(unhealthy)" during the wait (healthcheck has no adequate
  start_period for first boot).
- The wizard's own _poll_health gives up at 240 s and prints "! not healthy yet within the
  wait — check: docker compose logs faultline" — on a perfectly healthy stack.
- README says "Wait ~60s, then check: curl :8000/health"; the wizard prints "first boot
  takes ~2 minutes". Both off by ~5x on mid-range hardware.

## Root cause

Model load time (deberta-class weights, CPU init) + every migration on first boot, against
docs/wizard/healthcheck budgets written for warm restarts. Nothing lies to the operator —
everything just gives up early and says nothing while the biggest cost of the install runs.

## Reproduce

Documented install, any branch, first boot: watch docker compose ps go unhealthy while
docker logs shows only "startup.gliner2_loading" for ~7 minutes; /health answers ~10 min
in. (Second boot: warm cache, ~1-2 min — the asymmetry is the trap.)

## Fix direction (gauntlet work order)

1. Progress, not silence: log model-load milestones (weights mmap, config, init) with
   elapsed seconds at INFO, and set the compose healthcheck start_period to cover first
   boot (e.g. 15m) while keeping the fast interval for warm restarts.
2. Docs: state the measured range (first boot 5-15 min on commodity CPUs; warm restart
   1-2 min) in README Getting started AND the wizard's "Build + start" step.
3. Wizard: extend _poll_health's first-boot budget to >= 900 s with a once-a-minute "still
   loading models (Xs)..." line instead of a single dead 240 s window.
4. Pins: (i) healthcheck/config test asserting start_period covers the documented first-boot
   range; (ii) docs test asserting the 60s/2-minute claims are gone; (iii) a startup-log
   test asserting at least one progress line is emitted during model load.
5. **You are NOT your own critic.** When green, request a clean critic agent; the critic
   re-times a fresh first boot and reconciles every number before passing.
