"""Create the member_info directory index and its mappings.

Unlike the measurement-history indices in this package, member_info is a
*lookup table*, not append-only time-series: a few-thousand-row employee
roster that is refreshed in place. So there is no rollover family and no ISM
retention policy -- a single concrete index, upserted by EMP_NO (use EMP_NO as
the document _id so re-ingesting the roster overwrites instead of duplicating).

Search design:
  - Exact-identity fields (USER_ID, EMP_NO, EMAIL, RESV014) are `keyword`:
    term-looked-up in filter context, cached, near-instant.
  - Human-readable org/name fields are `keyword` AND copied into `_search_all`
    so a single search box can match them.
  - RESP_CONT -- the freely written job description -- is `text` analyzed with
    `nori` (Korean morphological analysis strips particles/조사 and splits
    compounds, so "검사" matches "검사를"/"장비 검사"). It is also copied into
    `_search_all`.
  - `search_all` is the combined nori field that powers one-box search across
    name + department + part + job + job-description. (No leading underscore:
    OpenSearch reserves `_`-prefixed names for metadata fields.)

Ingest: `ingest_members` bulk-upserts roster rows keyed on EMP_NO, stamping one
KST `os_inserted` per batch ("last refreshed in OS").
"""

import argparse
import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ops_store import OSDoc, OSIndex, create_client, normalize_document

OPENSEARCH_HOST = "skewnono-db1-os.osp01.skhynix.com"
OPENSEARCH_USER = "skewnono001"
OPENSEARCH_PASSWORD = ""

INDEX_NAME = "member_info"

SHARDS = 1
REPLICAS = 1

# EMP_NO is the natural key; ingest should pass it as the document _id so a
# nightly roster refresh upserts each person instead of appending duplicates.
ID_FIELD = "EMP_NO"

# Combined field that one search box queries; the fields below copy into it.
SEARCH_ALL_FIELD = "search_all"

# Exact-identity fields: term lookups / filters only, never full-text.
KEYWORD_FIELDS = ("USER_ID", "EMP_NO", "EMAIL", "RESV014")

# Human-readable fields: exact filter + faceting via keyword, AND fed into the
# combined search box via copy_to.
SEARCHABLE_KEYWORD_FIELDS = (
    "NAME_KOR",
    "DEPT_NAME_KOR",
    "PART_NAME_KO",
    "JOB_NAME_KOR",
    "DEPT_PATH_KOR",
)

# Free-written Korean text: the one true full-text field.
NORI_TEXT_FIELDS = ("RESP_CONT",)

# 8192 chars * 3 bytes (Korean UTF-8) stays under Lucene's 32766-byte term cap.
TEXT_KEYWORD_IGNORE_ABOVE = 8192


def build_mappings() -> dict[str, Any]:
    """Return the explicit mapping for the declared member fields.

    Columns not enumerated here fall through to the dynamic templates:
    `*_tm`/`*_dt` become dates, everything else stays a keyword (member rows
    are mostly codes and short labels, so keyword is the right default).
    """

    properties: dict[str, Any] = {
        SEARCH_ALL_FIELD: {"type": "text", "analyzer": "nori"},
        # KST timestamp refreshed on every upsert; "last refreshed in OS", not
        # "first landed". Does not end in `_tm`/`_dt`, so mapped explicitly.
        "os_inserted": {"type": "date"},
    }

    for field in KEYWORD_FIELDS:
        properties[field] = {"type": "keyword"}

    for field in SEARCHABLE_KEYWORD_FIELDS:
        properties[field] = {
            "type": "keyword",
            "copy_to": SEARCH_ALL_FIELD,
        }

    for field in NORI_TEXT_FIELDS:
        properties[field] = {
            "type": "text",
            "analyzer": "nori",
            "copy_to": SEARCH_ALL_FIELD,
            "fields": {
                "keyword": {
                    "type": "keyword",
                    "ignore_above": TEXT_KEYWORD_IGNORE_ABOVE,
                }
            },
        }

    return {
        "properties": properties,
        "dynamic_templates": [
            {
                "tm_suffix_as_date": {
                    "match_mapping_type": "string",
                    "match": "*_tm",
                    "mapping": {"type": "date"},
                }
            },
            {
                "dt_suffix_as_date": {
                    "match_mapping_type": "string",
                    "match": "*_dt",
                    "mapping": {"type": "date"},
                }
            },
            {
                "strings_as_keyword": {
                    "match_mapping_type": "string",
                    "mapping": {"type": "keyword"},
                }
            },
        ],
    }


def build_index_settings() -> dict[str, Any]:
    """Return the settings for the single member_info index."""

    return {
        "number_of_shards": SHARDS,
        "number_of_replicas": REPLICAS,
    }


def build_index_body() -> dict[str, Any]:
    """Return the create-index body (settings + mappings)."""

    return {
        "settings": build_index_settings(),
        "mappings": build_mappings(),
    }


def has_emp_no(member: Mapping[str, Any]) -> bool:
    """Return True if `member` carries a usable EMP_NO to key the document on.

    Rows without one cannot get a stable `_id`, so they are skipped at ingest
    (`iter_member_actions` filters with this) rather than landing as random-id
    duplicates that a roster refresh could never overwrite.
    """

    value = member.get(ID_FIELD)
    return value is not None and str(value).strip() != ""


def iter_member_actions(
    members: Iterable[Mapping[str, Any]],
    *,
    os_inserted: str,
    op_type: str = "index",
    normalize: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield raw bulk actions for roster rows, keyed on EMP_NO.

    Each row becomes one document whose `_id` is its EMP_NO, so re-ingesting the
    roster overwrites the same person instead of appending a duplicate. Rows
    failing `has_emp_no` are silently skipped. The shared `os_inserted` stamp
    (KST, tz-aware — the caller computes one value for the whole batch) is added
    to `_source`.

    `normalize` defaults to True because the roster arrives as DataFrame rows
    (`df.to_dict("records")`): it runs `normalize_document` to coerce `NaN`/`NaT`
    to None, numpy scalars to native types, and Timestamps to ISO strings — all
    of which are otherwise invalid JSON that breaks the bulk insert. It runs
    *before* the EMP_NO check, so a missing EMP_NO that came in as `NaN`
    (whose `str()` is the non-blank "nan") is correctly skipped. Pass
    `normalize=False` only when the rows are already JSON-clean.

    `op_type` defaults to `"index"` (upsert: overwrite by EMP_NO — the
    roster-refresh semantics); pass `"create"` to instead surface already-present
    EMP_NOs as 409s. Feed the result straight to `OSDoc.bulk`.
    """

    for member in members:
        source = normalize_document(member) if normalize else dict(member)
        if not has_emp_no(source):
            continue
        yield {
            "_op_type": op_type,
            "_index": INDEX_NAME,
            "_id": str(source[ID_FIELD]),
            "_source": {**source, "os_inserted": os_inserted},
        }


def current_kst_stamp() -> str:
    """Return the current KST timestamp as an ISO string (the os_inserted value).

    One run computes a single stamp and shares it between `ingest_members` and
    `prune_stale_members` so the prune boundary lines up exactly with the upsert
    stamp.
    """

    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat()


def ingest_members(
    doc_service: OSDoc,
    members: Iterable[Mapping[str, Any]],
    *,
    refresh: bool = False,
    normalize: bool = True,
    os_inserted: str | None = None,
) -> tuple[int, list[Any]]:
    """Bulk-upsert roster rows into member_info, keyed on EMP_NO.

    Stamps one KST `os_inserted` for the whole batch, builds upsert actions via
    `iter_member_actions`, and bulk-indexes them. Returns `OSDoc.bulk`'s
    `(indexed, errors)`. `refresh` is left off by default so the index's own
    refresh cadence applies; pass `refresh=True` for a small one-off load you
    want searchable immediately.

    `normalize` defaults to True for the DataFrame source (`df.to_dict("records")`):
    it makes `NaN`/`NaT`/numpy/Timestamp cells JSON-safe before they reach the
    bulk API. The whole scheduled extract→update step is therefore one call:

        doc = OSDoc(client=client, index="member_info")
        indexed, errors = ingest_members(doc, df.to_dict("records"))

    Pass `os_inserted` to reuse a stamp from `current_kst_stamp` when a later
    `prune_stale_members` needs the same boundary (or just call
    `refresh_member_directory`, which wires both together).
    """

    stamp = os_inserted or current_kst_stamp()
    return doc_service.bulk(
        iter_member_actions(members, os_inserted=stamp, normalize=normalize),
        refresh=refresh,
    )


def prune_stale_members(
    client: Any,
    *,
    before: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Delete member docs not refreshed at/after `before` (people who left).

    `ingest_members` re-stamps every still-present person with the run's KST
    `os_inserted`. Anyone carrying a strictly older `os_inserted` was absent from
    this run's roster -- i.e. they left -- so a `delete_by_query` on
    `os_inserted < before` removes exactly them. Pass the SAME stamp the ingest
    used as `before`: just-upserted docs carry `os_inserted == before`, which the
    strict `lt` range excludes, so they survive.

    Make the upserts visible before calling this (ingest with `refresh=True`, or
    use `refresh_member_directory`); otherwise the query can still match a
    re-stamped person's pre-refresh `os_inserted` and delete someone still on the
    roster. Returns the raw `delete_by_query` response (`deleted`, `total`, ...).
    """

    body = {"query": {"range": {"os_inserted": {"lt": before}}}}
    return client.delete_by_query(index=INDEX_NAME, body=body, refresh=refresh)


def refresh_member_directory(
    doc_service: OSDoc,
    members: Iterable[Mapping[str, Any]],
    *,
    prune: bool = True,
    normalize: bool = True,
) -> dict[str, Any]:
    """Run one full roster refresh: upsert everyone present, prune everyone gone.

    The scheduled job's single entry point. Computes one KST stamp, upserts all
    rows with it (`ingest_members`), then -- when `prune` -- deletes docs older
    than that exact stamp (`prune_stale_members`), which are the members no
    longer on the roster. The upsert runs with `refresh=True` so the re-stamped
    docs are searchable before the prune; combined with the shared stamp, that is
    what keeps still-present people from being deleted.

    Returns `{"stamp", "indexed", "errors", "pruned"}` (`pruned` is None when
    `prune=False`, else the `delete_by_query` response).
    """

    stamp = current_kst_stamp()
    indexed, errors = ingest_members(
        doc_service, members, os_inserted=stamp, normalize=normalize, refresh=True
    )
    pruned = None
    if prune:
        pruned = prune_stale_members(doc_service.client, before=stamp, refresh=True)
    return {
        "stamp": stamp,
        "indexed": indexed,
        "errors": errors,
        "pruned": pruned,
    }


def create_skewnono_client() -> Any:
    """Create a client for the skewnono OpenSearch cluster."""

    if not OPENSEARCH_PASSWORD:
        raise RuntimeError(
            "Set OPENSEARCH_PASSWORD at the top of "
            "ops_index_mgmt/member_info.py before running this script."
        )

    return create_client(
        host=OPENSEARCH_HOST,
        user=OPENSEARCH_USER,
        password=OPENSEARCH_PASSWORD,
    )


def ensure_member_info_index(client: Any) -> dict[str, Any]:
    """Create the member_info index if it does not already exist."""

    index_service = OSIndex(client=client, index=INDEX_NAME)
    if index_service.exists(INDEX_NAME, include_aliases=False):
        return {"created": False, "index": INDEX_NAME}

    response = index_service.create(
        index=INDEX_NAME,
        settings=build_index_settings(),
        mappings=build_mappings(),
        shards=SHARDS,
        replicas=REPLICAS,
    )
    return {"created": True, "index": INDEX_NAME, "response": response}


def build_dry_run_plan() -> dict[str, Any]:
    """Return the request this script will send without connecting."""

    return {
        "cluster": {
            "host": OPENSEARCH_HOST,
            "user": OPENSEARCH_USER,
            "password_set": bool(OPENSEARCH_PASSWORD),
        },
        "id_field": ID_FIELD,
        "index_request": {
            "method": "PUT",
            "path": f"/{INDEX_NAME}",
            "body": build_index_body(),
        },
    }


def setup_member_info(client: Any | None = None) -> dict[str, Any]:
    """Create the member_info index if it is missing."""

    actual_client = client or create_skewnono_client()
    return {"index": ensure_member_info_index(actual_client)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the member_info directory index with nori full-text on "
            "RESP_CONT and a _search_all combined search field."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the OpenSearch request without connecting to the cluster.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        result = build_dry_run_plan()
    else:
        result = setup_member_info()

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
