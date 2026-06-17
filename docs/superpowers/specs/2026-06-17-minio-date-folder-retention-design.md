# MinIO date-folder retention design

**Date:** 2026-06-17
**Module:** `minio_handler/object.py` (`MinioObject`)

## Problem

We need to delete old data in MinIO. Objects live under a deep, caller-known
path in which a `YYYY/MM/DD` date appears mid-key, e.g.:

```
<bucket>/<prefix>/hitachi_sem/cdsem/one/2026/06/11/data/...
```

The default `bucket` and `prefix` are configured on the service. Beyond the
prefix there is an intermediate sub-path (`hitachi_sem/cdsem/one`) that anchors
where the date folders begin, then three folder levels `YYYY/MM/DD`, then opaque
payload (`data/...`). We want to **inspect** the dated folders first, then
**delete** the ones older than a retention window.

This sits alongside the existing deletion strategies in `MinioObject`
(`delete_prefix`, `delete_matching`, `delete_many`): those express "old" as a
leading prefix or a key predicate. Here "old" is a date parsed out of a
mid-key folder triple, anchored at a caller-supplied sub-path.

## Decisions

- **Anchor model.** The caller supplies `base` (the `<here>` sub-path, e.g.
  `hitachi_sem/cdsem/one`). It composes onto `default_prefix` via the existing
  `_resolve_key`, exactly like every other key. Directly under the anchor are
  three folder levels: `YYYY/MM/DD`. The anchor is required so literal folders
  (`cdsem`) are never mistaken for date segments — no auto-detection.
- **Fixed depth.** The date is always exactly three zero-padded levels
  (`2026/06/11`), day-level retention. No month-only or single-segment variants.
- **"Older than N days".** A folder dated strictly before
  `today_KST - N days` is deleted. The last `N` days **including today** are
  kept. Example: `delete_older_than(30, ...)` run on 2026-06-17 keeps
  `2026-05-18` onward and deletes `2026-05-17` and earlier.
- **KST.** "today" is `datetime.now(ZoneInfo("Asia/Seoul")).date()`, per the
  repo's KST timestamp convention.
- **Both inspect and dry-run.** A standalone `list_date_folders` for inspection,
  plus a `dry_run` flag on the delete method, so callers can preview either way.

## API

Two methods and two slots dataclasses on `MinioObject`.

### `DateFolder` (dataclass, `slots=True`)

```python
@dataclass(slots=True)
class DateFolder:
    date: datetime.date   # parsed from YYYY/MM/DD
    path: str             # full key prefix ending in "/", e.g.
                          # "hitachi_sem/cdsem/one/2026/06/11/"
```

`path` is the full stored key prefix (default prefix included) and feeds
straight back into `list` / `remove_objects`.

### `list_date_folders(base, *, bucket=None) -> list[DateFolder]`

Discovers date folders under the anchor by a **three-level non-recursive walk**:

1. list the anchor (`base`) non-recursively → year common-prefixes
2. each year non-recursively → month common-prefixes
3. each month non-recursively → day common-prefixes

Each `YYYY/MM/DD` is parsed into a `date`; segments that don't parse as a
zero-padded year/month/day are skipped (a stray non-date folder shouldn't crash
discovery). Returns the folders **sorted ascending by date**.

This is the "take a look" call: cheap (only common-prefix listings, no object
scan), and boundary segments stay literal.

### `delete_older_than(days, base, *, bucket=None, dry_run=False) -> DeleteOlderResult`

- `cutoff = today_KST - timedelta(days=days)`
- `selected = [f for f in list_date_folders(base, bucket=bucket) if f.date < cutoff]`
- `dry_run=True` → return `DeleteOlderResult(folders=selected, errors=[])`,
  delete nothing.
- `dry_run=False` → for each selected folder, recursively list its objects and
  batch them into `remove_objects`; return
  `DeleteOlderResult(folders=selected, errors=<error entries>)`.

### `DeleteOlderResult` (dataclass, `slots=True`)

```python
@dataclass(slots=True)
class DeleteOlderResult:
    folders: list[DateFolder]   # what was selected (what would be / was deleted)
    errors: list[Any]           # remove_objects error entries; empty on dry-run
```

One return type for both modes, so inspect and execute read identically.

## Why these boundaries

- `list_date_folders` does one thing — discover and parse dated folders — and is
  reused by `delete_older_than`. It can be understood and tested without the
  delete path.
- `delete_older_than` adds only the cutoff math and removal; it depends on
  `list_date_folders` and the existing `_resolve_bucket` / `remove_objects`.
- Deleting a day reuses the `delete_prefix` shape (recursive list →
  `remove_objects`), server-side narrowed to `anchor/YYYY/MM/DD/`, so the
  trailing `data/...` subtree is swept without extra logic.

## Testing

Stdlib `unittest` + `unittest.mock`; mock the client (no live MinIO):

- `list_date_folders` issues the level-by-level non-recursive listings, parses
  `YYYY/MM/DD` into `date`, returns sorted; non-date folders are skipped.
- `delete_older_than` selects exactly the folders with `date < cutoff` for a
  fixed/injected "today".
- `dry_run=True` returns the selection and issues **no** `remove_objects` call.
- `dry_run=False` issues `remove_objects` with the expected day-subtree targets
  and surfaces error entries in `DeleteOlderResult.errors`.

Assert both return values and key client call kwargs (prefix, recursive flags).

## Out of scope

- Month-only / single-segment / non-padded date formats.
- Auto-detecting the date position without an anchor.
- Server-side lifecycle policies (no `PutBucketLifecycle` permission in this
  environment — manual list+delete is the established pattern).
