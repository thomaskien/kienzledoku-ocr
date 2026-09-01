#!/usr/bin/env bash
set -euo pipefail

VERSION="1.5.4"

readonly PACKAGES=(
  python3
  ocrmypdf
  tesseract-ocr
  tesseract-ocr-deu
  tesseract-ocr-eng
  tesseract-ocr-osd
  poppler-utils
  python3-pil
  python3-zxing-cpp
  ghostscript
  qpdf
  unpaper
)

usage() {
  cat <<'EOF'
KienzleDoku OCR-Abhängigkeiten installieren oder prüfen.

Verwendung:
  sudo ./scripts/install-ocr-dependencies.sh
  ./scripts/install-ocr-dependencies.sh --check
  ./scripts/install-ocr-dependencies.sh --help

Ohne Option werden OCRmyPDF/Tesseract sowie die Data-Matrix-Abhängigkeiten
über apt-get installiert. Die bereits mit T2med ausgelieferten AMDB-Zugänge
werden geprüft, aber nicht verändert. --check verändert das System nicht.
EOF
}

verify_dependencies() {
  local failed=0
  local command_name
  local languages=""
  local help_text=""
  local required_option
  local amdb_client="${T2MED_AMDB_CLIENT:-/opt/t2med/server/mariadb/bin/mariadb}"
  local amdb_config="${T2MED_AMDB_CONFIG:-/opt/t2med/server/mmi/service.conf}"
  local amdb_socket="${T2MED_AMDB_SOCKET:-/var/opt/t2med/data/mariadb/t2med-mariadb}"
  local amdb_schema=""

  echo "Prüfe OCR-Programme ..."
  for command_name in python3 ocrmypdf tesseract pdftotext pdftoppm gs qpdf unpaper; do
    if command -v "$command_name" >/dev/null 2>&1; then
      printf '  [OK] %s: %s\n' "$command_name" "$(command -v "$command_name")"
    else
      printf '  [FEHLT] %s\n' "$command_name" >&2
      failed=1
    fi
  done

  if command -v tesseract >/dev/null 2>&1; then
    languages="$(tesseract --list-langs 2>&1 || true)"
    for required_option in deu eng osd; do
      if grep -Fxq "$required_option" <<<"$languages"; then
        printf '  [OK] Tesseract-Sprache: %s\n' "$required_option"
      else
        printf '  [FEHLT] Tesseract-Sprache: %s\n' "$required_option" >&2
        failed=1
      fi
    done
  fi

  if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'from PIL import Image; import zxingcpp; assert callable(zxingcpp.read_barcodes)' >/dev/null 2>&1; then
      echo "  [OK] Python kann Pillow und zxingcpp laden (QR/Data Matrix)"
    else
      echo "  [FEHLT] Python-Module Pillow oder zxingcpp" >&2
      failed=1
    fi
  fi

  if command -v ocrmypdf >/dev/null 2>&1; then
    help_text="$(ocrmypdf --help 2>&1 || true)"
    if [[ "$help_text" == *"--mode"* || "$help_text" == *"--skip-text"* ]]; then
      echo "  [OK] OCRmyPDF kann vorhandene Textseiten beibehalten"
    else
      echo "  [FEHLT] OCRmyPDF unterstützt weder --mode noch --skip-text" >&2
      failed=1
    fi
    for required_option in \
      --rotate-pages \
      --rotate-pages-threshold \
      --deskew \
      --clean \
      --oversample \
      --output-type \
      --optimize \
      --tesseract-timeout \
      --jobs; do
      if [[ "$help_text" != *"$required_option"* ]]; then
        printf '  [FEHLT] OCRmyPDF-Option: %s\n' "$required_option" >&2
        failed=1
      fi
    done
    if [[ "$help_text" != *"--pages"* ]]; then
      echo "  [FEHLT] OCRmyPDF-Option: --pages" >&2
      failed=1
    fi
  fi

  if command -v qpdf >/dev/null 2>&1; then
    qpdf_help="$(qpdf --help=all 2>&1 || qpdf --help 2>&1 || true)"
    if [[ "$qpdf_help" == *"--rotate"* && "$qpdf_help" == *"--flatten-rotation"* ]]; then
      echo "  [OK] qpdf kann einzelne PDF-Seiten drehen"
    else
      echo "  [FEHLT] qpdf unterstützt --rotate/--flatten-rotation nicht" >&2
      failed=1
    fi
  fi

  echo "Prüfe lokalen T2med-AMDB-Zugang (nur lesende Nutzung) ..."
  if [[ -x "$amdb_client" ]]; then
    printf '  [OK] T2med-MariaDB-Client: %s\n' "$amdb_client"
  else
    printf '  [FEHLT] T2med-MariaDB-Client: %s\n' "$amdb_client" >&2
    failed=1
  fi
  if [[ -r "$amdb_config" ]]; then
    amdb_schema="$(sed -n 's/^[[:space:]]*dball\.dbschema[[:space:]]*=[[:space:]]*//p' "$amdb_config" | tail -1)"
    if [[ "$amdb_schema" =~ ^[A-Za-z0-9_]+$ ]]; then
      printf '  [OK] Aktives T2med-AMDB-Schema: %s (%s)\n' "$amdb_schema" "$amdb_config"
    else
      printf '  [FEHLT] Gültiges dball.dbschema in %s\n' "$amdb_config" >&2
      failed=1
    fi
  else
    printf '  [FEHLT] T2med-MMI-Konfiguration: %s\n' "$amdb_config" >&2
    failed=1
  fi
  if [[ -e "$amdb_socket" ]]; then
    printf '  [OK] T2med-MariaDB-Socket: %s\n' "$amdb_socket"
  else
    printf '  [FEHLT] T2med-MariaDB-Socket: %s\n' "$amdb_socket" >&2
    failed=1
  fi

  if (( failed != 0 )); then
    echo "OCR-Abhängigkeiten sind nicht vollständig." >&2
    return 1
  fi

  echo "OCR-Abhängigkeiten sind vollständig."
  ocrmypdf --version
  tesseract --version
  pdftotext -v 2>&1
  gs --version
  qpdf --version
  unpaper --version
}

main() {
  local mode="install"

  if (( $# > 1 )); then
    usage >&2
    return 2
  fi
  if (( $# == 1 )); then
    case "$1" in
      --check)
        mode="check"
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        echo "Unbekannte Option: $1" >&2
        usage >&2
        return 2
        ;;
    esac
  fi

  echo "KienzleDoku OCR-Abhängigkeiten v${VERSION}"
  if [[ "$mode" == "check" ]]; then
    verify_dependencies
    return
  fi

  if (( EUID != 0 )); then
    echo "Bitte als root ausführen, zum Beispiel mit sudo." >&2
    return 1
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Dieser Installer unterstützt Debian/Ubuntu mit apt-get." >&2
    return 1
  fi

  echo "Aktualisiere Paketlisten ..."
  apt-get update

  echo "Installiere KienzleFax-OCR- und Data-Matrix-Stack ..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y "${PACKAGES[@]}"

  verify_dependencies
}

main "$@"
