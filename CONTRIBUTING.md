# Contributing to FaultLine

Contributions are welcome. Please read this first — there is one gate, and it is not negotiable.

## The CLA gate

**FaultLine cannot merge a contribution until its author has signed the [Contributor License
Agreement](./CLA.md).**

This will feel like friction. Here is the honest reason for it, so you can decide with the facts:

FaultLine is **open core**. The engine you are reading is AGPLv3 — free to use, free to modify,
free to self-host, forever. It is funded by a commercial layer built on top of it, sold to people
who want it hosted and supported. That is the arrangement that pays for the open engine's
development.

That arrangement only works while one party holds the rights to the whole core. The AGPL is
copyleft: a proprietary layer that links AGPL code must itself be released under the AGPL — unless
the party doing the linking *owns the copyright*, because a licensor is not bound by their own
licence. If FaultLine merged your patch under bare AGPL, the project would no longer own its whole
core, and the commercial layer would become illegal to ship. The open engine would lose its
funding.

The CLA fixes that: you keep the copyright in your contribution, and you grant FaultLine the right
to license it — including commercially. **You do not sign away your work.** You keep every right
you had, including the right to use your own contribution however you like.

This is the same instrument used by Grafana, MongoDB, Sentry, Elastic and Cockroach, and it is used
for exactly this reason.

### How to sign

Sign once, and it covers every future contribution. Open a pull request; a bot will prompt you and
record your signature. If you are contributing on behalf of an employer, you need someone who can
bind the company to sign the corporate agreement — talk to us first.

If you would rather not sign, that is a legitimate choice and no hard feelings. Open an issue
instead: a well-described bug with a reproduction is worth more than most patches, and it needs no
paperwork.

## Before you open a PR

Read `CLAUDE.md` — it is the architecture document and it is blunt about what is real versus what is
aspirational. Then:

- **New relationship types belong in the `rel_types` database table, not in code.** Any `if
  rel_type == "..."` branch will be rejected. The ontology is metadata-driven and grows at runtime;
  a hardcoded list assumes a fixed world and silently drops everything outside it.
- **Never modify GLiNER2 zero-shot labels to carry extraction patterns or descriptions.** Labels are
  tokens in the same attention sequence as the text — verbose labels collapse detection accuracy
  (benchmarked 87% → 49%). This has been re-broken more than once. See Pitfall 11.
- **No UUIDs, rel_type tokens, or internal identifiers in anything a user sees.**
- **Strong ingest, lean query.** If recall is wrong, fix it at ingest. Do not bolt cleanup onto the
  query path.
- **PostgreSQL is authoritative.** The vector index is a derived, Class-C-only scratchpad. It never
  overrides Postgres.
- **Fail loud, never silent.** A safe default plus a `log_crit` beats silently continuing on bad
  data.

## Tests

All tests must pass:

```bash
pytest tests/
```

A green suite is necessary but is **not** evidence that your change works. It only proves the paths
the suite *executes* still behave. If you fix a rendering bug, render the real output at the seam
you claim to fix. If a test starts failing because of your change, read it before you "fix" it — it
may be pinning a contract you did not know existed, and satisfying it may be the wrong move.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](./SECURITY.md).
