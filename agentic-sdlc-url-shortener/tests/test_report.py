"""Tests for release evidence and metrics (report.py)."""
import tempfile
import unittest
from pathlib import Path

from sdlc_agent.report import (
    append_event,
    build_summary,
    compute_metrics,
    read_events,
    render_markdown,
    write_summary,
)


class ReportTests(unittest.TestCase):
    def test_metrics_and_events_match_execution(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            events_path = Path(tmp_dir) / "events.jsonl"
            ticks = iter([100.0, 100.0, 105.0, 110.0, 112.0, 120.0, 125.0])
            clock = lambda: next(ticks)

            append_event(events_path, "stage_started", {"stage": "analysis"}, clock=clock)
            append_event(events_path, "stage_completed", {"stage": "analysis"}, clock=clock)
            append_event(events_path, "stage_failed", {"stage": "validation"}, clock=clock)
            append_event(events_path, "retry", {"stage": "validation"}, clock=clock)
            append_event(events_path, "recovery", {"stage": "validation"}, clock=clock)
            append_event(events_path, "stage_completed", {"stage": "validation"}, clock=clock)
            append_event(events_path, "rollback", {"stage": "candidate"}, clock=clock)

            events = read_events(events_path)
            self.assertEqual(len(events), 7)
            self.assertEqual(len({event["event_id"] for event in events}), 7)  # unique event ids
            self.assertEqual([event["timestamp"] for event in events], sorted(event["timestamp"] for event in events))

            metrics = compute_metrics(events)
            self.assertEqual(metrics.retries, 1)
            self.assertEqual(metrics.rollbacks, 1)
            self.assertAlmostEqual(metrics.task_success_rate, 2 / 3)
            self.assertTrue(metrics.mttr_available)
            self.assertAlmostEqual(metrics.mean_time_to_recovery_seconds, 7.0)

            summary = build_summary(run_id="run-1", status="completed", metrics=metrics,
                                     artifact_hashes={"app.py": "abc123"})
            write_summary(Path(tmp_dir), summary)

            summary_json = (Path(tmp_dir) / "summary.json").read_text(encoding="utf-8")
            self.assertIn("abc123", summary_json)
            summary_markdown = (Path(tmp_dir) / "summary.md").read_text(encoding="utf-8")
            self.assertIn("app.py", summary_markdown)
            self.assertIn("7.00s", summary_markdown)

    def test_mttr_reported_unavailable_without_recovery(self):
        metrics = compute_metrics([])
        self.assertFalse(metrics.mttr_available)
        self.assertIsNone(metrics.mean_time_to_recovery_seconds)
        self.assertIsNone(metrics.task_success_rate)

        summary = build_summary("run-2", "failed", metrics, {})
        markdown = render_markdown(summary)
        self.assertIn("unavailable (no failure recovered)", markdown)

    def test_read_events_returns_empty_list_for_missing_log(self):
        self.assertEqual(read_events(Path("does-not-exist.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
