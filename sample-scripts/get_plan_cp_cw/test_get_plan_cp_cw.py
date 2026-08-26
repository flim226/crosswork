#!/usr/bin/env python3
"""Unit tests for Control Plane startup-plan geolocation enrichment."""

import importlib.util
from pathlib import Path
import unittest

module_path = Path(__file__).with_name("get_plan_cp_cw.py")
spec = importlib.util.spec_from_file_location("get_plan_cp_cw_script", module_path)
get_plan_cp_cw_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(get_plan_cp_cw_script)
update_plan_node_coordinates = get_plan_cp_cw_script.update_plan_node_coordinates


class UpdatePlanNodeCoordinatesTests(unittest.TestCase):
    def test_updates_matching_nodes_and_preserves_unmatched_nodes(self):
        plan = (
            b"<Nodes>\n"
            b"Name\tSite\tFunction\tProtected\tLongitude\tLatitude\n"
            b"node-1\tsite\tcore\tF\t\t\n"
            b"node-2\tsite\tcore\tF\t\t\n"
            b"<Circuits>\n"
        )
        result, updated, total = update_plan_node_coordinates(plan, {"node-1": (-87.6, 41.8)})

        self.assertEqual((updated, total), (1, 2))
        self.assertIn(b"node-1\tsite\tcore\tF\t-87.6\t41.8\n", result)
        self.assertIn(b"node-2\tsite\tcore\tF\t\t\n", result)

    def test_requires_nodes_coordinate_columns(self):
        with self.assertRaises(ValueError):
            update_plan_node_coordinates(b"<Nodes>\nName\tSite\n", {})


if __name__ == "__main__":
    unittest.main()
