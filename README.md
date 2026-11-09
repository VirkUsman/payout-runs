# payout-runs

_Built with AI assistance._

Pays marketplace sellers in batches, and keeps paying each of them exactly once
when a worker is killed mid transfer, when the bank's answer gets lost, or when
someone presses the button ten times.

Django 5.2, Django REST Framework, Celery on Redis, PostgreSQL. The tests use
real Postgres, a real Redis broker, real worker processes, and a fake bank that
is a real HTTP server, so it can apply a transfer and then hang up.

```
payouts/models.py     sale lines, runs, payouts
payouts/services.py   collecting sales, the lease, the fence, the reaper
payouts/tasks.py      the two Celery tasks
payouts/bank.py       what counts as a refusal and what counts as "no idea"
payouts/api.py        REST endpoints, JWT, sellers see only their own
tests/fake_bank.py    scriptable bank: refuse, 503, apply then drop the line
```

## The bad moment

The worker calls the bank. The bank moves the money. The connection dies before
the answer arrives, or the worker is killed before it writes the row.

Nothing on our side knows the money moved. The row says `sending`, and the only
way forward is to ask again. Retrying is therefore not optional, which means a
second request for the same payment is normal operation and the bank has to be
able to recognise it.

So the idempotency key is derived from the payout, never from the attempt:
`payout-<id>-<generation>`. `test_worker_killed_while_the_bank_is_answering`
sends `SIGKILL` to a real worker after the bank has applied the transfer. A
second worker picks the payout up, sends the same key, and the bank still holds
one transfer.

## What stops each kind of duplicate

| What goes wrong | What handles it |
|---|---|
| Message delivered twice | Row lock with `SKIP LOCKED` and a lease in `claim`. Eight concurrent workers produce one bank call |
| Worker dies holding the lease | Lease expires and the beat task re-queues it |
| Answer lost after the bank applied it | Same key on every attempt |
| Worker stalls past its lease, then wakes up | The attempt number is a fencing token, so its late write matches no row |
| Run rolled back after queueing | Messages go out in `transaction.on_commit` |
| Broker down at commit | Run is kept, the callback failure is logged, the reaper queues it later |
| Create pressed ten times | Unique period, and the repeat returns the existing run with a 200 |
| Two overlapping periods at once | Sale lines locked in id order. The second run waits, then finds them taken |

## A refusal and a timeout need opposite retries

Both end up as a failed payout that an operator can retry, and the key has to
be handled in opposite ways.

A **refusal** means no money moved. Banks remember the result against the key,
so after the seller fixes their IBAN the same key would replay the old refusal.
The retry bumps the generation and gets a new key.

**Giving up** after six unanswered attempts means the money may have moved. A
new key here is exactly how someone gets paid twice, so the retry keeps the old
one. `Payout.failure` exists to tell these two apart.

## Found by the tests

`transaction.on_commit(partial(fn, pk), robust=True)` looks correct and fails in
the case `robust` exists for. When the callback raises, Django 5.2 builds its log
message from `func.__qualname__`, a `functools.partial` does not have one, and
the resulting `AttributeError` escapes the handler. A broker outage at commit
would have returned a 500 for a run that had already been saved. The callback is
now a named function.

## Checking that the tests check something

Each of these was applied by hand and the suite went red:

| Deliberate break | Tests that failed |
|---|---|
| Key includes the attempt number | 2, including the `SIGKILL` test |
| Queue inside the transaction | 3 |
| Remove the fencing token | 2 |
| Remove the row lock in `claim` | 1 (eight workers) |
| Remove the lock on sale lines | 1 (overlapping periods) |
| Reuse the key after a refusal | 2 |
| Remove `select_related` on the list | 1 (query count) |

CI repeats the first of these on every push.

## API

| | |
|---|---|
| `POST /api/token/` | JWT login |
| `POST /api/runs/` | Staff. `{"period_end": "2026-10-31"}`. 201, or 200 for a repeat |
| `GET /api/runs/<id>/` | Staff. Counts and money by status |
| `GET /api/payouts/?status=failed` | Sellers get their own, staff get all |
| `POST /api/payouts/<id>/retry/` | Staff. 202, or 409 unless the payout has failed |

## Running it

Needs Postgres and Redis on localhost, and [uv](https://docs.astral.sh/uv/).

    createdb payout_runs
    uv sync
    uv run pytest

45 tests, about 35 seconds. Set `CELERY_BROKER_URL` if your Redis is not on 6379.
The tests purge that broker's default queue, so point it at a spare database.

`docker compose up` starts the API, worker, beat, Postgres and Redis, and CI
checks that it comes up. It expects a bank at `BANK_URL`, because none ships here
apart from the test double.

## What it leaves out

- It depends on the bank honouring idempotency keys. A bank that does not would
  need a reconciliation job reading its statement, and that is not built.
- One currency, and no seller balance beyond the open sale lines.
- Collecting sales loads the open lines into memory. The largest run tested has
  200 lines, and an `INSERT ... SELECT` would be the next step.
- No admin UI. Failed payouts are retried through the API.
- A payout at `gave_up` is safe to retry because the key is unchanged, but
  nothing here tells an operator whether the bank has already paid it.
