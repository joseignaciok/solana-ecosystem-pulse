import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pulse_report


class PulseReportTests(unittest.TestCase):
    def test_first_run_has_no_false_anomalies(self):
        current = {"tps": 100, "slotTimeMs": 400, "solPriceUsd": 150, "solanaTvlUsd": 1_000_000, "delinquentValidators": 2}
        self.assertEqual(pulse_report.compare(None, current), [])

    def test_material_changes_are_flagged(self):
        before = {"metrics": {"tps": 100, "slotTimeMs": 400, "solPriceUsd": 100, "solanaTvlUsd": 1_000_000, "delinquentValidators": 1}}
        current = {"tps": 140, "slotTimeMs": 520, "solPriceUsd": 110, "solanaTvlUsd": 1_150_000, "delinquentValidators": 12}
        alerts = pulse_report.compare(before, current)
        self.assertEqual({a["metric"] for a in alerts}, {"tps", "slotTimeMs", "solPriceUsd", "solanaTvlUsd", "delinquentValidators"})

    def test_read_only_collector_and_outputs(self):
        def fake_rpc(_url, method, _params=None):
            values = {
                "getHealth": "ok",
                "getEpochInfo": {"epoch": 20, "slotIndex": 50, "slotsInEpoch": 100},
                "getSlot": 123,
                "getBlockTime": 1700000000,
                "getRecentPerformanceSamples": [{"samplePeriodSecs": 60, "numTransactions": 6000, "numSlots": 120}],
                "getVoteAccounts": {"current": [{"activatedStake": 500}], "delinquent": []},
                "getSupply": {"value": {"total": 2_000_000_000, "circulating": 1_500_000_000, "nonCirculating": 500_000_000}},
            }
            return values[method]

        with patch.object(pulse_report, "rpc_call", fake_rpc), patch.object(
            pulse_report, "collect_market", return_value={"solPriceUsd": 150, "solanaTvlUsd": 2_000_000}
        ):
            report = pulse_report.make_report("https://rpc.example", None)
        self.assertTrue(report["readOnly"])
        self.assertEqual(report["metrics"]["tps"], 100)
        self.assertEqual(report["metrics"]["epochProgressPct"], 50.0)
        self.assertEqual(report["metrics"]["totalSupplySol"], 2.0)
        with tempfile.TemporaryDirectory() as tmp:
            pulse_report.write_outputs(report, Path(tmp))
            self.assertTrue((Path(tmp) / "index.html").exists())
            self.assertEqual(json.loads((Path(tmp) / "data" / "report.json").read_text())["schema"], "solana-ecosystem-pulse/v1")
            self.assertIn("read-only", (Path(tmp) / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
