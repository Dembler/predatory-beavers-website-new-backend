# Public calendar feed

`GET /api/v1/calendar` combines public calendar matches and match-level archive
evidence. Tournament-only honours are excluded. Records with the same nonempty
`source` + `external_id` appear once; the public calendar copy wins, including
editor corrections. A hidden/deleted calendar copy does not suppress independent
archive evidence.

Query parameters:

| Parameter | Values / default |
| --- | --- |
| `team` | `men`, `women`; omit for both |
| `discipline` | `5x5`, `3x3`, `phygital`; omit for all |
| `status` | `scheduled`, `live`, `finished`, `postponed`, `cancelled`, `unpublished`, `participated`; omit for all |
| `season` | `YYYY-YYYY`, `all`; omit for the server-selected default |
| `page` | integer >= 1; default 1 |
| `page_size` | integer 1–24; default 24 |
| `at` | ISO 8601 timestamp with timezone; omit for current UTC time |

Response is a direct JSON object (no `data` wrapper):

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 24,
  "total_pages": 0,
  "counts": {"upcoming": 0, "changed": 0, "results": 0, "awaiting": 0},
  "seasons": ["2026-2027"],
  "default_season": "2026-2027",
  "selected_season": "2026-2027",
  "at": "2026-11-07T12:00:00Z"
}
```

Each item has `kind` (`calendar` or `archive`), `section`, `season`, and exactly
one populated payload: `match` (`MatchRead`) or `result` (`ArchiveResultRead`).
The unused payload is null. `counts` describe the entire filtered set; `items`
contain only the requested page, with at most 24 complete records total.
Out-of-range pages clamp to the last available page (or page 1 for an empty feed).

Global ordering: `upcoming`, `changed`, `results`, `awaiting`. Upcoming games put
live games first, then ascending start date. Other sections use descending date.
Kind and UUID break equal-date ties deterministically. Pagination is global;
section headings should only render sections present on the current page.

`at` freezes the upcoming/awaiting boundary during navigation. Reuse the returned
value for subsequent pages; omit it when refreshing or changing filters. It is
not a database snapshot: later editor changes remain visible.

Season metadata follows team and discipline but ignores status and selected
season, so choosing an empty status does not erase the filter choices. Calendar
dates use Europe/Moscow's current UTC+3 offset at the July season boundary.
Explicit consecutive-year seasons take precedence. An undated calendar-year
archive record participates in both possible season filters but occurs only once
with `season: "unknown"` in the all-seasons feed. Source dates are unchanged.

The default prefers the nearest upcoming/live 5x5 season, then the most recent
finished 5x5 season, then the most recent available season. Empty feeds use `all`.
The database performs deduplication, filtering, counting and paging over a SQL
UNION. The application loads full ORM records only for the selected page.
