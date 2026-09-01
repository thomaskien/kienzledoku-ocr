from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from kienzledoku_ocr_backfill.progress import (
    TimestampedReporter,
    format_duration,
    timed_step,
)


class ProgressTests(unittest.TestCase):
    def test_timestamped_reporter_uses_berlin_time_and_keeps_blank_lines(self):
        output = []
        reporter = TimestampedReporter(
            output.append,
            now=lambda: datetime(
                2026, 9, 1, 10, 11, 12, tzinfo=ZoneInfo("Europe/Berlin")
            ),
        )

        reporter("OCR läuft")
        reporter("")

        self.assertEqual(
            output,
            ["[01.09.2026 10:11:12] OCR läuft", ""],
        )

    def test_timed_step_reports_and_records_duration_even_on_error(self):
        values = iter((10.0, 12.3456))
        output = []
        timings = {}

        with self.assertRaisesRegex(RuntimeError, "Testfehler"):
            with timed_step(
                "Testschritt",
                output.append,
                timings=timings,
                key="testStep",
                clock=lambda: next(values),
            ):
                raise RuntimeError("Testfehler")

        self.assertEqual(output, ["Dauer Testschritt: 2.346 s"])
        self.assertEqual(timings, {"testStep": 2.346})
        self.assertEqual(format_duration(-1), "0.000 s")


if __name__ == "__main__":
    unittest.main()
