"""Worked examples for searching the member_info directory index.

Not tests and not imported by anything; a copy-paste reference you run against
the real skewnono cluster. Each function is one self-contained use case built on
the helpers in `ops_index_mgmt/member_info_search.py`, which themselves wrap
`ops_store.OSSearch` with the right field/method pairing for this index.

Run order to see anything: the index must exist (member_info.setup_member_info)
and have been ingested (member_info_ingest.refresh_member_directory) first.
"""

from ops_index_mgmt.member_info_search import (
    create_member_search_service,
    get_member,
    members_in_dept,
    search_members,
)


def example_one_box_search() -> None:
    """The main case: one search box over name + org + job + job-description.

    `search_all` is the combined nori field every readable field copies into, so
    a single query spans all of them. Nori on both sides means "검사" also matches
    "검사를"/"장비 검사" -- no exact spelling needed.
    """
    search = create_member_search_service()
    result = search_members(search, "검사", size=20)
    for hit in result["hits"]["hits"]:
        src = hit["_source"]
        print(hit["_score"], src.get("NAME_KOR"), src.get("DEPT_NAME_KOR"))


def example_one_box_search_as_dataframe() -> None:
    """Same one-box search, but get a flat pandas DataFrame of the hits.

    Handy in a notebook -- columns are the member fields, one row per hit.
    """
    search = create_member_search_service()
    df = search_members(search, "결함 분석", size=50, as_dataframe=True)
    print(df[["NAME_KOR", "DEPT_NAME_KOR", "JOB_NAME_KOR"]])


def example_lookup_by_emp_no() -> None:
    """Exact fetch of one person by EMP_NO -- a direct GET on the _id.

    No query, no scoring. Returns None when nobody carries that EMP_NO, so guard
    before reading `_source`.
    """
    search = create_member_search_service()
    member = get_member(search, "12345")
    if member is None:
        print("no such EMP_NO")
    else:
        print(member["_source"])


def example_browse_a_department() -> None:
    """List a whole team by exact department name (a filter, not full-text).

    `DEPT_NAME_KOR` is an exact keyword, so pass the full department name
    byte-for-byte. `size` defaults high enough to page a whole team at once.
    """
    search = create_member_search_service()
    result = members_in_dept(search, "계측기술팀")
    print("count:", result["hits"]["total"]["value"])
    for hit in result["hits"]["hits"]:
        print(hit["_source"].get("NAME_KOR"))


def example_reuse_one_connection() -> None:
    """Open one client, run several searches on it.

    `create_member_search_service` builds a client when given none; pass your own
    to reuse a connection across many calls (or to share one already opened for
    ingest).
    """
    from ops_index_mgmt.member_info import create_skewnono_client

    client = create_skewnono_client()
    search = create_member_search_service(client)
    print(search_members(search, "검사")["hits"]["total"])
    print(members_in_dept(search, "계측기술팀")["hits"]["total"])
