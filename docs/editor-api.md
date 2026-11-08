# Editor API reliability

Apply migrations before starting the API (`alembic upgrade head`). New revisions do not
modify club content. Revision 0015 marks existing media as previously public; 0016 adds creation replay
keys; 0017 adds internal integer versions without rewriting imported timestamps;
0018 adds a match publication flag, initially enabled for existing undeleted matches. New
uploads remain private until referenced by a published player, team, achievement or
match. Authenticated editors can preview private images; these responses use
`private, no-store`. Previously published images may remain in browser/CDN caches.
Removing a reference cannot recall those copies.

All editor PATCH bodies accept `expected_updated_at`, copied verbatim from the last
GET response. DELETE accepts the same value as a query parameter. A stale version
returns `409` with `code: stale_record`. The ORM also checks an internal integer version in its SQL
UPDATE predicate, so overlapping requests cannot both save the same version.
Clients should retain the draft and offer to reload/compare the current record.
Legacy clients may omit the precondition during migration, but the admin UI always
sends it.

For POST creation of players, content, competitions, venues, matches and achievements,
send a UUID `Idempotency-Key` header. Keep the key for the same draft across timeout,
network failure and reauthentication. The authenticated editor ID, resource, key,
payload hash and new entity ID are recorded in the same database transaction as the
entity. Repeating an identical request returns that entity without creating another
record. Reusing a key for different data returns 409. A new draft needs a new key.
Authentication and CSRF are required on every replay. Keys are scoped to the editor,
so another user's request cannot replay somebody else's creation.
An inconsistent, incomplete replay ledger returns `503 idempotency_incomplete`;
retain the draft and key and investigate the original save rather than creating
another record under a new key.

Admin collections support literal Unicode case-insensitive `q` search, alongside
their existing filters. Content accepts `active`; matches accept `competition_id`.
Pagination totals use the same filters as rows. Search punctuation is literal,
including `%` and `_`.
Admin matches start with the latest date; public schedules retain chronological
order. Searching matches accepts their complete visible `Home — Away` title.

External `(source, external_id)` identities of venues, competitions and matches
remain reserved after soft deletion. Recreating a deleted external record returns
409; it does not silently revive data deliberately removed by an editor.

`GET /api/v1/admin/competitions/{id}/impact` returns `matches_count` and
`published_matches_count`. Check impact before deletion or unpublishing a tournament.

`GET /api/v1/admin/media/{id}/usage` lists references with editor/public URLs and field
names. A referenced-image deletion returns the same references in `errors`. Upload
responses add `reused`: duplicates preserve the existing shared alt text; edit its
description explicitly to change it everywhere.

Teams expose authenticated list, detail and PATCH at `/api/v1/admin/teams`; editable
fields are `name`, `logo_asset_id`, `active`. Team creation/deletion and category/slug
changes are deliberately outside this editor contract.

## Trash and recovery

Editors and administrators can list deleted records at
`GET /api/v1/admin/trash/{entity}` for `players`, `achievements`, `content`,
`competitions`, `venues`, `matches` and `media`. The authenticated route supports
`q`, `page`, `page_size` and, for content, `section`. Public endpoints and existing
admin lists never include these tombstones. Team deletion stays disabled.

Restore with `POST /api/v1/admin/trash/{entity}/{id}/restore` and the required body
`{"expected_updated_at":"<value from the trash item>"}`. Editor permissions and
CSRF are required. The original ID, scalar content and foreign keys are retained;
`is_deleted`/`deleted_at` are cleared and the item remains unpublished (`active=false`).
Media becomes private until referenced by a published record. A missing media blob,
deleted image, deleted team, competition or selected venue blocks recovery with
`409 restore_dependency`; recover that dependency first. The response explains it.

An identical retry on an already recovered hidden item returns `restored=false`
without changing data or adding a duplicate audit entry. A retry after publication
returns 409 and never hides the published record. Restore changes use the same ORM
version predicate as editing and are recorded as audit updates with
`after.operation="restore"`.

Recovered competitions leave dependent matches hidden, including when the league
is later published. Review and publish each match separately. Match PATCH accepts
`active`; public schedules, home results and image visibility exclude inactive matches.
Explicitly publishing a player, achievement or match with a hidden parent returns
`409 publication_dependency` for teams/competitions. New match publication or a
new venue assignment requires a published selected venue. The admin shows the
missing-parent explanation.

Hiding or deleting a venue leaves already published matches available. All public
match responses (list/detail, home and calendar) redact both `venue` and `venue_id`
while the venue is unpublished/deleted. The admin retains the original association;
editing an already published match with that unchanged venue is allowed. Logos stay
public while the match remains published. Restoring a venue keeps it hidden and
does not change matches' `active` or `featured` values; venue details return only
after the venue is explicitly published. A deleted match still requires its venue
to be recovered before match recovery.

The UI exposes **Записи / Корзина** in each editable collection and **Фотографии /
Корзина** in the media library. Deleted items can only be restored, not edited or
permanently deleted. The success action opens the recovered hidden item for review.

## Production deployment

`compose.yaml` is a development example. Use `compose.production.yaml` explicitly for a
production deployment, with `APP_PUBLIC_ORIGIN` set to the exact HTTPS frontend origin.
It enables production validation, Secure `__Host-` cookies and binds API port 8000
to loopback. An HTTPS reverse proxy on the host must route `/api` and `/media` to
that port, serve the frontend, enforce a request-body limit and rate-limit login
across API workers. The in-process limiter supplements the proxy limiter.

Back up the SQLite database and media volume before deploying migrations. The
application's Secure cookies require HTTPS, including when accessing the admin UI.

### First deployment with existing club data

The image deliberately excludes `data/`. An empty `backend_data` volume starts with
an empty schema; migrations do not import the club snapshot or create an administrator.
Before the first `up`, transfer a **consistent backup of the actual working database**
and its matching media directory. Do not substitute the repository snapshot for a
newer working database. Stop writes while taking the final database/media backup;
use SQLite's backup API (or `.backup`), not a live copy of only the `.db` file when
WAL may be active. Keep the export outside version control.

The following commands are for a **new, empty** deployment volume. Set
`APP_PUBLIC_ORIGIN` in the environment first. Replace `/trusted/export` with the
directory containing `local.db` and `media/` from that backup:

```sh
docker compose -f compose.production.yaml build
docker compose -f compose.production.yaml create migrate
docker compose -f compose.production.yaml cp /trusted/export/local.db migrate:/app/data/local.db
docker compose -f compose.production.yaml cp /trusted/export/media/. migrate:/app/data/media
docker compose -f compose.production.yaml run --rm --no-deps --user root migrate chown -R app:app /app/data
docker compose -f compose.production.yaml run --rm --no-deps migrate alembic upgrade head
```

Do not repeat the copy over an existing deployment: it would replace its data.
The one-time ownership command targets the dedicated application volume so the
unprivileged runtime can update SQLite and media. Existing users and password hashes
are retained. If the imported database has no active administrator, create one with
an interactive password prompt inside the same volume:

```sh
docker compose -f compose.production.yaml run --rm --no-deps -it migrate python -m predatory_beavers.cli create-admin --username admin --email admin@example.com
docker compose -f compose.production.yaml up -d
```

For a deliberately empty installation, omit the copy, migrate first, run
`python -m predatory_beavers.cli seed-club` through the same one-off `migrate`
service, then create the administrator. This creates two teams, not historical club
content. Verify `/health/ready`, login, published rosters, calendar and image URLs
before switching traffic. The transfer commands are an operational example; no
production volume or Docker container was started during the local audit.
