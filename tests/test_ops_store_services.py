import importlib.util
import os
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None

from ops_store import (
    OSConfig,
    OSDoc,
    OSIndex,
    create_client,
    load_config,
    normalize_document,
)
from ops_store.search import OSSearch


class FakeDataFrame:
    def __init__(
        self,
        columns: list[str],
        rows: list[tuple[object, ...]],
        *,
        index: list[object] | None = None,
    ) -> None:
        self.columns = columns
        self._rows = rows
        self.index = index or list(range(len(rows)))

    def itertuples(self, index: bool = False, name: str | None = None):
        del index, name
        for row in self._rows:
            yield row

    def __len__(self) -> int:
        return len(self._rows)


class FakeSearchDataFrame:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.columns = list(records[0].keys()) if records else []


class FakePandasModule:
    def DataFrame(self, records: list[dict[str, object]]) -> FakeSearchDataFrame:
        return FakeSearchDataFrame(records)


class OSConfigTests(unittest.TestCase):
    def test_load_config_uses_https_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()

        self.assertEqual(config.port, 443)
        self.assertTrue(config.use_ssl)
        self.assertFalse(config.verify_certs)
        self.assertFalse(config.ssl_show_warn)

    def test_load_config_reads_environment(self) -> None:
        env = {
            "OPENSEARCH_HOST": "cluster.internal",
            "OPENSEARCH_PORT": "9443",
            "OPENSEARCH_USE_SSL": "true",
            "OPENSEARCH_VERIFY_CERTS": "true",
            "OPENSEARCH_BULK_CHUNK": "200",
        }

        with patch.dict(os.environ, env, clear=False):
            config = load_config()

        self.assertEqual(config.host, "cluster.internal")
        self.assertEqual(config.port, 9443)
        self.assertTrue(config.use_ssl)
        self.assertTrue(config.verify_certs)
        self.assertEqual(config.bulk_chunk, 200)

    def test_create_client_uses_generated_kwargs(self) -> None:
        config = OSConfig(host="localhost", port=9200, use_ssl=False)

        with patch("ops_store.base._opensearch_class") as factory:
            mock_class = Mock()
            factory.return_value = mock_class
            create_client(config=config)

        mock_class.assert_called_once()
        kwargs = mock_class.call_args.kwargs
        self.assertEqual(kwargs["hosts"][0]["scheme"], "http")


class OSDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.service = OSDoc(client=self.client, index="articles")

    def test_index_uses_default_index(self) -> None:
        self.client.index.return_value = {"_id": "doc-1", "result": "created"}
        self.service.index({"title": "hello"}, doc_id="doc-1", refresh="wait_for")

        self.client.index.assert_called_once_with(
            index="articles",
            body={"title": "hello"},
            id="doc-1",
            refresh="wait_for",
        )

    def test_upsert_wraps_document_with_doc_as_upsert(self) -> None:
        self.service.upsert("doc-1", {"title": "updated"})

        self.client.update.assert_called_once_with(
            index="articles",
            id="doc-1",
            body={"doc": {"title": "updated"}, "doc_as_upsert": True},
        )

    def test_bulk_index_builds_actions(self) -> None:
        documents = [{"id": "1", "title": "one"}, {"id": "2", "title": "two"}]

        with patch("ops_store.document._bulk_helper") as bulk_helper:
            bulk_function = Mock(return_value=(2, []))
            bulk_helper.return_value = bulk_function
            result = self.service.bulk_index(documents, id_field="id", refresh=True)

        self.assertEqual(result, (2, []))
        actions = list(bulk_function.call_args.args[1])
        self.assertEqual(
            actions,
            [
                {"_index": "articles", "_source": {"id": "1", "title": "one"}, "_id": "1"},
                {"_index": "articles", "_source": {"id": "2", "title": "two"}, "_id": "2"},
            ],
        )
        self.assertEqual(bulk_function.call_args.kwargs["chunk_size"], 500)
        self.assertTrue(bulk_function.call_args.kwargs["refresh"])

    def test_bulk_index_normalize_makes_documents_json_safe(self) -> None:
        documents = [
            {
                "id": 7,
                "created_at": datetime(2024, 4, 21, 10, 30, 0),
                "published_on": date(2024, 4, 21),
                "publish_time": time(10, 30, 0),
                "elapsed": timedelta(seconds=90),
                "price": Decimal("12.50"),
                "score": float("nan"),
                "meta": {"batch": Decimal("3.25")},
            }
        ]

        with patch("ops_store.document._bulk_helper") as bulk_helper:
            bulk_function = Mock(return_value=(1, []))
            bulk_helper.return_value = bulk_function
            self.service.bulk_index(documents, id_field="id", normalize=True)

        actions = list(bulk_function.call_args.args[1])
        self.assertEqual(
            actions,
            [
                {
                    "_index": "articles",
                    "_id": 7,
                    "_source": {
                        "id": 7,
                        "created_at": "2024-04-21T10:30:00",
                        "published_on": "2024-04-21",
                        "publish_time": "10:30:00",
                        "elapsed": 90.0,
                        "price": 12.5,
                        "score": None,
                        "meta": {"batch": 3.25},
                    },
                }
            ],
        )

    def test_bulk_index_dataframe_streams_rows_and_sets_ids(self) -> None:
        dataframe = FakeDataFrame(
            ["doc_id", "title", "created_at", "score"],
            [
                (101, "one", datetime(2024, 4, 21, 8, 0, 0), 1.5),
                (102, "two", datetime(2024, 4, 21, 9, 0, 0), float("nan")),
            ],
        )

        with patch("ops_store.document._bulk_helper") as bulk_helper:
            bulk_function = Mock(return_value=(2, []))
            bulk_helper.return_value = bulk_function
            result = self.service.bulk_index_dataframe(
                dataframe,
                id_field="doc_id",
                refresh=True,
            )

        self.assertEqual(result, (2, []))
        actions = list(bulk_function.call_args.args[1])
        self.assertEqual(
            actions,
            [
                {
                    "_index": "articles",
                    "_id": "101",
                    "_source": {
                        "doc_id": 101,
                        "title": "one",
                        "created_at": "2024-04-21T08:00:00",
                        "score": 1.5,
                    },
                },
                {
                    "_index": "articles",
                    "_id": "102",
                    "_source": {
                        "doc_id": 102,
                        "title": "two",
                        "created_at": "2024-04-21T09:00:00",
                        "score": None,
                    },
                },
            ],
        )
        self.assertEqual(bulk_function.call_args.kwargs["chunk_size"], 500)
        self.assertTrue(bulk_function.call_args.kwargs["refresh"])

    def test_bulk_index_dataframe_sets_op_type_when_given(self) -> None:
        dataframe = FakeDataFrame(
            ["doc_id", "title"],
            [(101, "one"), (102, "two")],
        )

        with patch("ops_store.document._bulk_helper") as bulk_helper:
            bulk_function = Mock(return_value=(2, []))
            bulk_helper.return_value = bulk_function
            self.service.bulk_index_dataframe(
                dataframe,
                id_field="doc_id",
                op_type="create",
            )

        actions = list(bulk_function.call_args.args[1])
        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertEqual(action["_op_type"], "create")


@unittest.skipUnless(_HAS_PANDAS, "pandas not installed")
class OSDocDataFrameDatetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.service = OSDoc(client=self.client, index="events")

    def test_bulk_index_dataframe_normalizes_datetime64_columns(self) -> None:
        import pandas as pd

        dataframe = pd.DataFrame(
            {
                "doc_id": ["a", "b", "c"],
                "naive_ts": pd.to_datetime(
                    ["2024-04-21 08:00:00", "2024-04-21 09:00:00", None]
                ),
                "tz_ts": pd.to_datetime(
                    ["2024-04-21 08:00:00+09:00", None, "2024-04-21 10:00:00+09:00"],
                    utc=False,
                ),
            }
        )
        self.assertEqual(dataframe["naive_ts"].dtype.name, "datetime64[ns]")

        with patch("ops_store.document._bulk_helper") as bulk_helper:
            bulk_function = Mock(return_value=(3, []))
            bulk_helper.return_value = bulk_function
            self.service.bulk_index_dataframe(dataframe, id_field="doc_id")

        actions = list(bulk_function.call_args.args[1])
        sources = [action["_source"] for action in actions]

        self.assertEqual(sources[0]["naive_ts"], "2024-04-21T08:00:00")
        self.assertEqual(sources[1]["naive_ts"], "2024-04-21T09:00:00")
        self.assertIsNone(sources[2]["naive_ts"])

        self.assertEqual(sources[0]["tz_ts"], "2024-04-21T08:00:00+09:00")
        self.assertIsNone(sources[1]["tz_ts"])
        self.assertEqual(sources[2]["tz_ts"], "2024-04-21T10:00:00+09:00")


class OSDocumentNormalizationTests(unittest.TestCase):
    def test_normalize_document_converts_nested_values(self) -> None:
        document = {
            "created_at": datetime(2024, 4, 21, 10, 30, 0),
            "price": Decimal("4.50"),
            "missing": float("nan"),
            "nested": {1: date(2024, 4, 21)},
            "values": [time(11, 15, 0), timedelta(minutes=3)],
        }

        normalized = normalize_document(document)

        self.assertEqual(
            normalized,
            {
                "created_at": "2024-04-21T10:30:00",
                "price": 4.5,
                "missing": None,
                "nested": {"1": "2024-04-21"},
                "values": ["11:15:00", 180.0],
            },
        )


class OSIndexTests(unittest.TestCase):
    def test_exists_checks_aliases_by_default(self) -> None:
        client = Mock()
        client.indices.exists.return_value = False
        client.indices.exists_alias.return_value = True
        service = OSIndex(client=client, index="logs")

        result = service.exists()

        self.assertTrue(result)
        self.assertEqual(
            client.indices.method_calls,
            [
                unittest.mock.call.exists(index="logs"),
                unittest.mock.call.exists_alias(name="logs"),
            ],
        )

    def test_alias_exists_uses_alias_api(self) -> None:
        client = Mock()
        client.indices.exists_alias.return_value = True
        service = OSIndex(client=client, index="logs")

        result = service.alias_exists()

        self.assertTrue(result)
        client.indices.exists_alias.assert_called_once_with(name="logs")

    def test_create_applies_default_settings(self) -> None:
        client = Mock()
        service = OSIndex(client=client, index="logs")

        service.create(mappings={"properties": {"title": {"type": "text"}}})

        client.indices.create.assert_called_once()
        kwargs = client.indices.create.call_args.kwargs
        self.assertEqual(kwargs["index"], "logs")
        self.assertEqual(kwargs["body"]["settings"]["number_of_shards"], 1)
        self.assertIn("mappings", kwargs["body"])

    def test_create_can_attach_aliases(self) -> None:
        client = Mock()
        service = OSIndex(client=client, index="logs-000001")

        service.create(
            aliases={
                "logs": {"is_write_index": True},
            }
        )

        kwargs = client.indices.create.call_args.kwargs
        self.assertEqual(
            kwargs["body"]["aliases"],
            {
                "logs": {"is_write_index": True},
            },
        )

    def test_recreate_index_deletes_existing_index_before_creating(self) -> None:
        client = Mock()
        client.indices.exists.return_value = True
        service = OSIndex(client=client, index="logs")

        service.recreate_index(
            "logs",
            shards=3,
            replica=1,
            mappings={"properties": {"title": {"type": "text"}}},
            aliases={"logs": {"is_write_index": True}},
        )

        self.assertEqual(
            client.indices.method_calls,
            [
                unittest.mock.call.exists(index="logs"),
                unittest.mock.call.delete(index="logs"),
                unittest.mock.call.create(
                    index="logs",
                    body={
                        "settings": {
                            "number_of_shards": 3,
                            "number_of_replicas": 1,
                            "refresh_interval": "30s",
                        },
                        "mappings": {"properties": {"title": {"type": "text"}}},
                        "aliases": {"logs": {"is_write_index": True}},
                    },
                ),
            ],
        )

    def test_recreate_index_creates_when_index_does_not_exist(self) -> None:
        client = Mock()
        client.indices.exists.return_value = False
        service = OSIndex(client=client, index="logs")

        service.recreate_index("logs")

        client.indices.delete.assert_not_called()
        client.indices.create.assert_called_once_with(
            index="logs",
            body={
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "refresh_interval": "30s",
                }
            },
        )

    def test_recreate_index_does_not_delete_alias_only_match(self) -> None:
        client = Mock()
        client.indices.exists.return_value = False
        client.indices.exists_alias.return_value = True
        service = OSIndex(client=client, index="logs")

        service.recreate_index("logs")

        client.indices.delete.assert_not_called()
        client.indices.create.assert_called_once_with(
            index="logs",
            body={
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "refresh_interval": "30s",
                }
            },
        )
        self.assertEqual(
            client.indices.method_calls,
            [
                unittest.mock.call.exists(index="logs"),
                unittest.mock.call.create(
                    index="logs",
                    body={
                        "settings": {
                            "number_of_shards": 1,
                            "number_of_replicas": 0,
                            "refresh_interval": "30s",
                        }
                    },
                ),
            ],
        )

    def test_describe_summarizes_alias_backing_indices_and_rollover(self) -> None:
        client = Mock()
        client.indices.exists.return_value = False
        client.indices.exists_alias.return_value = True
        client.indices.get_alias.side_effect = [
            {
                "logs-000001": {"aliases": {"logs": {"is_write_index": False}}},
                "logs-000002": {"aliases": {"logs": {"is_write_index": True}}},
            },
            {
                "logs-000001": {
                    "aliases": {
                        "logs": {"is_write_index": False},
                        "logs-search": {},
                    }
                },
                "logs-000002": {
                    "aliases": {
                        "logs": {"is_write_index": True},
                        "logs-search": {},
                    }
                },
            },
        ]
        service = OSIndex(client=client, index="logs")

        description = service.describe()

        self.assertEqual(
            description,
            {
                "name": "logs",
                "exists": True,
                "resource_type": "alias",
                "is_index": False,
                "is_alias": True,
                "backing_indices": ["logs-000001", "logs-000002"],
                "aliases": {
                    "logs": {
                        "backing_indices": ["logs-000001", "logs-000002"],
                        "write_index": "logs-000002",
                    },
                    "logs-search": {
                        "backing_indices": ["logs-000001", "logs-000002"],
                        "write_index": None,
                    },
                },
                "searchable_names": [
                    "logs",
                    "logs-000001",
                    "logs-000002",
                    "logs-search",
                ],
                "rollover": {
                    "alias": "logs",
                    "backing_indices": ["logs-000001", "logs-000002"],
                    "write_index": "logs-000002",
                    "ready": True,
                    "uses_numbered_suffix": True,
                },
            },
        )
        self.assertEqual(
            client.indices.method_calls,
            [
                unittest.mock.call.exists(index="logs"),
                unittest.mock.call.exists_alias(name="logs"),
                unittest.mock.call.get_alias(name="logs"),
                unittest.mock.call.get_alias(index="logs-000001,logs-000002"),
            ],
        )

    def test_describe_can_include_raw_metadata(self) -> None:
        client = Mock()
        client.indices.exists.return_value = True
        client.indices.get.return_value = {
            "logs-000002": {
                "aliases": {"logs": {"is_write_index": True}},
                "settings": {"index": {"number_of_replicas": "0"}},
                "mappings": {"properties": {"title": {"type": "text"}}},
            }
        }
        service = OSIndex(client=client, index="logs-000002")

        description = service.describe(include_metadata=True)

        self.assertEqual(description["resource_type"], "index")
        self.assertEqual(description["backing_indices"], ["logs-000002"])
        self.assertEqual(description["rollover"]["alias"], "logs")
        self.assertEqual(description["rollover"]["write_index"], "logs-000002")
        self.assertEqual(
            description["metadata"],
            {
                "logs-000002": {
                    "aliases": {"logs": {"is_write_index": True}},
                    "settings": {"index": {"number_of_replicas": "0"}},
                    "mappings": {"properties": {"title": {"type": "text"}}},
                }
            },
        )

    def test_rollover_uses_alias_and_conditions(self) -> None:
        client = Mock()
        service = OSIndex(client=client, index="logs")

        service.rollover(
            conditions={"max_docs": 1000000},
            new_index="logs-000002",
            dry_run=True,
        )

        client.indices.rollover.assert_called_once_with(
            alias="logs",
            new_index="logs-000002",
            body={"conditions": {"max_docs": 1000000}},
            params={"dry_run": True},
        )

    def test_reindex_from_remote_builds_body_and_params(self) -> None:
        client = Mock()
        service = OSIndex(client=client, index="logs_clustered")

        service.reindex_from_remote(
            source_host="https://standalone:9200",
            source_index="logs",
            source_username="admin",
            source_password="secret",
            query={"range": {"@timestamp": {"gte": "now-7d"}}},
        )

        client.reindex.assert_called_once_with(
            body={
                "conflicts": "abort",
                "source": {
                    "remote": {
                        "host": "https://standalone:9200",
                        "socket_timeout": "1m",
                        "connect_timeout": "10s",
                        "username": "admin",
                        "password": "secret",
                    },
                    "index": "logs",
                    "size": 1000,
                    "query": {"range": {"@timestamp": {"gte": "now-7d"}}},
                },
                "dest": {"index": "logs_clustered", "op_type": "index"},
            },
            params={
                "wait_for_completion": False,
                "slices": "auto",
                "refresh": False,
            },
        )

    def test_reindex_from_remote_overrides_dest_and_throttles(self) -> None:
        client = Mock()
        service = OSIndex(client=client, index="default_dest")

        service.reindex_from_remote(
            source_host="http://src:9200",
            source_index=["logs-2026-04-22", "logs-2026-04-23"],
            dest_index="logs_new",
            size=500,
            op_type="create",
            conflicts="proceed",
            slices=6,
            refresh=True,
            wait_for_completion=True,
            requests_per_second=2000.0,
        )

        body = client.reindex.call_args.kwargs["body"]
        params = client.reindex.call_args.kwargs["params"]
        self.assertEqual(body["conflicts"], "proceed")
        self.assertEqual(
            body["source"]["index"],
            ["logs-2026-04-22", "logs-2026-04-23"],
        )
        self.assertEqual(body["source"]["size"], 500)
        self.assertNotIn("username", body["source"]["remote"])
        self.assertNotIn("query", body["source"])
        self.assertEqual(body["dest"], {"index": "logs_new", "op_type": "create"})
        self.assertEqual(
            params,
            {
                "wait_for_completion": True,
                "slices": 6,
                "refresh": True,
                "requests_per_second": 2000.0,
            },
        )


class OSSearchTests(unittest.TestCase):
    def test_match_works_with_alias_default_index(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="knowledge_alias")

        service.match("title", "flask", size=5)

        client.search.assert_called_once_with(
            index="knowledge_alias",
            body={"query": {"match": {"title": "flask"}}, "size": 5},
        )

    def test_match_can_override_alias_with_concrete_index(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="knowledge_alias")

        service.match("title", "flask", index="knowledge-000001", size=5)

        client.search.assert_called_once_with(
            index="knowledge-000001",
            body={"query": {"match": {"title": "flask"}}, "size": 5},
        )

    def test_match_builds_query_body(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="knowledge")

        service.match("title", "flask", size=5)

        client.search.assert_called_once_with(
            index="knowledge",
            body={"query": {"match": {"title": "flask"}}, "size": 5},
        )

    def test_filter_terms_ands_fields_by_default(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.filter_terms(
            {
                "category.keyword": ["books", "movies"],
                "status": ["published", "featured"],
            },
            size=25,
        )

        client.search.assert_called_once_with(
            index="events",
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"terms": {"category.keyword": ["books", "movies"]}},
                            {"terms": {"status": ["published", "featured"]}},
                        ]
                    }
                },
                "size": 25,
            },
        )

    def test_filter_terms_with_minimum_should_match_relaxes_to_should(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.filter_terms(
            {
                "category.keyword": ["books"],
                "status": ["published"],
                "tag": ["hot"],
            },
            minimum_should_match=2,
        )

        client.search.assert_called_once_with(
            index="events",
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"category.keyword": ["books"]}},
                                        {"terms": {"status": ["published"]}},
                                        {"terms": {"tag": ["hot"]}},
                                    ],
                                    "minimum_should_match": 2,
                                }
                            }
                        ]
                    }
                },
                "size": 10,
            },
        )

    def test_filter_terms_skips_empty_value_lists(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.filter_terms(
            {
                "category.keyword": ["books"],
                "status": [],
            }
        )

        body = client.search.call_args.kwargs["body"]
        self.assertEqual(
            body["query"]["bool"]["filter"],
            [{"terms": {"category.keyword": ["books"]}}],
        )

    def test_filter_terms_combines_with_additional_query(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.filter_terms(
            {"status": ["published"]},
            query={"match": {"title": "flask"}},
        )

        body = client.search.call_args.kwargs["body"]
        self.assertEqual(
            body["query"]["bool"]["must"], [{"match": {"title": "flask"}}]
        )
        self.assertEqual(
            body["query"]["bool"]["filter"],
            [{"terms": {"status": ["published"]}}],
        )

    def test_latest_sorts_desc_on_time_field(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {"mappings": {"properties": {"event_tm": {"type": "date"}}}}
        }
        client.search.return_value = {
            "hits": {"hits": [{"_id": "doc-9", "_source": {"event_tm": "2026-04-23T10:00:00Z"}}]}
        }
        service = OSSearch(client=client, index="events")

        result = service.latest("event_tm")

        self.assertEqual(result["hits"]["hits"][0]["_id"], "doc-9")
        client.indices.get_mapping.assert_called_once_with(index="events")
        client.search.assert_called_once_with(
            index="events",
            body={"sort": [{"event_tm": {"order": "desc"}}], "size": 1},
        )

    def test_latest_accepts_date_nanos_field(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {"mappings": {"properties": {"event_tm": {"type": "date_nanos"}}}}
        }
        service = OSSearch(client=client, index="events")

        service.latest("event_tm")

        client.search.assert_called_once()

    def test_latest_raises_when_field_is_not_a_date(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {"mappings": {"properties": {"event_tm": {"type": "keyword"}}}}
        }
        service = OSSearch(client=client, index="events")

        with self.assertRaises(ValueError) as ctx:
            service.latest("event_tm")

        self.assertIn("keyword", str(ctx.exception))
        client.search.assert_not_called()

    def test_latest_raises_when_field_missing_from_mapping(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {"mappings": {"properties": {"other_tm": {"type": "date"}}}}
        }
        service = OSSearch(client=client, index="events")

        with self.assertRaises(ValueError):
            service.latest("event_tm")

        client.search.assert_not_called()

    def test_latest_validates_every_backing_index_behind_alias(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events-000001": {
                "mappings": {"properties": {"event_tm": {"type": "date"}}}
            },
            "events-000002": {
                "mappings": {"properties": {"event_tm": {"type": "keyword"}}}
            },
        }
        service = OSSearch(client=client, index="events_alias")

        with self.assertRaises(ValueError) as ctx:
            service.latest("event_tm")

        self.assertIn("events-000002", str(ctx.exception))
        client.search.assert_not_called()

    def test_latest_resolves_nested_field_path(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {
                "mappings": {
                    "properties": {
                        "event": {"properties": {"tm": {"type": "date"}}}
                    }
                }
            }
        }
        service = OSSearch(client=client, index="events")

        service.latest("event.tm")

        client.search.assert_called_once_with(
            index="events",
            body={"sort": [{"event.tm": {"order": "desc"}}], "size": 1},
        )

    def test_latest_applies_query_and_size(self) -> None:
        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {"mappings": {"properties": {"event_tm": {"type": "date"}}}}
        }
        service = OSSearch(client=client, index="events")

        service.latest(
            "event_tm",
            size=5,
            query={"term": {"user_id": "u-1"}},
        )

        client.search.assert_called_once_with(
            index="events",
            body={
                "sort": [{"event_tm": {"order": "desc"}}],
                "size": 5,
                "query": {"term": {"user_id": "u-1"}},
            },
        )

    def test_latest_returns_none_when_mapping_index_missing(self) -> None:
        from opensearchpy.exceptions import NotFoundError

        client = Mock()
        client.indices.get_mapping.side_effect = NotFoundError(
            404, "index_not_found_exception", {}
        )
        service = OSSearch(client=client, index="events")

        self.assertIsNone(service.latest("event_tm"))
        client.search.assert_not_called()

    def test_latest_returns_none_when_search_index_missing(self) -> None:
        from opensearchpy.exceptions import NotFoundError

        client = Mock()
        client.indices.get_mapping.return_value = {
            "events": {"mappings": {"properties": {"event_tm": {"type": "date"}}}}
        }
        client.search.side_effect = NotFoundError(
            404, "index_not_found_exception", {}
        )
        service = OSSearch(client=client, index="events")

        self.assertIsNone(service.latest("event_tm"))

    def test_sample_uses_random_score_with_match_all(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.sample()

        client.search.assert_called_once_with(
            index="events",
            body={
                "size": 10,
                "query": {
                    "function_score": {
                        "query": {"match_all": {}},
                        "random_score": {},
                    }
                },
            },
        )

    def test_sample_with_seed_includes_seq_no_field(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.sample(size=5, seed=42)

        client.search.assert_called_once_with(
            index="events",
            body={
                "size": 5,
                "query": {
                    "function_score": {
                        "query": {"match_all": {}},
                        "random_score": {"seed": 42, "field": "_seq_no"},
                    }
                },
            },
        )

    def test_sample_applies_caller_query(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="events")

        service.sample(size=3, query={"term": {"status": "ok"}})

        client.search.assert_called_once_with(
            index="events",
            body={
                "size": 3,
                "query": {
                    "function_score": {
                        "query": {"term": {"status": "ok"}},
                        "random_score": {},
                    }
                },
            },
        )

    def test_unique_values_returns_terms_bucket_keys(self) -> None:
        client = Mock()
        client.search.return_value = {
            "aggregations": {
                "unique_values": {
                    "buckets": [
                        {"key": "alpha", "doc_count": 3},
                        {"key": "beta", "doc_count": 1},
                    ]
                }
            }
        }
        service = OSSearch(client=client, index="knowledge")

        values = service.unique_values("category.keyword", size=500)

        self.assertEqual(values, ["alpha", "beta"])
        client.search.assert_called_once_with(
            index="knowledge",
            body={
                "aggs": {
                    "unique_values": {
                        "terms": {"field": "category.keyword", "size": 500}
                    }
                },
                "size": 0,
            },
        )

    def test_to_dataframe_uses_source_fields_by_default(self) -> None:
        service = OSSearch(client=Mock(), index="knowledge")
        result = {
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_index": "knowledge",
                        "_score": 1.25,
                        "_source": {"title": "Flask Guide", "status": "published"},
                    },
                    {
                        "_id": "doc-2",
                        "_index": "knowledge",
                        "_score": 0.9,
                        "_source": {"title": "FastAPI Guide", "status": "draft"},
                    },
                ]
            }
        }

        with patch("ops_store.search._pandas_module", return_value=FakePandasModule()):
            dataframe = service.to_dataframe(result)

        self.assertEqual(
            dataframe.records,
            [
                {"title": "Flask Guide", "status": "published"},
                {"title": "FastAPI Guide", "status": "draft"},
            ],
        )
        self.assertEqual(dataframe.columns, ["title", "status"])

    def test_to_dataframe_can_include_hit_metadata(self) -> None:
        service = OSSearch(client=Mock(), index="knowledge")
        result = {
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_index": "knowledge",
                        "_score": 1.25,
                        "_source": {"title": "Flask Guide"},
                    }
                ]
            }
        }

        with patch("ops_store.search._pandas_module", return_value=FakePandasModule()):
            dataframe = service.to_dataframe(result, include_meta=True)

        self.assertEqual(
            dataframe.records,
            [
                {
                    "title": "Flask Guide",
                    "_id": "doc-1",
                    "_index": "knowledge",
                    "_score": 1.25,
                }
            ],
        )

    def test_match_dataframe_returns_dataframe_from_search_hits(self) -> None:
        client = Mock()
        client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_index": "knowledge",
                        "_score": 1.25,
                        "_source": {"title": "Flask Guide", "status": "published"},
                    }
                ]
            }
        }
        service = OSSearch(client=client, index="knowledge")

        with patch("ops_store.search._pandas_module", return_value=FakePandasModule()):
            dataframe = service.match_dataframe(
                "title",
                "flask",
                size=5,
                include_meta=True,
            )

        client.search.assert_called_once_with(
            index="knowledge",
            body={"query": {"match": {"title": "flask"}}, "size": 5},
        )
        self.assertEqual(
            dataframe.records,
            [
                {
                    "title": "Flask Guide",
                    "status": "published",
                    "_id": "doc-1",
                    "_index": "knowledge",
                    "_score": 1.25,
                }
            ],
        )

    def test_search_dataframe_all_collects_scroll_pages(self) -> None:
        client = Mock()
        client.search.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_index": "knowledge",
                        "_score": 1.25,
                        "_source": {"title": "Flask Guide", "status": "published"},
                    }
                ]
            },
        }
        client.scroll.side_effect = [
            {
                "_scroll_id": "scroll-1",
                "hits": {
                    "hits": [
                        {
                            "_id": "doc-2",
                            "_index": "knowledge",
                            "_score": 0.9,
                            "_source": {"title": "FastAPI Guide", "status": "draft"},
                        }
                    ]
                },
            },
            {
                "_scroll_id": "scroll-1",
                "hits": {"hits": []},
            },
        ]
        service = OSSearch(client=client, index="knowledge")

        with patch("ops_store.search._pandas_module", return_value=FakePandasModule()):
            dataframe = service.search_dataframe_all(
                {"query": {"match_all": {}}},
                batch_size=1,
            )

        client.search.assert_called_once_with(
            index="knowledge",
            body={"query": {"match_all": {}}, "size": 1},
            scroll="2m",
        )
        self.assertEqual(client.scroll.call_count, 2)
        client.clear_scroll.assert_called_once_with(scroll_id="scroll-1")
        self.assertEqual(
            dataframe.records,
            [
                {"title": "Flask Guide", "status": "published"},
                {"title": "FastAPI Guide", "status": "draft"},
            ],
        )

    def test_match_dataframe_all_builds_match_query_and_includes_meta(self) -> None:
        client = Mock()
        client.search.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_index": "knowledge",
                        "_score": 1.25,
                        "_source": {"title": "Flask Guide", "status": "published"},
                    }
                ]
            },
        }
        client.scroll.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {"hits": []},
        }
        service = OSSearch(client=client, index="knowledge")

        with patch("ops_store.search._pandas_module", return_value=FakePandasModule()):
            dataframe = service.match_dataframe_all(
                "title",
                "flask",
                batch_size=50,
                include_meta=True,
            )

        client.search.assert_called_once_with(
            index="knowledge",
            body={"query": {"match": {"title": "flask"}}, "size": 50},
            scroll="2m",
        )
        self.assertEqual(
            dataframe.records,
            [
                {
                    "title": "Flask Guide",
                    "status": "published",
                    "_id": "doc-1",
                    "_index": "knowledge",
                    "_score": 1.25,
                }
            ],
        )
        client.clear_scroll.assert_called_once_with(scroll_id="scroll-1")

    def test_search_dataframe_all_clears_scroll_on_error(self) -> None:
        client = Mock()
        client.search.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_index": "knowledge",
                        "_score": 1.25,
                        "_source": {"title": "Flask Guide"},
                    }
                ]
            },
        }
        client.scroll.side_effect = RuntimeError("scroll failed")
        service = OSSearch(client=client, index="knowledge")

        with patch("ops_store.search._pandas_module", return_value=FakePandasModule()):
            with self.assertRaisesRegex(RuntimeError, "scroll failed"):
                service.search_dataframe_all(
                    {"query": {"match_all": {}}},
                    batch_size=1,
                )

        client.clear_scroll.assert_called_once_with(scroll_id="scroll-1")

    def test_search_dataframe_all_requires_pandas_before_search(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="knowledge")

        with patch("ops_store.search._pandas_module", return_value=None):
            with self.assertRaises(ImportError):
                service.search_dataframe_all({"query": {"match_all": {}}})

        client.search.assert_not_called()

    def test_to_dataframe_requires_pandas(self) -> None:
        service = OSSearch(client=Mock(), index="knowledge")

        with patch("ops_store.search._pandas_module", return_value=None):
            with self.assertRaises(ImportError):
                service.to_dataframe({"hits": {"hits": []}})


if __name__ == "__main__":
    unittest.main()
