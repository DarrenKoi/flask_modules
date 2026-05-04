"""
Registry of FTP sources for the ftp_ingest topic.

Adding a new source = appending one dict here. No DAG code changes.
In production, swap this hardcoded list for a YAML/JSON loader so
non-developers can edit the registry without a deploy.

Each entry is a fully self-describing job spec:
- name:        unique slug, used as task_id suffix and as a path component
- conn_id:     Airflow Connection that holds host/port/user/password
- remote_path: absolute path on the FTP server
- s3_key:      destination key in object storage (MinIO/S3)
"""

from typing import TypedDict


class FtpSource(TypedDict):
    name: str
    conn_id: str
    remote_path: str
    s3_key: str


# A few examples — production has ~200 entries (load from YAML/Variable).
SOURCES: list[FtpSource] = [
    {
        "name": "vendor_a_orders",
        "conn_id": "ftp_vendor_a",
        "remote_path": "/outbound/orders.csv",
        "s3_key": "raw/vendor_a/orders.csv",
    },
    {
        "name": "vendor_b_inventory",
        "conn_id": "ftp_vendor_b",
        "remote_path": "/exports/inventory.zip",
        "s3_key": "raw/vendor_b/inventory.zip",
    },
    {
        "name": "vendor_c_returns",
        "conn_id": "ftp_vendor_c",
        "remote_path": "/daily/returns.json",
        "s3_key": "raw/vendor_c/returns.json",
    },
]
