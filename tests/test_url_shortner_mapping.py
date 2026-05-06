import unittest
from unittest.mock import MagicMock

from url_shortner.mapping import URLMapping


def _make_service() -> tuple[URLMapping, MagicMock]:
    client = MagicMock()
    service = URLMapping(client=client, database="db", collection="links")
    return service, client


class URLMappingTests(unittest.TestCase):
    def test_create_inserts_document_with_code_as_id(self) -> None:
        service, client = _make_service()
        coll = client["db"]["links"]

        service.create("abc1234", "https://example.com", owner="alice")

        coll.insert_one.assert_called_once()
        document = coll.insert_one.call_args.args[0]
        self.assertEqual(document["_id"], "abc1234")
        self.assertEqual(document["url"], "https://example.com")
        self.assertEqual(document["owner"], "alice")
        self.assertFalse(document["is_custom"])
        self.assertIn("created_at", document)

    def test_create_sets_is_custom_flag(self) -> None:
        service, client = _make_service()
        coll = client["db"]["links"]

        service.create("q3-roadmap", "https://wiki", is_custom=True)
        document = coll.insert_one.call_args.args[0]
        self.assertTrue(document["is_custom"])

    def test_lookup_uses_id_filter(self) -> None:
        service, client = _make_service()
        coll = client["db"]["links"]
        coll.find_one.return_value = {"_id": "abc1234", "url": "https://example.com"}

        result = service.lookup("abc1234")

        coll.find_one.assert_called_once_with({"_id": "abc1234"})
        self.assertEqual(result["url"], "https://example.com")

    def test_list_by_owner_sorts_by_created_at_desc(self) -> None:
        service, client = _make_service()
        coll = client["db"]["links"]
        cursor = MagicMock()
        coll.find.return_value = cursor
        cursor.sort.return_value = cursor
        cursor.limit.return_value = iter([{"_id": "a"}, {"_id": "b"}])

        result = service.list_by_owner("alice", limit=50)

        coll.find.assert_called_once_with({"owner": "alice"})
        cursor.sort.assert_called_once_with("created_at", -1)
        cursor.limit.assert_called_once_with(50)
        self.assertEqual([r["_id"] for r in result], ["a", "b"])

    def test_ensure_indexes_creates_owner_and_created_at(self) -> None:
        service, client = _make_service()
        coll = client["db"]["links"]

        service.ensure_indexes()

        index_calls = [call.args[0] for call in coll.create_index.call_args_list]
        self.assertEqual(index_calls, ["owner", "created_at"])


if __name__ == "__main__":
    unittest.main()
