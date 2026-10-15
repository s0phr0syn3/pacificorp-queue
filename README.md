# pacificorp-queue

Loads PacifiCorp's public generation interconnection queue pages (OATI OASIS)
into Postgres, repeatably: running it twice on the same data changes nothing,
and running it on a new publication date adds a new snapshot.

It covers all six queue pages (Cluster 1–4, Transition Cluster, Transition
Cluster 2; 734 requests as of 2026-09-14) and the legacy serial-queue page,
which is published but empty.

One runtime dependency (`psycopg`). Python 3.12. Everything else is the standard
library.

## Run it

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

docker run -d --name pcq-pg -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:18-alpine
until docker exec pcq-pg pg_isready -h 127.0.0.1 -U postgres -q; do sleep 0.5; done  # TCP: init server is socket-only
export DATABASE_URL=postgresql://postgres:pw@localhost:5432/postgres

pacificorp-queue migrate            # create / upgrade the schema
pacificorp-queue load               # fetch all pages over TLS and load them
pacificorp-queue load --source tc2  # one page
pacificorp-queue load --html-dir tests/fixtures  # offline: needs <source>.html files
pacificorp-queue load --strict      # fail a page on any unrecognized value
```

`--html-dir` needs one `<source>.html` file for each selected source (`tc1`, `cs1`–`cs4`,
`tc2`, `sq`); a missing file is reported up front and nothing is loaded. The files in
`tests/fixtures` are four-row excerpts of the real pages, enough to try the pipeline.

Output is one log line per page:
`source=cs4 as_of=2026-09-14 parsed=122 inserted=0 updated=0 unchanged=122 deleted=0 issues=0`.
Exit code is non-zero if any page failed.

## Tests

```bash
pytest                       # needs Docker: starts postgres:18 via Testcontainers
pytest -m "not integration"  # no database needed (parser, normalizer, fetch)
PCQ_TEST_DSN=postgresql://... pytest   # use an existing server instead of Docker
PCQ_FULL_PAGES_DIR=/path/to/saved/pages pytest   # opt-in: the full 734-row run
```

Each integration test runs in its own schema, dropped afterwards. Without Docker
the integration tests error; they do not skip.

The suite covers: every data trap below (as a fixture row each); loading twice is
a no-op down to `updated_at`; one changed cell updates exactly one row; a row
that disappears from a republished date is deleted; a new as-of date adds a
snapshot; an id that appears on two pages is rejected; an empty page is refused;
strict mode writes nothing; a failure after the upsert rolls the whole load back;
two concurrent loads of one page do not double count; TLS pinning and retry
behaviour. `ruff` and `mypy --strict` are clean.

## How it works

```
fetch.py      TLS-pinned download (stdlib urllib)
parse.py      HTML table -> header + string rows (stdlib html.parser)
normalize.py  header aliases + vocabularies -> typed QueueEntry   (table-driven)
load.py       diff against stored hashes, upsert changed rows, one transaction
pipeline.py   glue: decode -> parse -> normalize -> load
```

**TLS.** `oasis.oati.com` presents a certificate chaining to a private OATI root,
not a public CA. The root is bundled in `certs/` and its SHA-256
(`7A:77:C6:…:0B:24:79`) is pinned in code; the tool refuses to fetch if the file
differs. The connection trusts only that root (not the system store) with
certificate and hostname verification on. I pin the root rather than the leaf
because the leaf expires on 2026-12-13 and the root in 2038. I checked offline
that the leaf and issuing CA I captured validate against this root, with
hostname checking and strict X.509 rules. Nothing disables verification anywhere.

**Idempotency.** Rows are keyed by `(as_of, queue_number)`. Each row stores a
SHA-256 of everything persisted (normalized columns and the raw cells), so a
change to the page *or* to the normalization rules is noticed. A load reads the
stored hashes for that page and date, writes only new or changed rows, and
deletes rows that vanished. Unchanged rows are not touched, so `updated_at`
means "content last changed". The whole load, including a `source_load`
provenance row (page hash, `Last-Modified`, `ETag`, counts), is one transaction,
serialized per page by an advisory lock. The page's own as-of date is the
snapshot key, not the time the job ran.

**Failure policy.** Nothing is dropped quietly.
- Unrecognized column header, wrong cell count, missing or ambiguous as-of date,
  unreadable required value (queue number, request date, status): the page fails.
- Unrecognized value in an optional column: stored as NULL, logged, counted in
  `source_load.issues`; the original text is always kept in `raw`. `--strict`
  fails the page instead.
- An empty page fails unless the source is the serial archive, which is empty.
- Text is decoded strictly as windows-1252, so no replacement characters reach
  the database.

**Why no Alembic / ORM / pandas.** The schema is one SQL file applied by a
small runner (`schema_migration` table, advisory-locked), the queries are a
handful of plain statements, and the parser is one short module. That keeps the
install to a single dependency. Alembic would be the first thing I add once there is a
second migration worth reviewing.

## What the data taught me

All counts are for the 2026-09-14 publication.

| Trap | Handling |
|---|---|
| Column names differ by page: `S`/`W` vs `Summer`/`Winter`; `Former Queue Number` (T2) vs `Former Serial Queue Number` (TC1) | alias table in `normalize.FIELDS` |
| Header row position | found by content, not by index |
| Banner, legend and spacer rows | recognised by cell count; a short row that starts like a queue id is an error |
| `Withdrawn` rows that are really transfers: 110 say `Request moved to T2 Cluster` (81) or `Combined with …, moved to T2 Cluster` (29) | `moved_to_cluster = 'T2'`; `status` stays `withdrawn` |
| Request date resets on transfer: all 92 T2 requests carry a later date than their earliest parent | `request_date` is the page's value; parents are in `former_queue_numbers` |
| Multi-parent lineage (10 T2 requests have 2–5 parents, e.g. `C3-155, C3-156, C4-051, C4-073`) | `former_queue_numbers text[]`, GIN-indexed |
| Excel artifacts: `1/0/1900` (Qualifying Facility, a COD), bare `0` (97 status explanations), `NA` cluster area | NULL |
| `TBD` dates, blank company (538 of 734, shown only after an agreement is signed) | NULL |
| Mixed vocabularies: `QF`/`Yes`, `NO`/`No`, `WITHDRAWN`/`Withdrawn`, 22 spellings of 16 fuel types | folded in `normalize` tables |
| Serial queue page is empty | allowed for that source only |

Example queries:

```sql
-- Earliest parent request date for each T2 request (one hop is enough today:
-- no C3/C4 request has parents of its own).
SELECT t.queue_number, t.request_date, min(p.request_date) AS earliest_parent
FROM queue_entry t
JOIN queue_entry p
  ON p.queue_number = ANY (t.former_queue_numbers) AND p.as_of = t.as_of
WHERE t.source = 'tc2'
GROUP BY t.queue_number, t.request_date;

-- Withdrawn because it moved, vs withdrawn for good (Clusters 3 and 4).
SELECT moved_to_cluster IS NOT NULL AS transferred, count(*)
FROM queue_entry
WHERE status = 'withdrawn' AND source IN ('cs3', 'cs4')
GROUP BY 1;
```

## What I know and what I don't

I am new to energy markets. This is what I am sure of, what I inferred, and what
I left alone.

**Know (from the pages themselves)**
- Page names, id prefixes and the `Cluster` column agree: `C1`–`C4` are Cluster
  1–4, `TCS-` is "Transition Cluster", `T2-` is "Transition Cluster 2".
- Transition Cluster requests name the *serial-queue* number they came from
  (`Former Serial Queue Number`, e.g. `839`); the serial page is empty, so those
  parents are not in the data. Transition Cluster 2
  requests name Cluster 3 or 4 parents, and every parent they name is present.
- Every request marked "moved to T2" is the parent of at least one T2 request.

**Inferred, not confirmed**
- FERC Order 2023 required utilities to move existing queued projects through a
  one-time "transitional" cluster study (see
  [K&L Gates summary](https://www.klgates.com/thought-leadership/Order-No-2023-Interconnection-Reform-is-Finally-Here-9-7-2023)).
  I read the Transition Clusters as PacifiCorp's version of that. I did not read
  PacifiCorp's tariff, so why Cluster 3/4 requests were moved into Transition
  Cluster 2 is my reading of the data, not a sourced fact.
- `Combined with C4-050, moved to T2 Cluster` I read as "merged with that request
  into one T2 project". It is why some T2 requests have several parents.
- `1/0/1900` in Qualifying Facility is an Excel zero date, not an answer, so it
  is stored as unknown (NULL), not as "No".
- `Pump Storage` → `Pumped Storage` and `Battery` → `Battery Storage` are
  merges I made (visible in `normalize.FUEL_TYPE`); the original is in `raw`.

**Don't know**
- Whether `ER/NR` and `NR/ER` mean different things. They are stored as published.
- What the `Application Rules` codes (`LGI`, `SGI`, `OGI`, `OLGI`, `UGI`, `WGI`,
  `QFLGI`) stand for beyond what their names suggest; they are stored as codes.
- Why `C4-032` (withdrawn, not marked as moved) is named as the parent of
  `T2-001`. It is the only parent in the data not marked as moved. It looks like
  an inconsistency in the source, and a test pins it so it cannot go unnoticed.
- How agreement history in the free text (`LGIA executed 5/14/2026, suspended
  5/18/2026`) should be modelled. It is kept as text.
- Whether withdrawn-and-transferred requests should count towards "capacity in
  the queue". Counting both the parent and the T2 child would double count, which
  is why `moved_to_cluster` exists.

## Not done

- Original request date through chains deeper than one hop (a recursive CTE over
  `former_queue_numbers`). Not needed for this snapshot, since lineage is at most
  one hop; the array column and GIN index are there for it.
- Scheduling, and alerting when a page's row count falls sharply between dates.
- A live fetch in CI. CI (`.github/workflows/ci.yml`) runs lint, `mypy --strict` and the
  full suite, including the Postgres 18 Testcontainers tests, on Python 3.12, 3.13 and
  3.14, but it does not hit PacifiCorp's server. The TLS chain was verified offline
  with `openssl`, not by a test. A manual run against the live site on 2026-10-14
  loaded all 734 rows with no issues, and an immediate second run reported every row
  unchanged.
- Actions are pinned to major versions, not commit SHAs.
- Python 3.12, 3.13 and 3.14 passed the suite, `ruff` and `mypy --strict` locally against
  Postgres 18 before the CI matrix was added; the matrix is what keeps that true.

## AI assistance

This project was written with AI assistance (Claude), as the take-home
instructions allow.
