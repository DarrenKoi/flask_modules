import os
import unittest
from unittest.mock import Mock, call, patch

from ops_index_mgmt import hitachi_sem_msr_info as mgmt


class SemMsrInfoIndexMgmtTests(unittest.TestCase):
    def test_build_index_settings_uses_requested_cluster_shape(self) -> None:
        settings = mgmt.build_index_settings("cdsem_msr_info")

        self.assertEqual(settings["number_of_shards"], 3)
        self.assertEqual(settings["number_of_replicas"], 1)
        self.assertEqual(
            settings["plugins.index_state_management.rollover_alias"],
            "cdsem_msr_info",
        )

    def test_build_ism_policy_rolls_over_at_15gb_and_deletes_after_50d(self) -> None:
        body = mgmt.build_ism_policy_body()
        policy = body["policy"]
        hot_state = policy["states"][0]
        delete_state = policy["states"][1]

        self.assertEqual(
            policy["ism_template"],
            [
                {
                    "index_patterns": ["cdsem_msr_info-*", "hvsem_msr_info-*"],
                    "priority": 100,
                }
            ],
        )
        self.assertEqual(
            hot_state["actions"],
            [{"rollover": {"min_size": "15gb"}}],
        )
        self.assertEqual(
            hot_state["transitions"],
            [
                {
                    "state_name": "delete",
                    "conditions": {"min_index_age": "50d"},
                }
            ],
        )
        self.assertEqual(delete_state["actions"], [{"delete": {}}])

    def test_build_index_template_sets_per_alias_rollover_alias(self) -> None:
        body = mgmt.build_index_template_body("hvsem_msr_info")

        self.assertEqual(body["index_patterns"], ["hvsem_msr_info-*"])
        self.assertEqual(
            body["template"]["settings"],
            mgmt.build_index_settings("hvsem_msr_info"),
        )

    def test_create_client_reads_connection_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SKEWNONO_OPENSEARCH_HOST": "cluster.example",
                "SKEWNONO_OPENSEARCH_USER": "sem-user",
                "SKEWNONO_OPENSEARCH_PASSWORD": "secret",
            },
            clear=True,
        ):
            with patch("ops_index_mgmt.hitachi_sem_msr_info.create_client") as factory:
                mgmt.create_skewnono_client()

        factory.assert_called_once_with(
            host="cluster.example",
            user="sem-user",
            password="secret",
        )

    def test_create_client_defaults_host_and_user(self) -> None:
        with patch.dict(
            os.environ,
            {"SKEWNONO_OPENSEARCH_PASSWORD": "secret"},
            clear=True,
        ):
            with patch("ops_index_mgmt.hitachi_sem_msr_info.create_client") as factory:
                mgmt.create_skewnono_client()

        factory.assert_called_once_with(
            host="skewnono-db1-os.osp01.skhynix.com",
            user="skewnono001",
            password="secret",
        )

    def test_create_client_requires_connection_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                mgmt.create_skewnono_client()

    def test_put_management_resources_uses_expected_paths(self) -> None:
        client = Mock()

        mgmt.put_ism_policy(client)
        mgmt.put_index_templates(client)

        client.transport.perform_request.assert_has_calls(
            [
                call(
                    "PUT",
                    "/_plugins/_ism/policies/sem_msr_info_retention_policy",
                    body=mgmt.build_ism_policy_body(),
                ),
                call(
                    "PUT",
                    "/_index_template/cdsem_msr_info_template",
                    body=mgmt.build_index_template_body("cdsem_msr_info"),
                ),
                call(
                    "PUT",
                    "/_index_template/hvsem_msr_info_template",
                    body=mgmt.build_index_template_body("hvsem_msr_info"),
                ),
            ]
        )

    def test_ensure_rollover_index_creates_numbered_backing_index(self) -> None:
        client = Mock()
        client.indices.exists.side_effect = [False, False]
        client.indices.exists_alias.return_value = False
        client.indices.create.return_value = {"acknowledged": True}

        result = mgmt.ensure_rollover_index(client, "cdsem_msr_info")

        self.assertTrue(result["created"])
        self.assertEqual(result["alias"], "cdsem_msr_info")
        self.assertEqual(result["write_index"], "cdsem_msr_info-000001")
        client.indices.create.assert_called_once_with(
            index="cdsem_msr_info-000001",
            body={
                "settings": mgmt.build_index_settings("cdsem_msr_info"),
                "aliases": {
                    "cdsem_msr_info": {
                        "is_write_index": True,
                    }
                },
            },
        )

    def test_ensure_rollover_index_reuses_existing_rollover_alias(self) -> None:
        client = Mock()
        client.indices.exists.side_effect = [False, False]
        client.indices.exists_alias.side_effect = [True, True]
        client.indices.get_alias.side_effect = [
            {
                "hvsem_msr_info-000001": {
                    "aliases": {
                        "hvsem_msr_info": {"is_write_index": True}
                    }
                }
            },
            {
                "hvsem_msr_info-000001": {
                    "aliases": {
                        "hvsem_msr_info": {"is_write_index": True}
                    }
                }
            },
        ]

        result = mgmt.ensure_rollover_index(client, "hvsem_msr_info")

        self.assertFalse(result["created"])
        self.assertEqual(result["write_index"], "hvsem_msr_info-000001")
        client.indices.create.assert_not_called()

    def test_ensure_rollover_index_rejects_non_rollover_existing_index(self) -> None:
        client = Mock()
        client.indices.exists.return_value = True
        client.indices.get.return_value = {
            "cdsem_msr_info": {
                "aliases": {},
            }
        }

        with self.assertRaises(RuntimeError):
            mgmt.ensure_rollover_index(client, "cdsem_msr_info")

    def test_ensure_rollover_indices_creates_both_aliases(self) -> None:
        client = Mock()
        client.indices.exists.side_effect = [False, False, False, False]
        client.indices.exists_alias.return_value = False

        result = mgmt.ensure_rollover_indices(client)

        self.assertEqual(
            sorted(result),
            ["cdsem_msr_info", "hvsem_msr_info"],
        )
        self.assertEqual(client.indices.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
