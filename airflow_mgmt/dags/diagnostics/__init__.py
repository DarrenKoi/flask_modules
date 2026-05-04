"""
Diagnostics topic — DAGs that inspect the Airflow runtime itself.

These never touch business data. They're for answering "what does the
worker have available?" without needing shell access to the cluster.
"""
