import unittest
from unittest.mock import Mock, call, patch

from ops_index_mgmt import beam_reso_cdsem as beam_reso
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


class BeamResoCdsemTests(unittest.TestCase):
    def test_make_doc_id_joins_the_three_id_fields_in_order(self) -> None:
        doc = {
            "eqp_ip": "10.1.2.3",
            "timestamp": "2026-06-09T10:00:00+09:00",
            "beam_condition": "lowkv",
            "other": "ignored",
        }

        self.assertEqual(
            beam_reso.make_doc_id(doc),
            "10.1.2.3_2026-06-09T10:00:00+09:00_lowkv",
        )

    def test_make_doc_id_coerces_non_string_values(self) -> None:
        doc = {"eqp_ip": "10.1.2.3", "timestamp": 1700000000, "beam_condition": 5}

        self.assertEqual(beam_reso.make_doc_id(doc), "10.1.2.3_1700000000_5")

    def test_make_doc_id_raises_when_an_id_field_is_missing(self) -> None:
        with self.assertRaises(KeyError):
            beam_reso.make_doc_id({"eqp_ip": "10.1.2.3", "timestamp": "t"})

    def test_has_id_fields_accepts_complete_doc_including_zero_values(self) -> None:
        self.assertTrue(
            beam_reso.has_id_fields(
                {"eqp_ip": "10.1.2.3", "timestamp": 0, "beam_condition": 0}
            )
        )

    def test_has_id_fields_rejects_missing_none_and_blank(self) -> None:
        base = {"eqp_ip": "ip", "timestamp": "t", "beam_condition": "b"}

        self.assertFalse(beam_reso.has_id_fields({k: v for k, v in base.items() if k != "timestamp"}))
        self.assertFalse(beam_reso.has_id_fields({**base, "eqp_ip": None}))
        self.assertFalse(beam_reso.has_id_fields({**base, "beam_condition": "   "}))

    def test_iter_bulk_actions_skips_docs_missing_an_id_field(self) -> None:
        docs = [
            {"eqp_ip": "ip1", "timestamp": "t1", "beam_condition": "b1", "v": 1},
            {"eqp_ip": "ip2", "timestamp": "t2"},  # missing beam_condition -> skip
            {"eqp_ip": "ip3", "timestamp": "t3", "beam_condition": "b3", "v": 3},
        ]

        actions = list(
            beam_reso.iter_bulk_actions(
                docs,
                index="beam_shape_cdsem",
                os_inserted="2026-06-09T10:00:00+09:00",
            )
        )

        self.assertEqual([a["_id"] for a in actions], ["ip1_t1_b1", "ip3_t3_b3"])
        first = actions[0]
        self.assertEqual(first["_op_type"], "create")
        self.assertEqual(first["_index"], "beam_shape_cdsem")
        self.assertEqual(first["_source"]["v"], 1)
        self.assertEqual(
            first["_source"]["os_inserted"], "2026-06-09T10:00:00+09:00"
        )

    def test_iter_bulk_actions_honors_op_type_override(self) -> None:
        docs = [{"eqp_ip": "ip", "timestamp": "t", "beam_condition": "b"}]

        actions = list(
            beam_reso.iter_bulk_actions(
                docs,
                index="reso_center_cdsem",
                os_inserted="2026-06-09T10:00:00+09:00",
                op_type="index",
            )
        )

        self.assertEqual(actions[0]["_op_type"], "index")

    def test_reso_center_mapping_disables_the_three_resolution_objects(self) -> None:
        mappings = beam_reso.build_mappings("reso_center_cdsem")
        props = mappings["properties"]

        for field in (
            "Resolution_Range",
            "Resolution_Range_Raw",
            "Resolution_Range_Smooth",
        ):
            self.assertEqual(props[field], {"type": "object", "enabled": False})
        self.assertEqual(props["os_inserted"], {"type": "date"})

    def test_beam_shape_mapping_has_no_disabled_objects(self) -> None:
        props = beam_reso.build_mappings("beam_shape_cdsem")["properties"]

        self.assertEqual(list(props), ["os_inserted"])

    def test_shared_policy_covers_both_index_patterns(self) -> None:
        policy = beam_reso.build_ism_policy_body()["policy"]

        self.assertEqual(
            policy["ism_template"][0]["index_patterns"],
            ["beam_shape_cdsem-*", "reso_center_cdsem-*"],
        )
        self.assertEqual(
            policy["states"][0]["actions"],
            [{"rollover": {"min_doc_count": 500000}}],
        )
        self.assertEqual(
            policy["states"][0]["transitions"][0]["conditions"],
            {"min_index_age": "1095d"},
        )


if __name__ == "__main__":
    unittest.main()
