"""Explicit planner effort is source-bound while historical plans retain max."""
import unittest

from native_models import attempts_from_report, planner_profile, planner_profile_from_payload


class PlannerEffortTests(unittest.TestCase):
    def test_new_plans_bind_selected_effort_and_legacy_plans_keep_max(self):
        for effort in ("high", "xhigh", "max"):
            with self.subTest(effort=effort):
                payload = {"experimental_query_plan": True, "planner_model": "gpt-6-luna",
                           "planner_reasoning_effort": effort}
                self.assertEqual(planner_profile_from_payload(payload),
                                 {"model": "gpt-6-luna", "reasoning_effort": effort, "service_tier": "fast"})
        self.assertEqual(planner_profile_from_payload({"experimental_query_plan": True,
                                                       "planner_model": "gpt-6-luna"})["reasoning_effort"], "max")
        self.assertIsNone(planner_profile_from_payload({"experimental_query_plan": False}))
        with self.assertRaises(ValueError):
            planner_profile_from_payload({"experimental_query_plan": False,
                                          "planner_reasoning_effort": "high"})

    def test_accounting_accepts_only_matching_declared_planner_effort(self):
        record = {"attempt_id": "synthetic-1", "role": "query_planner", "status": "failed",
                  "requested_model": "gpt-6-luna", "requested_reasoning_effort": "high",
                  "requested_service_tier": "fast", "elapsed_ms": 1,
                  "server_retry_notifications": 0, "accounting_complete": False,
                  "usage": None}
        report = {"status": "failed", "model_attempts": [record]}
        reader = {"model": "gpt-6-luna", "reasoning_effort": "high", "service_tier": "fast"}
        self.assertEqual(len(attempts_from_report(report, reader, required=True,
                                                  planner_profile=planner_profile("gpt-6-luna", "high"))), 1)
        with self.assertRaisesRegex(ValueError, "requested profile differs"):
            attempts_from_report(report, reader, required=True,
                                 planner_profile=planner_profile("gpt-6-luna", "max"))
        for invalid in ("low", "medium", "MAX", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                planner_profile("gpt-6-luna", invalid)


if __name__ == "__main__":
    unittest.main()
