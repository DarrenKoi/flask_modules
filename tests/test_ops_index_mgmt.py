import unittest
from unittest.mock import Mock, call, patch

from ops_index_mgmt import hitachi_sem_msr_info as mgmt


class SemMsrInfoIndexMgmtTests(unittest.TestCase):
    def test_build_index_settings_uses_requested_cluster_shape(self) -> None:
        settings = mgmt.build_index_settings("meas_hist_cdsem")

        self.assertEqual(settings["number_of_shards"], 3)
        self.assertEqual(settings["number_of_replicas"], 1)
        self.assertEqual(
            settings["plugins.index_state_management.rollover_alias"],
            "meas_hist_cdsem",
        )

    def test_build_ism_policy_rolls_over_after_60d_and_deletes_after_365d(
        self,
    ) -> None:
        body = mgmt.build_ism_policy_body()
        policy = body["policy"]
        hot_state = policy["states"][0]
        delete_state = policy["states"][1]

        self.assertEqual(
            policy["ism_template"],
            [
                {
                    "index_patterns": ["meas_hist_cdsem-*", "meas_hist_hvsem-*"],
                    "priority": 100,
                }
            ],
        )
        self.assertEqual(
            hot_state["actions"],
            [{"rollover": {"min_index_age": "60d"}}],
        )
        self.assertEqual(
            hot_state["transitions"],
            [
                {
                    "state_name": "delete",
                    "conditions": {"min_index_age": "365d"},
                }
            ],
        )
        self.assertEqual(delete_state["actions"], [{"delete": {}}])

    def test_build_index_template_sets_per_alias_rollover_alias(self) -> None:
        body = mgmt.build_index_template_body("meas_hist_hvsem")

        self.assertEqual(body["index_patterns"], ["meas_hist_hvsem-*"])
        self.assertEqual(
            body["template"]["settings"],
            mgmt.build_index_settings("meas_hist_hvsem"),
        )

    def test_create_client_reads_connection_from_module_variables(self) -> None:
        with patch.object(mgmt, "OPENSEARCH_HOST", "cluster.example"):
            with patch.object(mgmt, "OPENSEARCH_USER", "sem-user"):
                with patch.object(mgmt, "OPENSEARCH_PASSWORD", "secret"):
                    with patch(
                        "ops_index_mgmt.hitachi_sem_msr_info.create_client"
                    ) as factory:
                        mgmt.create_skewnono_client()

        factory.assert_called_once_with(
            host="cluster.example",
            user="sem-user",
            password="secret",
        )

    def test_create_client_defaults_host_and_user(self) -> None:
        with patch.object(mgmt, "OPENSEARCH_PASSWORD", "secret"):
            with patch("ops_index_mgmt.hitachi_sem_msr_info.create_client") as factory:
                mgmt.create_skewnono_client()

        factory.assert_called_once_with(
            host="skewnono-db1-os.osp01.skhynix.com",
            user="skewnono001",
            password="secret",
        )

    def test_create_client_requires_password_variable(self) -> None:
        with patch.object(mgmt, "OPENSEARCH_PASSWORD", ""):
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
                    "/_plugins/_ism/policies/sem_meas_hist_retention_policy",
                    body=mgmt.build_ism_policy_body(),
                ),
                call(
                    "PUT",
                    "/_index_template/meas_hist_cdsem_template",
                    body=mgmt.build_index_template_body("meas_hist_cdsem"),
                ),
                call(
                    "PUT",
                    "/_index_template/meas_hist_hvsem_template",
                    body=mgmt.build_index_template_body("meas_hist_hvsem"),
                ),
            ]
        )

    def test_ensure_rollover_index_creates_numbered_backing_index(self) -> None:
        client = Mock()
        client.indices.exists.side_effect = [False, False]
        client.indices.exists_alias.return_value = False
        client.indices.create.return_value = {"acknowledged": True}

        result = mgmt.ensure_rollover_index(client, "meas_hist_cdsem")

        self.assertTrue(result["created"])
        self.assertEqual(result["alias"], "meas_hist_cdsem")
        self.assertEqual(result["write_index"], "meas_hist_cdsem-000001")
        client.indices.create.assert_called_once_with(
            index="meas_hist_cdsem-000001",
            body={
                "settings": mgmt.build_index_settings("meas_hist_cdsem"),
                "aliases": {
                    "meas_hist_cdsem": {
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
                "meas_hist_hvsem-000001": {
                    "aliases": {
                        "meas_hist_hvsem": {"is_write_index": True}
                    }
                }
            },
            {
                "meas_hist_hvsem-000001": {
                    "aliases": {
                        "meas_hist_hvsem": {"is_write_index": True}
                    }
                }
            },
        ]

        result = mgmt.ensure_rollover_index(client, "meas_hist_hvsem")

        self.assertFalse(result["created"])
        self.assertEqual(result["write_index"], "meas_hist_hvsem-000001")
        client.indices.create.assert_not_called()

    def test_ensure_rollover_index_rejects_non_rollover_existing_index(self) -> None:
        client = Mock()
        client.indices.exists.return_value = True
        client.indices.get.return_value = {
            "meas_hist_cdsem": {
                "aliases": {},
            }
        }

        with self.assertRaises(RuntimeError):
            mgmt.ensure_rollover_index(client, "meas_hist_cdsem")

    def test_ensure_rollover_indices_creates_both_aliases(self) -> None:
        client = Mock()
        client.indices.exists.side_effect = [False, False, False, False]
        client.indices.exists_alias.return_value = False

        result = mgmt.ensure_rollover_indices(client)

        self.assertEqual(
            sorted(result),
            ["meas_hist_cdsem", "meas_hist_hvsem"],
        )
        self.assertEqual(client.indices.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
