"""Deterministic outcome accounting, without servers or timed workloads."""
import unittest
from scripts import benchmark

helpers = vars(benchmark)


class BenchmarkContractTests(unittest.TestCase):
    def test_profile_and_stability_artifacts_cover_frozen_sizes(self):
        sizes = [4096, 131072, 1048576]
        artifacts = helpers["diagnostic_artifacts"](sizes)
        self.assertEqual([item["size"] for item in artifacts], sizes)
        self.assertEqual([item["profile_kind"] for item in artifacts], ["exact-read"] * 3)
        self.assertEqual([item["profile"] for item in artifacts], [
            "client-read.prof", "client-read-131072.prof", "client-read-1048576.prof"])
        self.assertEqual([item["profile_receipt"] for item in artifacts], [
            "profile.usage", "profile-131072.usage", "profile-1048576.usage"])
        self.assertEqual([item["stability_receipt"] for item in artifacts], [
            "stability.usage", "stability-131072.usage", "stability-1048576.usage"])
        self.assertEqual(len({item[key] for item in artifacts for key in ("profile_receipt", "stability_receipt")}), 6)
        smoke = helpers["diagnostic_artifacts"]([4096])
        self.assertEqual(smoke[0]["profile"], "client-read.prof")
        self.assertEqual(smoke[0]["stability_receipt"], "stability.usage")

    def test_only_the_observed_missing_metadata_quorum_is_expected(self):
        sample = {"returncode": 2, "timed_out": False, "stderr": '{"error":"metadata needs 3 server responses; received 2"}'}
        self.assertTrue(helpers["expected_quorum_rejection"](sample))
        for failure in ["bad owner", "permission denied", "metadata needs 3 server responses; received 1"]:
            self.assertFalse(helpers["expected_quorum_rejection"]({**sample, "stderr": '{"error":"' + failure + '"}'}))
        self.assertFalse(helpers["expected_quorum_rejection"]({**sample, "timed_out": True}))
        self.assertFalse(helpers["expected_quorum_rejection"]({**sample, "stderr": "not JSON"}))

    def test_load_counts_and_tails_do_not_mix_quorum_rejections_with_reads(self):
        rows = [{"correct": True, "expected_rejection": False, "timed_out": False, "scheduled_latency_seconds": 9},
                {"correct": False, "expected_rejection": True, "timed_out": False, "scheduled_latency_seconds": .1},
                {"correct": False, "expected_rejection": False, "timed_out": True, "scheduled_latency_seconds": 60}]
        result = helpers["load_outcomes"](rows)
        self.assertEqual({k: result[k] for k in ("offered", "completed", "rejected", "errors", "timeouts")},
                         {"offered": 3, "completed": 1, "rejected": 1, "errors": 1, "timeouts": 1})
        self.assertEqual(result["latency_by_outcome"]["completed"]["p50_seconds"], 9)
        self.assertEqual(result["latency_by_outcome"]["rejected"]["p50_seconds"], .1)
        self.assertEqual(result["latency_by_outcome"]["errors"]["p50_seconds"], 60)

    def test_empty_and_small_samples_do_not_invent_p99(self):
        self.assertIsNone(helpers["latency_summary"]([])["p50_seconds"])
        self.assertIsNone(helpers["latency_summary"](list(range(99)))["p99_seconds"])
        self.assertEqual(helpers["latency_summary"](list(range(100)))["p99_seconds"], 98)
        empty = helpers["load_outcomes"]([])
        self.assertEqual(empty["offered"], 0)
        self.assertEqual(empty["latency_by_outcome"]["rejected"]["samples"], 0)
        with self.assertRaises(AssertionError):
            helpers["load_outcomes"]([{"correct": True, "expected_rejection": True, "timed_out": False, "scheduled_latency_seconds": 1}])
