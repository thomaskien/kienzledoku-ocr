from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from kienzledoku_ocr_backfill.errors import VerificationError
from kienzledoku_ocr_backfill.formatter import (
    compose_text,
    contains_ocr_marker,
    make_footer,
)
from kienzledoku_ocr_backfill.models import DocumentSnapshot
from kienzledoku_ocr_backfill.verifier import verify_update


class FormatterTests(unittest.TestCase):
    def test_exact_format_and_berlin_footer(self):
        footer = make_footer(
            "1.00",
            datetime(2026, 8, 30, 14, 55, tzinfo=ZoneInfo("Europe/Berlin")),
        )
        self.assertEqual(footer, "kienzledoku OCR v1.00, 30.08.2026 14:55")
        self.assertEqual(
            compose_text("Alter Titel  \n", "  OCR-Text\n", footer),
            "Alter Titel\n\n\nOCR-Text\n\n" + footer,
        )

    def test_marker_is_idempotent_and_line_anchored(self):
        self.assertTrue(contains_ocr_marker("x\nkienzledoku OCR v1.00, 30.08.2026"))
        self.assertFalse(contains_ocr_marker("x kienzledoku OCR v1.00, 30.08.2026"))


class VerifierTests(unittest.TestCase):
    def _snapshot(self, text, *, reference="cdn://same", revision=0):
        return DocumentSnapshot(
            dto={
                "ref": {"objectId": {"id": "doc-1"}, "revision": revision},
                "text": text,
                "verweis": reference,
                "gueltigkeitszeitpunkt": 123,
                "fachinformationstyp": 75,
            },
            object_id="doc-1",
            revision=revision,
            text=text,
        )

    def test_exact_update_passes(self):
        before = self._snapshot("Alt", revision=3)
        footer = "kienzledoku OCR v1.00, 30.08.2026 14:55"
        expected = compose_text(before.text, "OCR", footer)
        after = self._snapshot(expected, revision=4)
        verify_update(before, after, expected, footer)

    def test_changed_reference_fails(self):
        before = self._snapshot("Alt", revision=3)
        footer = "kienzledoku OCR v1.00, 30.08.2026 14:55"
        expected = compose_text(before.text, "OCR", footer)
        after = self._snapshot(expected, reference="cdn://changed", revision=4)
        with self.assertRaises(VerificationError):
            verify_update(before, after, expected, footer)

    def test_revision_must_increase(self):
        before = self._snapshot("Alt", revision=3)
        footer = "kienzledoku OCR v1.00, 30.08.2026 14:55"
        expected = compose_text(before.text, "OCR", footer)
        after = self._snapshot(expected, revision=3)
        with self.assertRaises(VerificationError):
            verify_update(before, after, expected, footer)


if __name__ == "__main__":
    unittest.main()
