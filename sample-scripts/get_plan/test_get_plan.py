#!/usr/bin/env python3
"""Unit tests for plan geolocation enrichment."""

import importlib.util
from pathlib import Path
import unittest

module_path = Path(__file__).with_name("get_plan.py")
spec = importlib.util.spec_from_file_location("get_plan_script", module_path)
get_plan_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(get_plan_script)
update_plan_node_coordinates = get_plan_script.update_plan_node_coordinates


class UpdatePlanNodeCoordinatesTests(unittest.TestCase):
    def test_updates_matching_nodes_and_preserves_unmatched_nodes(self):
        plan = (
            b"<Network>\n"
            b"<Nodes>\n"
            b"Name\tSite\tFunction\tProtected\tLongitude\tLatitude\n"
            b"cr1.atl\tatl\tcore\tF\t\t\n"
            b"cr1.bos\tbos\tcore\tF\t\t\n"
            b"\n<Circuits>\n"
        )
        result, updated, total = update_plan_node_coordinates(
            plan, {"cr1.atl": (-84.43, 33.65)}
        )

        self.assertEqual(updated, 1)
        self.assertEqual(total, 2)
        self.assertIn(b"cr1.atl\tatl\tcore\tF\t-84.43\t33.65\n", result)
        self.assertIn(b"cr1.bos\tbos\tcore\tF\t\t\n", result)

    def test_requires_nodes_table_coordinate_columns(self):
        with self.assertRaises(ValueError):
            update_plan_node_coordinates(b"<Network>\n", {})


if __name__ == "__main__":
    unittest.main()
