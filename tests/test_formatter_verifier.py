from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from kienzledoku_ocr_backfill.errors import VerificationError
from kienzledoku_ocr_backfill.formatter import (
    BLOCK_BEGIN,
    BLOCK_END,
    compose_text,
    contains_ocr_marker,
    first_text_lines,
    latest_ocr_marker,
    make_footer,
    remove_managed_ocr_block,
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
            "Alter Titel\n\n\n"
            + BLOCK_BEGIN
            + "\nOCR-Text\n\n"
            + footer
            + "\n"
            + BLOCK_END,
        )

    def test_marker_is_idempotent_and_line_anchored(self):
        self.assertTrue(contains_ocr_marker("x\nkienzledoku OCR v1.00, 30.08.2026"))
        self.assertTrue(contains_ocr_marker("x\n" + BLOCK_BEGIN + "\nunvollständig"))
        self.assertFalse(contains_ocr_marker("x kienzledoku OCR v1.00, 30.08.2026"))

    def test_complete_managed_block_can_be_removed_exactly(self):
        footer = "kienzledoku OCR v1.1, 31.08.2026 12:00"
        value = compose_text("Titel", "Neuer OCR-Text", footer)
        self.assertEqual(remove_managed_ocr_block(value), "Titel")
        self.assertIsNone(remove_managed_ocr_block(value.removesuffix(BLOCK_END)))

    def test_marker_details_and_two_line_preview(self):
        value = (
            "Titel\n\n"
            "kienzledoku OCR v1.1, 31.08.2026 12:00\n"
            "kienzledoku OCR v1.3, 31.08.2026 15:30"
        )
        self.assertEqual(latest_ocr_marker(value), ("1.3", "31.08.2026 15:30"))
        self.assertEqual(first_text_lines(" Eins \n\nZwei\nDrei"), ["Eins", "Zwei"])
        self.assertEqual(first_text_lines(None), ["(leer)"])


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
