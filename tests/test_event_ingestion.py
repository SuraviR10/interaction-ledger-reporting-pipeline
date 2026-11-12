"""
Unit tests for Checkpoint 4A: JSONL Event Reading, Parsing, and Validation.
"""

from pathlib import Path
import tempfile
import unittest

from solution.event_ingestion import (
    EventIngestionStats,
    ParsedEvent,
    parse_event_line,
    read_event_jsonl,
)


class EventIngestionCheckpoint4ATests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_jsonl(self, filename: str, lines: list[str]) -> Path:
        file_path = self.temp_path / filename
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return file_path

    def test_parse_valid_event_with_phone(self):
        line = (
            '{"event_id": "evt_001", "event_type": "call.initiated", '
            '"workflow_id": "motor_renewal", "call_id": "call_100", '
            '"occurred_at": "2026-10-01T09:00:00Z", "phone": "+91 98765 43210", '
            '"payload": {"provider": "mock"}}'
        )
        status, event = parse_event_line(line)
        self.assertEqual(status, "valid")
        self.assertIsInstance(event, ParsedEvent)
        self.assertEqual(event.event_id, "evt_001")
        self.assertEqual(event.event_type, "call.initiated")
        self.assertEqual(event.workflow_id, "motor_renewal")
        self.assertEqual(event.call_id, "call_100")
        self.assertEqual(event.occurred_at_raw, "2026-10-01T09:00:00Z")
        self.assertEqual(event.phone_raw, "+91 98765 43210")
        self.assertEqual(event.payload, {"provider": "mock"})

    def test_parse_disposition_override_without_phone(self):
        # Override events intentionally lack a phone field
        line = (
            '{"event_id": "evt_002", "event_type": "disposition.override", '
            '"workflow_id": "motor_renewal", "call_id": "call_100", '
            '"occurred_at": "2026-10-02T10:00:00+05:30", '
            '"payload": {"disposition_code": "renewed", "overridden_by": "sup_1"}}'
        )
        status, event = parse_event_line(line)
        self.assertEqual(status, "valid")
        self.assertIsNone(event.phone_raw)
        self.assertEqual(event.event_type, "disposition.override")
        self.assertEqual(event.payload.get("overridden_by"), "sup_1")

    def test_blank_lines_are_skipped(self):
        status, event = parse_event_line("   \n")
        self.assertEqual(status, "empty")
        self.assertIsNone(event)

    def test_truncated_json_detected_as_malformed(self):
        # Truncated syntax injected by synthetic generator
        truncated = '{"event_id": "evt_003", "event_type": "call.'
        status, event = parse_event_line(truncated)
        self.assertEqual(status, "malformed")
        self.assertIsNone(event)

    def test_non_dictionary_json_detected_as_malformed(self):
        # JSON arrays, numbers, booleans, and null are not valid event records
        for non_dict in ["[1, 2, 3]", "12345", "true", "null", '"just a string"']:
            status, event = parse_event_line(non_dict)
            self.assertEqual(status, "malformed", f"Failed to mark non-dict as malformed: {non_dict}")
            self.assertIsNone(event)

    def test_missing_required_core_fields(self):
        # Each of these core fields must be present and non-empty
        core_fields = ["event_id", "event_type", "workflow_id", "call_id", "occurred_at"]
        base = {
            "event_id": "evt_004",
            "event_type": "call.connected",
            "workflow_id": "motor_renewal",
            "call_id": "call_101",
            "occurred_at": "2026-10-01 09:05:00",
        }

        import json
        for missing in core_fields:
            data = dict(base)
            del data[missing]
            status, event = parse_event_line(json.dumps(data))
            self.assertEqual(status, "malformed", f"Failed when missing: {missing}")

            # Also test when field is empty string
            data[missing] = "   "
            status, event = parse_event_line(json.dumps(data))
            self.assertEqual(status, "malformed", f"Failed when empty: {missing}")

    def test_read_event_jsonl_stream_and_stats(self):
        lines = [
            '{"event_id": "e1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z", "phone": "9876543210"}',
            "",  # empty line (not counted)
            '   ',  # whitespace line (not counted)
            '{"event_id": "e2", "event_type": "call.',  # malformed JSON (counted in events_total and malformed_events)
            '{"event_type": "call.connected", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:01:00Z"}',  # missing event_id (malformed)
            '{"event_id": "e3", "event_type": "disposition.override", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z"}',
        ]
        file_path = self._write_jsonl("events_test.jsonl", lines)

        stats = EventIngestionStats()
        events = list(read_event_jsonl(file_path, stats=stats))

        # 4 non-empty lines total (e1, malformed JSON, missing event_id, e3)
        self.assertEqual(stats.events_total, 4)
        # 2 malformed lines
        self.assertEqual(stats.malformed_events, 2)
        # 2 valid events parsed (e1 and e3)
        self.assertEqual(len(events), 2)
        self.assertEqual([e.event_id for e in events], ["e1", "e3"])

    def test_stats_merge(self):
        s1 = EventIngestionStats(events_total=10, malformed_events=2)
        s2 = EventIngestionStats(events_total=5, malformed_events=1)
        s1.merge(s2)
        self.assertEqual(s1.events_total, 15)
        self.assertEqual(s1.malformed_events, 3)


if __name__ == "__main__":
    unittest.main()
