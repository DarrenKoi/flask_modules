import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from logging import NullHandler, StreamHandler
from pathlib import Path
from unittest.mock import Mock, patch

import ops_store.logging as os_logging
from ops_store import (
    OSConfig,
    OSDoc,
    OSIndex,
    configure_logging,
    create_client,
    load_config,
    normalize_document,
)
from ops_store.logging import DEFAULT_LOG_DIR, PIDFileHandler, get_logger, summarize_result
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

    def tearDown(self) -> None:
        for logger_name in (
            "opensearch.test_config",
            "opensearch.test_handler",
            "opensearch.test_file",
        ):
            logger = get_logger(logger_name.removeprefix("opensearch."))
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

    def test_configure_logging_uses_propagation_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = configure_logging(
                name="test_config",
                level="INFO",
                log_dir=tmp_dir,
            )

            self.assertEqual(logger.name, "opensearch.test_config")
            self.assertTrue(logger.propagate)
            self.assertFalse(any(type(handler) is StreamHandler for handler in logger.handlers))
            self.assertTrue(any(isinstance(handler, PIDFileHandler) for handler in logger.handlers))

    def test_configure_logging_can_add_stream_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = configure_logging(
                name="test_handler",
                level="INFO",
                log_dir=tmp_dir,
                add_handler=True,
                propagate=False,
            )

            self.assertFalse(logger.propagate)
            self.assertTrue(any(type(handler) is StreamHandler for handler in logger.handlers))
            self.assertTrue(any(isinstance(handler, PIDFileHandler) for handler in logger.handlers))
            self.assertFalse(any(isinstance(handler, NullHandler) for handler in logger.handlers))

    def test_configure_logging_writes_under_requested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = configure_logging(
                name="test_file",
                level="INFO",
                log_dir=tmp_dir,
                propagate=False,
            )

            logger.info("hello opensearch")

            expected_path = Path(tmp_dir) / f"opensearch.{os.getpid()}.log"
            self.assertTrue(expected_path.exists())
            self.assertTrue(any(isinstance(handler, PIDFileHandler) for handler in logger.handlers))
            self.assertIn("hello opensearch", expected_path.read_text(encoding="utf-8"))

    def test_default_log_dir_targets_project_root(self) -> None:
        self.assertEqual(
            DEFAULT_LOG_DIR,
            Path(os_logging.__file__).resolve().parent.parent / "logs" / "opensearch",
        )


class OSDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.service = OSDoc(client=self.client, index="articles")

    def test_index_uses_default_index(self) -> None:
        self.client.index.return_value = {"_id": "doc-1", "result": "created"}
        self.service.logger = Mock()
        self.service.index({"title": "hello"}, doc_id="doc-1", refresh="wait_for")

        self.client.index.assert_called_once_with(
            index="articles",
            body={"title": "hello"},
            id="doc-1",
            refresh="wait_for",
        )
        self.service.logger.info.assert_called_once()

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
            self.service.logger = Mock()
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
        self.service.logger.info.assert_called_once()

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
            self.service.logger = Mock()
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
        self.service.logger.info.assert_called_once()

    def test_bulk_index_dataframe_can_use_dataframe_index_as_id(self) -> None:
        dataframe = FakeDataFrame(
            ["title"],
            [("one",), ("two",)],
            index=["a-1", "a-2"],
        )

        with patch("ops_store.document._bulk_helper") as bulk_helper:
            bulk_function = Mock(return_value=(2, []))
            bulk_helper.return_value = bulk_function
            self.service.bulk_index_dataframe(dataframe, id_from_index=True)

        actions = list(bulk_function.call_args.args[1])
        self.assertEqual(actions[0]["_id"], "a-1")
        self.assertEqual(actions[1]["_id"], "a-2")

    def test_bulk_index_dataframe_rejects_two_id_sources(self) -> None:
        dataframe = FakeDataFrame(["doc_id"], [(1,)])

        with self.assertRaises(ValueError):
            self.service.bulk_index_dataframe(
                dataframe,
                id_field="doc_id",
                id_from_index=True,
            )


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


class OSLoggingTests(unittest.TestCase):
    def test_summarize_result_reduces_search_payload(self) -> None:
        result = summarize_result(
            {
                "took": 2,
                "timed_out": False,
                "hits": {"total": {"value": 3, "relation": "eq"}},
                "aggregations": {"by_tag": {"buckets": []}},
            }
        )

        self.assertEqual(
            result,
            {
                "took": 2,
                "timed_out": False,
                "hits_total": 3,
                "aggregations": ["by_tag"],
            },
        )


class OSIndexTests(unittest.TestCase):
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

    def test_rollover_uses_alias_and_conditions(self) -> None:
        client = Mock()
        service = OSIndex(client=client, index="logs")
        service.logger = Mock()

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
        service.logger.info.assert_called_once()


class OSSearchTests(unittest.TestCase):
    def test_match_builds_query_body(self) -> None:
        client = Mock()
        service = OSSearch(client=client, index="knowledge")

        service.match("title", "flask", size=5)

        client.search.assert_called_once_with(
            index="knowledge",
            body={"query": {"match": {"title": "flask"}}, "size": 5},
        )


if __name__ == "__main__":
    unittest.main()
