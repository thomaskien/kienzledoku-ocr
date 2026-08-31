"""Generic QR/Data-Matrix extraction for PDF and raster image documents."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


SUPPORTED_IMAGE_FORMATS = {"PNG", "JPEG", "TIFF"}


@dataclass(frozen=True)
class QrCode:
    page: int
    code_type: str
    data: bytes
    text: Optional[str]
    rect: dict[str, int]
    polygon: tuple[dict[str, int], ...]
    dpi: Optional[int]
    retry: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "type": self.code_type,
            "text": self.text,
            "base64": base64.b64encode(self.data).decode("ascii"),
            "rect": self.rect,
            "polygon": list(self.polygon),
            "dpi": self.dpi,
            "retry": self.retry,
        }


@dataclass(frozen=True)
class QrExtractionResult:
    source: str
    codes: tuple[QrCode, ...]
    errors: tuple[dict[str, Any], ...]
    pages_scanned: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "codes": [code.as_dict() for code in self.codes],
            "errors": list(self.errors),
            "pagesScanned": self.pages_scanned,
        }


def _dependencies() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "Python-Paket Pillow fehlt (Ubuntu: python3-pil)"
        ) from exc
    try:
        import zxingcpp
    except ImportError as exc:
        raise RuntimeError(
            "Python-Paket zxingcpp fehlt (Ubuntu: python3-zxing-cpp)"
        ) from exc
    return Image, ImageOps, zxingcpp


def _format_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name)
    text = str(value)
    return text.rsplit(".", 1)[-1].replace(" ", "")


def _point(point: Any) -> dict[str, int]:
    return {"x": int(point.x), "y": int(point.y)}


def _geometry(position: Any) -> tuple[dict[str, int], tuple[dict[str, int], ...]]:
    points = tuple(
        _point(getattr(position, name))
        for name in ("top_left", "top_right", "bottom_right", "bottom_left")
    )
    xs = [point["x"] for point in points]
    ys = [point["y"] for point in points]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return (
        {
            "left": left,
            "top": top,
            "width": right - left,
            "height": bottom - top,
        },
        points,
    )


def _decode_image(
    image: Any,
    *,
    page: int,
    dpi: Optional[int],
    retry: bool,
    zxingcpp: Any,
) -> list[QrCode]:
    found: list[QrCode] = []
    seen: set[tuple[str, bytes, tuple[tuple[str, int], ...]]] = set()
    results = zxingcpp.read_barcodes(
        image,
        try_rotate=True,
        try_downscale=True,
        try_invert=True,
    )
    for barcode in results:
        raw = bytes(barcode.bytes)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        rect, polygon = _geometry(barcode.position)
        code_type = _format_name(barcode.format)
        identity = (code_type, raw, tuple(sorted(rect.items())))
        if identity in seen:
            continue
        seen.add(identity)
        found.append(
            QrCode(
                page=page,
                code_type=code_type,
                data=raw,
                text=text,
                rect=rect,
                polygon=polygon,
                dpi=dpi,
                retry=retry,
            )
        )
    return found


def _page_number(path: Path) -> int:
    match = re.search(r"-([0-9]+)\.png$", path.name)
    if match is None:
        raise ValueError(f"Seitennummer nicht lesbar: {path.name}")
    return int(match.group(1))


def _run_renderer(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def _error(page: Optional[int], stage: str, exc: Any) -> dict[str, Any]:
    return {
        "page": page,
        "stage": stage,
        "error": str(exc)[:1000],
    }


def _extract_pdf(
    source: Path,
    *,
    pdftoppm: str,
    dpi: int,
    retry_dpi: int,
    timeout: float,
    decoder: Callable[..., list[QrCode]],
    Image: Any,
    zxingcpp: Any,
) -> QrExtractionResult:
    codes: list[QrCode] = []
    errors: list[dict[str, Any]] = []
    pages_scanned = 0
    with tempfile.TemporaryDirectory(prefix="kienzledoku-qr-pdf-") as tmp:
        root = Path(tmp)
        prefix = root / "page"
        try:
            rendered = _run_renderer(
                [pdftoppm, "-png", "-gray", "-r", str(dpi), str(source), str(prefix)],
                timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return QrExtractionResult(
                str(source), (), (_error(None, "pdf_render", exc),), 0
            )
        if rendered.returncode != 0:
            detail = rendered.stderr.decode("utf-8", "replace").strip()
            errors.append(
                _error(None, "pdf_render", detail or f"Status {rendered.returncode}")
            )

        page_images: list[tuple[int, Path]] = []
        for image_path in root.glob("page-*.png"):
            try:
                page_images.append((_page_number(image_path), image_path))
            except ValueError as exc:
                errors.append(_error(None, "page_number", exc))
        page_images.sort()
        if not page_images:
            errors.append(_error(None, "pdf_render", "pdftoppm erzeugte keine Seitenbilder"))

        for page, image_path in page_images:
            pages_scanned = max(pages_scanned, page)
            page_codes: list[QrCode] = []
            try:
                with Image.open(image_path) as image:
                    image.load()
                    page_codes = decoder(
                        image,
                        page=page,
                        dpi=dpi,
                        retry=False,
                        zxingcpp=zxingcpp,
                    )
            except Exception as exc:
                errors.append(_error(page, "decode", exc))

            if not page_codes and retry_dpi > dpi:
                retry_prefix = root / f"retry-{page}"
                try:
                    retry_rendered = _run_renderer(
                        [
                            pdftoppm,
                            "-f",
                            str(page),
                            "-l",
                            str(page),
                            "-singlefile",
                            "-png",
                            "-gray",
                            "-r",
                            str(retry_dpi),
                            str(source),
                            str(retry_prefix),
                        ],
                        timeout,
                    )
                    retry_image = retry_prefix.with_suffix(".png")
                    if retry_rendered.returncode != 0 or not retry_image.is_file():
                        detail = retry_rendered.stderr.decode("utf-8", "replace").strip()
                        raise RuntimeError(
                            detail or f"pdftoppm-Status {retry_rendered.returncode}"
                        )
                    with Image.open(retry_image) as image:
                        image.load()
                        page_codes = decoder(
                            image,
                            page=page,
                            dpi=retry_dpi,
                            retry=True,
                            zxingcpp=zxingcpp,
                        )
                except Exception as exc:
                    errors.append(_error(page, "retry", exc))
            codes.extend(page_codes)

    return QrExtractionResult(
        str(source), tuple(codes), tuple(errors), pages_scanned
    )


def _extract_image(
    source: Path,
    *,
    retry_scale: float,
    decoder: Callable[..., list[QrCode]],
    Image: Any,
    ImageOps: Any,
    zxingcpp: Any,
) -> QrExtractionResult:
    codes: list[QrCode] = []
    errors: list[dict[str, Any]] = []
    pages_scanned = 0
    try:
        image_file = Image.open(source)
    except Exception as exc:
        return QrExtractionResult(
            str(source), (), (_error(None, "image_open", exc),), 0
        )

    with image_file:
        image_format = (image_file.format or "").upper()
        if image_format not in SUPPORTED_IMAGE_FORMATS:
            return QrExtractionResult(
                str(source), (), (_error(None, "file_type", image_format),), 0
            )
        frames = int(getattr(image_file, "n_frames", 1))
        for index in range(frames):
            page = index + 1
            pages_scanned = page
            try:
                image_file.seek(index)
                image = ImageOps.exif_transpose(image_file.copy()).convert("RGB")
                page_codes = decoder(
                    image,
                    page=page,
                    dpi=None,
                    retry=False,
                    zxingcpp=zxingcpp,
                )
                if not page_codes and retry_scale > 1:
                    enlarged = image.resize(
                        (
                            max(1, round(image.width * retry_scale)),
                            max(1, round(image.height * retry_scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                    page_codes = decoder(
                        enlarged,
                        page=page,
                        dpi=None,
                        retry=True,
                        zxingcpp=zxingcpp,
                    )
                codes.extend(page_codes)
            except Exception as exc:
                errors.append(_error(page, "decode", exc))
    return QrExtractionResult(
        str(source), tuple(codes), tuple(errors), pages_scanned
    )


def extract_qr_codes(
    source: str | Path,
    *,
    pdftoppm: str = "pdftoppm",
    dpi: int = 300,
    retry_dpi: int = 600,
    retry_scale: float = 2.0,
    timeout: float = 300.0,
    decoder: Optional[Callable[..., list[QrCode]]] = None,
) -> QrExtractionResult:
    """Return every barcode payload without making domain-specific assumptions."""
    path = Path(source)
    if dpi < 72 or retry_dpi < dpi:
        raise ValueError("Ungültige PDF-Renderauflösung")
    if retry_scale < 1 or timeout <= 0:
        raise ValueError("Ungültige QR-Wiederholungsoption")
    if not path.is_file():
        return QrExtractionResult(
            str(path), (), (_error(None, "source", "Datei nicht gefunden"),), 0
        )

    try:
        Image, ImageOps, zxingcpp = _dependencies()
    except RuntimeError as exc:
        return QrExtractionResult(
            str(path), (), (_error(None, "dependency", exc),), 0
        )
    decode = decoder or _decode_image
    try:
        with path.open("rb") as stream:
            header = stream.read(8)
    except OSError as exc:
        return QrExtractionResult(
            str(path), (), (_error(None, "source", exc),), 0
        )
    if header.startswith(b"%PDF-"):
        return _extract_pdf(
            path,
            pdftoppm=pdftoppm,
            dpi=dpi,
            retry_dpi=retry_dpi,
            timeout=timeout,
            decoder=decode,
            Image=Image,
            zxingcpp=zxingcpp,
        )
    return _extract_image(
        path,
        retry_scale=retry_scale,
        decoder=decode,
        Image=Image,
        ImageOps=ImageOps,
        zxingcpp=zxingcpp,
    )


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="QR- und Data-Matrix-Codes aus PDF/PNG/JPEG/TIFF extrahieren"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--pdftoppm", default="pdftoppm")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--retry-dpi", type=int, default=600)
    parser.add_argument("--retry-scale", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        result = extract_qr_codes(
            args.source,
            pdftoppm=args.pdftoppm,
            dpi=args.dpi,
            retry_dpi=args.retry_dpi,
            retry_scale=args.retry_scale,
            timeout=args.timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 2 if result.errors and not result.codes else 0


if __name__ == "__main__":
    raise SystemExit(_main())
