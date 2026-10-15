# Data notes

What the PacifiCorp pages contain, how each oddity is handled, and where my
understanding of the domain stops. The README links here from its summary.

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
  5/18/2026`) should be modeled. It is kept as text.
- Whether withdrawn-and-transferred requests should count towards "capacity in
  the queue". Counting both the parent and the T2 child would double count, which
  is why `moved_to_cluster` exists.
