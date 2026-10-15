# pacificorp-queue

Loads PacifiCorp's public generation interconnection queue pages (OATI OASIS) into
Postgres, repeatably: running it twice on the same data changes nothing, and a new
publication date adds a new snapshot. It covers all six queue pages (Cluster 1-4,
Transition Cluster, Transition Cluster 2; 734 requests as of 2026-09-14) and the
legacy serial-queue page, which is published but empty.

One runtime dependency (`psycopg`), Python 3.12+, everything else standard library.

## Why this project

I wanted something close to the work this role describes: pulling messy public data
into Postgres so that re-running it is safe. These pages are Excel-exported HTML,
served from a private certificate authority, with column names that shift between
pages. That gives real decisions to defend (how to trust the server, what counts as a
change between publications, what to do with values the page gets wrong) in an amount
of code that can be reviewed in one sitting.

I also considered submitting `drift`, my `pg_stat_statements` trending and anomaly
tool. It needed more work than the time allowed before I would put it in front of
reviewers as production code, and the take-home brief seems to prefer a smaller
amount of high-quality work.

## How to run it

```bash
python3 -m venv .venv && . .venv/bin/activate   # Python 3.12 or newer
pip install -e ".[dev]"

docker run -d --name pcq-pg -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:18-alpine
# -h 127.0.0.1 forces a TCP check: the image's init server listens on a socket only
until docker exec pcq-pg pg_isready -h 127.0.0.1 -U postgres -q; do sleep 0.5; done
export DATABASE_URL=postgresql://postgres:pw@localhost:5432/postgres

pacificorp-queue migrate            # create / upgrade the schema
pacificorp-queue load               # fetch all pages and load them
pacificorp-queue load --source tc2  # one page
pacificorp-queue load --html-dir tests/fixtures  # offline, from saved pages
pacificorp-queue load --strict      # fail a page on any unrecognized value

docker rm -f pcq-pg                 # stop and remove the database when done
```

If port 5432 is already taken, start the container with `-p 5433:5432` (or any
available and suitable port) and use that port in `DATABASE_URL`.

`--html-dir` takes any directory that holds one `<source>.html` file for each selected
source (`tc1`, `cs1`-`cs4`, `tc2`, `sq`), for example pages you saved from the site; a
missing file is reported before anything loads. The example uses `tests/fixtures`, which
ships four-row excerpts of the real pages. Output is one log line per page, for example
`source=cs4 as_of=2026-09-14 parsed=122 inserted=0 updated=0 unchanged=122 deleted=0 issues=0`,
and the exit code is non-zero if any page failed.

## Tests

```bash
pytest                       # needs Docker: starts postgres:18 via Testcontainers
pytest -m "not integration"  # no database needed (parser, normalizer, fetch)
PCQ_TEST_DSN=postgresql://... pytest   # use an existing server instead of Docker
PCQ_FULL_PAGES_DIR=/path/to/saved/pages pytest   # opt-in: the full 734-row run
ruff check . && ruff format --check . && mypy   # lint, formatting, types
```

Each integration test runs in its own schema; without Docker they error rather than
skip. The suite covers each data trap in the [data notes](docs/data-notes.md) as a
fixture row, a second load being a no-op, one changed cell updating exactly one row,
deletion of vanished rows, an id on two pages being rejected, an empty page being
refused, strict mode writing nothing, rollback on a failure after the upsert,
concurrent loads of one page, and TLS and retry behaviour. `ruff` and `mypy --strict`
are clean.

## How it works

```
fetch.py      TLS-pinned download (stdlib urllib)
parse.py      HTML table -> header + string rows (stdlib html.parser)
normalize.py  header aliases + vocabularies -> typed QueueEntry   (table-driven)
model.py      QueueEntry, the stored record, and its content hash
load.py       diff against stored hashes, upsert changed rows, one transaction
pipeline.py   glue: decode -> parse -> normalize -> load

sources.py    the seven pages: key, URL, and whether an empty page is allowed
db.py         SQL migration runner
guard.py      refuses to start a transaction inside an open one
cli.py        `migrate` and `load` commands, exit codes, per-page error isolation
```

### TLS

`oasis.oati.com` chains to a private OATI root, not a public CA. The root is bundled in
`src/pacificorp_queue/certs/` and its SHA-256 (`7A:77:C6:...:0B:24:79`) is pinned in code.
The connection trusts only that root, not the system store, with certificate and
hostname verification on, and nothing disables verification anywhere. I pin the root
rather than the leaf because the leaf expires on 2026-12-13 and the root in 2038. I
checked offline that the leaf and issuing CA I captured validate against this root, with
hostname checking and strict X.509 rules.

### Idempotency

Rows are keyed by `(as_of, queue_number)` and store a SHA-256 of everything persisted
(normalized columns and raw cells), so a change to the page *or* to the normalization
rules is noticed. A load writes only new or changed rows and deletes rows that
vanished, so `updated_at` means "content last changed". The whole load, including a
`source_load` provenance row (page hash, `Last-Modified`, `ETag`, counts), is one
transaction serialized per page by an advisory lock. The page's own as-of date is the
snapshot key, not the time the job ran.

### Failure policy

Nothing is dropped quietly:
- Unrecognized header, a row that looks like a queue entry but has the wrong number of
  cells, a missing or ambiguous as-of date, or an unreadable required value (queue
  number, request date, status): the page fails. Rows that do not fit the table
  (banners, legend, blank spacers) are skipped, and the count is logged per page.
- Unrecognized value in an optional column: stored as NULL, logged, counted in
  `source_load.issues`, original text kept in `raw`. `--strict` fails the page instead.
- An empty page fails unless the source is the serial archive. Text is decoded strictly
  as windows-1252, so no replacement characters reach the database.

### Dependencies

The schema is one SQL file (`src/pacificorp_queue/migrations/001_initial.sql`) applied
by a small runner (`schema_migration` table, advisory-locked), the queries are a
handful of plain statements, and the parser is one short module. That keeps the
installation to a single dependency. As the project grows, I would consider packages
like Alembic, an ORM and pandas.

All SQL values are bound parameters. The one query with dynamic column names (the
upsert in `load.py`) is composed with `psycopg.sql`, so no query is built by string
formatting.

## Judgment calls

I am new to energy markets, so [docs/data-notes.md](docs/data-notes.md) separates what
the pages show, what I inferred, and what I left alone. The calls that matter most:

- 110 "Withdrawn" rows are really transfers to Transition Cluster 2. They keep
  `status = 'withdrawn'` and get `moved_to_cluster = 'T2'`, so capacity is not counted
  twice.
- Lineage is `former_queue_numbers text[]` (GIN-indexed), because a T2 request can have
  up to five parents. Request dates reset on transfer, so the original date needs that
  lineage.
- Excel artifacts (`1/0/1900`, a bare `0`, `NA`) become NULL on purpose, not issues.
- Unconfirmed:
  - Why Cluster 3 and 4 requests moved into Transition Cluster 2.
  - What "Combined with ..." means.
  - Whether `ER/NR` and `NR/ER` differ.

## Not done

- Original request date through chains deeper than one hop (a recursive CTE over
  `former_queue_numbers`). Lineage is at most one hop today; the array column and GIN
  index are there for it.
- Scheduling, and alerting when a page's row count falls sharply between dates.
- Concurrent loads of two different pages that contain the same queue number for the
  same as-of date. Loads are serialized per source, so both could pass the cross-page
  clash check and the second upsert would overwrite the first row. The CLI loads
  sequentially, and one id on two pages would already be a data anomaly. The fix is a
  global lock, or `WHERE queue_entry.source = EXCLUDED.source` on the upsert.
- A live fetch in CI. CI (`.github/workflows/ci.yml`) runs lint, `mypy --strict` and the
  full suite, including the Postgres 18 Testcontainers tests, on Python 3.12, 3.13 and
  3.14, but never hits PacifiCorp's server. A manual run against the live site on
  2026-10-15 loaded all 734 rows with no issues, and an immediate second run reported
  every row unchanged. The TLS chain was verified offline with `openssl`, not by a test.
  Actions are pinned to major versions, not commit SHAs.
- Smaller gaps I know about. There is no cap on response size (one trusted host, reached
  over TLS with a pinned root). The refusal of https-to-http redirects is tested at the
  redirect handler, not end to end through `fetch_page`.
- Other utilities. This tool is scoped to PacifiCorp on purpose. Extending it would take
  more than adding entries to sources.py: a Source would need to carry its utility and
  full URL, and queue_entry.source would need a utility column (or namespaced keys like
  ppw:cs4) so that keys can't collide. The larger piece is normalize.py, whose header table
  and vocabularies describe PacifiCorp's columns and spellings. Each utility would need its
  own normalizer behind a small shared interface that returns QueueEntry. I haven't built
  or tested code for any additional utilities.

## AI assistance

Claude (Anthropic) wrote the code and tests, working from a scope I set: PacifiCorp's
six queue pages, one runtime dependency, Python 3.12, Postgres 18, and CI. I then went
through the code file by file. Review found and fixed:

- `fetch_page` did not retry a response body cut short, although its docstring said it
  did. It does now, with a test.
- Redirects from https to a weaker scheme are refused.
- `load` printed raw tracebacks for a missing `--html-dir`, an unreachable database and
  a bundled root CA that fails its fingerprint check. Each is now one logged line and
  exit code 2.
- Duplicate source keys are rejected, and `load --help` lists each source.

Beyond the test suite: the TLS chain was checked with `openssl`, the tool was run
against the live site, and the tests for the redirect refusal and the bad-root-CA exit
were mutation-checked to fail when the code they cover is removed.
