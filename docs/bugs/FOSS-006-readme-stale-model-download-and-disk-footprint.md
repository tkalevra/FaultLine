# FOSS-006 — README install section claims "first start downloads the GLiNER2 model (~500 MB)"; reality: models are BAKED at build (1.2 GB HF cache), no first-start download, and the total disk footprint is documented nowhere

- **Branch matrix:** es 761cf914 reproduced (measured) · it 9f2724ac same Dockerfile pattern · main c0fe9322 same
- **Measurement note (round-1 critic flag):** the 16 GB headline is the du(1) reality of /var/lib/docker on the ZFS-backed dind right after first boot, while docker system df accounts components to ~7.5 GB (images 2.901 GB + build cache 4.486 GB of which 2.289 reclaimable + volumes 0.073 GB). Treat 16 GB as the peak-provisioning number (build-time layer duplication included) and ~3 GB as steady state after builder prune — both measured; the gap is du-vs-docker-accounting, not an error in either. README states the components AND the safe headline; critic accepted (flag non-blocking).
- **Severity:** Medium (doc truthfulness; operators size disks from docs — and the prior external report of a >150 GB FOSS footprint could not be reproduced, which is itself a documentation failure: nobody can tell what the real number is)
- **Discovered:** 2026-10-17, fresh-install gauntlet

## Symptom

README (all three branches), Getting started:

    The first start downloads the GLiNER2 extraction model (~500 MB, CPU-only — no GPU or
    CUDA required). Takes 3–5 minutes.

Measured reality on the documented install (docker compose up -d --build):

- NOTHING downloads at first start. The GLiNER2 weights are baked into the image at BUILD
  (Dockerfile RUN GLiNER2.from_pretrained(...); runtime sets HF_HUB_OFFLINE=1).
- The baked HF cache is 1.2 GB (fastino/gliner2-multi-v1 incl. mdeberta encoder) on es/it;
  the image is 2.76 GB (es) / 2.12 GB (it) / 1.99 GB (en).
- First start instead spends ~10 min loading the baked model (see FOSS-007).
- The Requirements section documents RAM ("About 4 GB free; uses ~3 GB after boot") but NO
  disk number. Measured fresh-install disk: images 2.9 GB + build cache 4.5 GB (2.3 GB
  reclaimable) + volumes <0.1 GB = **15.8 GB total under /var/lib/docker** (~3 GB steady
  state after builder prune).

The prior external report "FOSS install footprint >150 GB" (drafted 2026-10-09,
docs/ISSUES-pending-install-footprint.md on the public clone's issues branch) did NOT
reproduce on this path — but with no documented number, an operator seeing 15.8 GB (or
worse, 15.8 GB per language branch times three) has nothing to check it against.

## Reproduce

1. Documented install, any branch. Watch first start: no model download occurs (and with
   HF_HUB_OFFLINE=1 none is even attempted).
2. docker system df after install: Images ~2.9 GB, Build Cache ~4.5 GB.
3. grep the README for "500 MB" — stale claim present on all three branches.

## Fix direction (gauntlet work order)

1. Replace the stale paragraph with the truth: models are baked at build; the DOWNLOAD
   happens during docker compose build (name the two artifacts and their sizes per branch);
   first START only loads them (link FOSS-007 for timing).
2. Add a Disk line to Requirements: "~16 GB free for the first build+boot (about 3 GB
   steady-state after 'docker builder prune'); add ~2.5 GB per extra language checkout."
3. Close out the old >150 GB draft issue with the measured matrix (can't reproduce; likely
   stacked rebuild cache or an unrelated measure) — keep the draft doc's audit list as the
   checklist the critic validates.
4. Pins: a docs test that fails when the README mentions a first-start model download while
   the Dockerfile bakes the model (grep-level is fine); a docs test asserting the
   Requirements section contains a disk-footprint number.
5. **You are NOT your own critic.** When green, request a clean critic agent; the critic
   re-measures one fresh install end-to-end and reconciles every number in the new docs
   against the measurement before passing.
