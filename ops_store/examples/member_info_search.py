"""Standalone, runnable examples for searching the member_info directory index.

Not tests and not imported by anything; a self-contained copy-paste reference
you run against the real cluster. Everything it needs lives in this one file --
the connection, the field/method pairing, and the search helpers -- so it has no
dependency on `ops_index_mgmt` (that package only *builds* indices). The only
import is `ops_store` itself.

The member_info index (built by ops_index_mgmt/member_info.py) is shaped for a
few specific queries, and these helpers bake in the right field for each so a
caller never mismatches nori-analyzed text against an exact keyword:

  - `search_members` runs a `match` on `search_all` -- the combined nori field
    name, department, part, job, and RESP_CONT all copy into. The one-box search.
  - `members_in_dept` runs a `term` on the exact `DEPT_NAME_KOR` keyword -- a
    browse/filter, not full-text.
  - `get_member` fetches by EMP_NO, which is the document `_id`, so it is a
    single GET (no query) and returns None when nobody matches.

Never feed raw user input to `query_string`/`simple_query_string` here: RESP_CONT
is free text full of `-`, `*`, `/`, parens that those parsers treat as operators.
`match`/`term` run the input through nori and never parse punctuation as syntax.

Run order to see anything: the index must already exist and have been ingested.
"""

from typing import Any

from opensearchpy.exceptions import NotFoundError

from ops_store import OSSearch, create_client

# ── connection + index constants — replace with your environment ────────────
OPENSEARCH_HOST = "skewnono-db1-os.osp01.skhynix.com"
OPENSEARCH_USER = "skewnono001"
OPENSEARCH_PASSWORD = ""

INDEX_NAME = "member_info"
SEARCH_ALL_FIELD = "search_all"  # combined nori field every readable field copies into
DEPT_FIELD = "DEPT_NAME_KOR"     # exact keyword a department browse filters on


def create_member_search_service(client: Any | None = None) -> OSSearch:
    """Return an OSSearch bound to member_info, building a client if none given.

    With no `client` it opens one from the constants above (via ops_store's
    `create_client`), so a script can search in one line:

        search_members(create_member_search_service(), "검사")

    Pass an existing `client` to reuse a connection.
    """
    actual_client = client or create_client(
        host=OPENSEARCH_HOST,
        user=OPENSEARCH_USER,
        password=OPENSEARCH_PASSWORD,
    )
    return OSSearch(client=actual_client, index=INDEX_NAME)


def search_members(
    search: OSSearch,
    text: str,
    *,
    size: int = 10,
    as_dataframe: bool = False,
) -> Any:
    """One-box directory search: `match` ``text`` against ``search_all``.

    The main entry point. `search_all` is the nori field name, department, part,
    job, and RESP_CONT all copy into, so a single query spans all of them -- and
    because both sides run through nori, "검사" also matches "검사를"/"장비 검사".
    Returns the raw OpenSearch response (member fields under each hit's
    `_source`); pass `as_dataframe=True` for a flat pandas DataFrame of the hits.
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
    Returns the raw response; pass `as_dataframe=True` for a DataFrame.
    """
    result = search.term(DEPT_FIELD, dept, size=size)
    if as_dataframe:
        return search.to_dataframe(result)
    return result


def get_member(search: OSSearch, emp_no: Any) -> dict[str, Any] | None:
    """Fetch one member by EMP_NO, or None if nobody has that EMP_NO.

    EMP_NO is the document `_id`, so this is a direct GET -- the fastest path, no
    query, no scoring. `emp_no` is coerced to str to match how ingest keys the
    `_id`. Returns the raw GET response (member fields under `_source`); a missing
    EMP_NO yields None rather than raising.
    """
    try:
        return search.client.get(index=INDEX_NAME, id=str(emp_no))
    except NotFoundError:
        return None


def example_one_box_search() -> None:
    """The main case: one search box over name + org + job + job-description."""
    search = create_member_search_service()
    result = search_members(search, "검사", size=20)
    for hit in result["hits"]["hits"]:
        src = hit["_source"]
        print(hit["_score"], src.get("NAME_KOR"), src.get("DEPT_NAME_KOR"))


def example_one_box_search_as_dataframe() -> None:
    """Same one-box search, but get a flat pandas DataFrame of the hits."""
    search = create_member_search_service()
    df = search_members(search, "결함 분석", size=50, as_dataframe=True)
    print(df[["NAME_KOR", "DEPT_NAME_KOR", "JOB_NAME_KOR"]])


def example_lookup_by_emp_no() -> None:
    """Exact fetch of one person by EMP_NO -- a direct GET on the _id."""
    search = create_member_search_service()
    member = get_member(search, "12345")
    if member is None:
        print("no such EMP_NO")
    else:
        print(member["_source"])


def example_browse_a_department() -> None:
    """List a whole team by exact department name (a filter, not full-text)."""
    search = create_member_search_service()
    result = members_in_dept(search, "계측기술팀")
    print("count:", result["hits"]["total"]["value"])
    for hit in result["hits"]["hits"]:
        print(hit["_source"].get("NAME_KOR"))


def example_reuse_one_connection() -> None:
    """Open one client, run several searches on it."""
    client = create_client(
        host=OPENSEARCH_HOST,
        user=OPENSEARCH_USER,
        password=OPENSEARCH_PASSWORD,
    )
    search = create_member_search_service(client)
    print(search_members(search, "검사")["hits"]["total"])
    print(members_in_dept(search, "계측기술팀")["hits"]["total"])
