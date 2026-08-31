import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-ocr-dependencies.sh"


class DependencyInstallerTests(unittest.TestCase):
    def test_bash_syntax_and_help(self):
        syntax = subprocess.run(
            ["bash", "-n", str(INSTALLER)], capture_output=True, text=True
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        help_result = subprocess.run(
            ["bash", str(INSTALLER), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--check", help_result.stdout)

    def test_read_only_check_accepts_confirmed_toolchain(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            ocrmypdf = bin_dir / "ocrmypdf"
            ocrmypdf.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ ${1:-} == --help ]]; then\n"
                "  echo '--mode --rotate-pages --rotate-pages-threshold --deskew --clean --oversample --output-type --optimize --tesseract-timeout --jobs'\n"
                "else\n"
                "  echo 'ocrmypdf test'\n"
                "fi\n",
                encoding="utf-8",
            )
            ocrmypdf.chmod(0o755)

            tesseract = bin_dir / "tesseract"
            tesseract.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ ${1:-} == --list-langs ]]; then\n"
                "  printf 'deu\\neng\\nosd\\n'\n"
                "else\n"
                "  echo 'tesseract test'\n"
                "fi\n",
                encoding="utf-8",
            )
            tesseract.chmod(0o755)

            for name in ("pdftotext", "gs", "qpdf", "unpaper"):
                stub = bin_dir / name
                stub.write_text(
                    "#!/usr/bin/env bash\necho 'tool test'\n",
                    encoding="utf-8",
                )
                stub.chmod(0o755)

            qpdf = bin_dir / "qpdf"
            qpdf.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ ${1:-} == --help=all ]]; then\n"
                "  echo '--rotate --flatten-rotation'\n"
                "else\n"
                "  echo 'qpdf test'\n"
                "fi\n",
                encoding="utf-8",
            )
            qpdf.chmod(0o755)

            environment = dict(os.environ)
            environment["PATH"] = f"{bin_dir}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", str(INSTALLER), "--check"],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OCR-Abhängigkeiten sind vollständig", result.stdout)

    def test_required_packages_are_explicit(self):
        text = INSTALLER.read_text(encoding="utf-8")
        for package in (
            "ocrmypdf",
            "tesseract-ocr",
            "tesseract-ocr-deu",
            "tesseract-ocr-eng",
            "tesseract-ocr-osd",
            "poppler-utils",
            "ghostscript",
            "qpdf",
            "unpaper",
        ):
            self.assertIn(package, text)


if __name__ == "__main__":
    unittest.main()
