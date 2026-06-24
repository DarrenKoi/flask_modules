"""Read-side helpers for the member_info directory index.

`member_info.py` builds the index and `member_info_ingest.py` writes to it; this
module is the third concern -- *reading* it back. The helpers bake in the right
field/method pairing the index was designed around, so callers never have to
remember which field is analyzed text and which is an exact keyword:

  - `search_members` runs a `match` on `search_all` -- the combined nori field
    that name, department, part, job, and job-description all copy into. This is
    the one-box directory search.
  - `members_in_dept` runs a `term` on the exact `DEPT_NAME_KOR` keyword -- a
    browse/filter, not full-text.
  - `get_member` fetches by EMP_NO, which is the document `_id`, so it is a
    single GET (no query) and returns None when nobody matches.

Mirroring `create_member_doc_service` on the write side,
`create_member_search_service()` builds the OSSearch itself so a script can read
the directory in one line without wiring up a client.

Never feed raw user input to `query_string`/`simple_query_string` against this
index (see member_info.py): RESP_CONT is free text full of `-`, `*`, `/`, parens
that those parsers treat as operators. These helpers stay on `match`/`term`,
which run the input through nori and never parse punctuation as syntax.
"""

from typing import Any

from opensearchpy.exceptions import NotFoundError

from ops_store import OSSearch

from ops_index_mgmt.member_info import (
    INDEX_NAME,
    SEARCH_ALL_FIELD,
    create_skewnono_client,
)

# Exact-keyword field a department browse filters on. Lives here (not in
# member_info.py) because it is a query choice, not part of the index shape --
# the index just makes every SEARCHABLE_KEYWORD_FIELD a filterable keyword.
DEPT_FIELD = "DEPT_NAME_KOR"


def create_member_search_service(client: Any | None = None) -> OSSearch:
    """Return an OSSearch bound to the member_info index, ready to query.

    The read-side twin of `create_member_doc_service`: with no `client` it builds
    one via `create_skewnono_client` (which wraps ops_store's `create_client`
    with the skewnono credentials), so a standalone script can search in one
    line --

        search_members(create_member_search_service(), "검사")

    Pass an existing `client` to reuse a connection (e.g. one already opened for
    ingest, or a mock in tests).
    """

    actual_client = client or create_skewnono_client()
    return OSSearch(client=actual_client, index=INDEX_NAME)


def search_members(
    search: OSSearch,
    text: str,
    *,
    size: int = 10,
    as_dataframe: bool = False,
) -> Any:
    """One-box directory search: `match` ``text`` against ``search_all``.

    This is the main entry point. `search_all` is the nori field that name,
    department, part, job, and RESP_CONT all copy into, so a single query spans
    all of them -- and because both sides run through nori, "검사" also matches
    "검사를"/"장비 검사". Returns the raw OpenSearch response (member fields under
    each hit's `_source`); pass `as_dataframe=True` for a flat pandas DataFrame
    of the hits instead.
    """

    result = search.match(SEARCH_ALL_FIELD, text, size=size)
    if as_dataframe:
        return search.to_dataframe(result)
    return result


def members_in_dept(
    search: OSSearch,
    dept: str,
    *,
    size: int = 200,
    as_dataframe: bool = False,
) -> Any:
    """Browse one department: exact `term` on ``DEPT_NAME_KOR``.

    A filter, not full-text -- `DEPT_NAME_KOR` is an exact keyword, so ``dept``
    must be the whole department name byte-for-byte (use `search_members` for
    fuzzy/partial). `size` defaults to 200 to cover a whole team in one page.
    Returns the raw OpenSearch response; pass `as_dataframe=True` for a DataFrame.
    """

    result = search.term(DEPT_FIELD, dept, size=size)
    if as_dataframe:
        return search.to_dataframe(result)
    return result


def get_member(search: OSSearch, emp_no: Any) -> dict[str, Any] | None:
    """Fetch one member by EMP_NO, or None if nobody has that EMP_NO.

    EMP_NO is the document `_id`, so this is a direct GET -- the fastest path,
    no query, no scoring. `emp_no` is coerced to str to match how ingest keys the
    `_id`. Returns the raw GET response (member fields under `_source`); a missing
    EMP_NO yields None rather than raising, mirroring `OSSearch.latest`.
    """

    try:
        return search.client.get(index=INDEX_NAME, id=str(emp_no))
    except NotFoundError:
        return None
