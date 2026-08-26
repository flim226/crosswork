#!/usr/bin/env python3
"""Unit tests for SR-policy retrieval."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

module_path = Path(__file__).with_name("get_sr_policies.py")
spec = importlib.util.spec_from_file_location("get_sr_policies_script", module_path)
get_sr_policies_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(get_sr_policies_script)


class GetSrPoliciesTests(unittest.TestCase):
    def test_uses_expected_endpoint_and_bearer_token(self):
        session = Mock()
        response = Mock(ok=True)
        response.json.return_value = {"cisco-crosswork-segment-routing-policy:sr-policies": {"policy": []}}
        session.get.return_value = response

        result = get_sr_policies_script.get_sr_policies(
            session, "https://cnc.example:30603", "example-token"
        )

        self.assertEqual(result["cisco-crosswork-segment-routing-policy:sr-policies"]["policy"], [])
        session.get.assert_called_once_with(
            "https://cnc.example:30603"
            "/crosswork/nbi/optima/v2/restconf/data/"
            "cisco-crosswork-segment-routing-policy:sr-policies",
            headers={
                "Authorization": "Bearer example-token",
                "Accept": "application/yang-data+json",
            },
        )


if __name__ == "__main__":
    unittest.main()
