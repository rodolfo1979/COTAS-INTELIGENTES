from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas

PdfBBox = tuple[float, float, float, float]


DIMENSION_RE = re.compile(
    r"""
    ^
    (?:
      (?:[0-9]+x\s*)?
      (?:
        (?:\+/-|\u00b1)?\s*[rms]?\s*(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)
        |
        [0-9]+/[0-9]+
        |
        [0-9]+\s*-\s*[0-9]+
      )
      (?:\s*(?:mm|cm|in|deg|degree|degrees|grados|"|\u00b0)?)?
      (?:\s*(?:\+/-|\u00b1|\+|-)\s*(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+))?
      (?:\s*/\s*(?:\+|-)?\s*[0-9]+(?:[.,][0-9]+)?)?
    )
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

NOT_DIMENSION_RE = re.compile(
    r"(?i)^(?:rev|revision|sheet|page|date|scale|material|qty|cantidad|cliente|client)$"
)
NON_DIMENSION_LINE_RE = re.compile(
    r"(?i)\b(?:rev|revision|sheet|page|date|scale|material|qty|cantidad|cliente|client)\b"
)


@dataclass
class DimensionCandidate:
    number: int
    page: int
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float
    reason: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    return (
        text.strip()
        .replace(",", ".")
        .replace("\u00d8", "R")
        .replace("\u2300", "R")
        .replace("\u00b0", "deg")
    )


def looks_like_dimension(
    raw_text: str,
    line_text: str = "",
    neighbor_text: str = "",
) -> tuple[bool, float, str]:
    
    text = normalize_text(raw_text)
    compact = re.sub(r"\s+", "", text)

    if not compact or NOT_DIMENSION_RE.match(compact):
        return False, 0.0, "ignored label"

    if not re.search(r"\d", compact):
        return False, 0.0, "no digits"

    line_tokens = [token for token in re.split(r"\s+", line_text.strip()) if token]
    context_text = normalize_text(" ".join(part for part in [line_text, neighbor_text] if part))
    line_has_unit_nearby = bool(
        re.search(r"(?i)\b(mm|cm|in|deg|grados|aprox|approx)\b|\"", context_text)
    )

    if compact in {"99", "01", "0.0", "00"}:
        return False, 0.0, "ignored ocr artifact"
    if re.match(r"^[0-9]{3}$", compact) and compact not in {"110"} and not line_has_unit_nearby:
        return False, 0.0, "ignored unlikely ocr integer"

    has_strong_marker = bool(
        re.search(r"(?i)(^r|^m|x|\+/-|\u00b1|\+|-|/|mm|cm|in|deg|grados|\")", compact)
    )
    if re.match(r"^[0-9]$", compact) and not has_strong_marker and not line_has_unit_nearby:
        return False, 0.0, "ignored single digit"

    if line_text and NON_DIMENSION_LINE_RE.search(line_text) and not has_strong_marker:
        return False, 0.0, "ignored title block or note line"

    is_plain_number = bool(re.match(r"^[0-9]+(?:[.,][0-9]+)?$", compact))
    if is_plain_number and len(line_tokens) > 12 and not line_has_unit_nearby:
        return False, 0.0, "plain number inside note"

    if DIMENSION_RE.match(compact):
        if re.match(r"^[0-9]{4,}$", compact):
            return False, 0.0, "plain long integer"

        confidence = 0.74
        reason = "dimension pattern"
        if re.search(r"(?i)(mm|cm|in|deg|grados|\")", compact):
            confidence += 0.08
            reason += " with unit"
        if re.search(r"(?i)(^r|^m|x|\+/-|\u00b1|\+|-|/)", compact):
            confidence += 0.08
            reason += " with technical marker"
        return True, min(confidence, 0.96), reason

    if is_plain_number:
        confidence = 0.72 if line_has_unit_nearby else 0.62
        reason = "plain numeric value with nearby unit" if line_has_unit_nearby else "plain numeric value"
        return True, confidence, reason

    return False, 0.0, "not a dimension"


def word_center(word: dict[str, Any]) -> tuple[float, float]:
    return (
        (float(word["x0"]) + float(word["x1"])) / 2,
        (float(word["top"]) + float(word["bottom"])) / 2,
    )


def point_inside_bbox(x: float, y: float, bbox: PdfBBox, padding: float = 2.0) -> bool:
    x0, top, x1, bottom = bbox
    return (x0 - padding) <= x <= (x1 + padding) and (top - padding) <= y <= (bottom + padding)


def word_inside_any_bbox(word: dict[str, Any], bboxes: list[PdfBBox]) -> bool:
    x, y = word_center(word)
    return any(point_inside_bbox(x, y, bbox) for bbox in bboxes)


def detected_table_bboxes(page: pdfplumber.page.Page) -> list[PdfBBox]:
    bboxes: list[PdfBBox] = []
    try:
        tables = page.find_tables()
    except Exception:
        return bboxes

    page_area = float(page.width) * float(page.height)
    for table in tables:
        x0, top, x1, bottom = [float(value) for value in table.bbox]
        width = x1 - x0
        height = bottom - top
        if width <= 0 or height <= 0:
            continue

        area_ratio = (width * height) / page_area
        page_width_ratio = width / float(page.width)
        page_height_ratio = height / float(page.height)

        # pdfplumber can mistake the drawing border/grid for one huge table.
        # If we exclude that box, every dimension inside the drawing disappears.
        if area_ratio > 0.60 or (page_width_ratio > 0.85 and page_height_ratio > 0.75):
            continue

        # Avoid treating a small dimension callout box as a table. Real title
        # blocks and reference tables usually occupy a meaningful page area.
        if area_ratio < 0.015:
            continue

        near_page_edge = (
            bottom >= float(page.height) * 0.72
            or x0 <= float(page.width) * 0.06
            or x1 >= float(page.width) * 0.94
        )
        if not near_page_edge:
            continue

        bboxes.append((x0, top, x1, bottom))

    return bboxes


def nearby_line_words(words: list[dict[str, Any]], word: dict[str, Any]) -> list[dict[str, Any]]:
    top = float(word.get("top", 0))
    peers = [peer for peer in words if abs(float(peer.get("top", 0)) - top) < 3]
    return sorted(peers, key=lambda item: float(item.get("x0", 0)))


def nearby_context(line_words: list[dict[str, Any]], word: dict[str, Any]) -> str:
    try:
        index = line_words.index(word)
    except ValueError:
        return ""

    start = max(0, index - 1)
    end = min(len(line_words), index + 2)
    return " ".join(str(peer.get("text", "")) for peer in line_words[start:end])


def word_bbox_from_pdf_word(word: dict[str, Any]) -> dict[str, float]:
    return {
        "x": float(word["x0"]),
        "y": float(word["top"]),
        "width": float(word["x1"] - word["x0"]),
        "height": float(word["bottom"] - word["top"]),
    }


def union_pdf_words(words: list[dict[str, Any]]) -> dict[str, float]:
    xs = [float(word["x0"]) for word in words]
    ys = [float(word["top"]) for word in words]
    rights = [float(word["x1"]) for word in words]
    bottoms = [float(word["bottom"]) for word in words]
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(rights) - min(xs),
        "height": max(bottoms) - min(ys),
    }


def reverse_rotated_text(text: str) -> str:
    return normalize_text(text[::-1])


def extract_rotated_word_candidates(
    words: list[dict[str, Any]],
    page_index: int,
    page_width: float,
    page_height: float,
    table_bboxes: list[PdfBBox] | None = None,
) -> list[dict[str, Any]]:
    table_bboxes = table_bboxes or []
    rotated_words = [
        word
        for word in words
        if not bool(word.get("upright", True)) and not word_inside_any_bbox(word, table_bboxes)
    ]
    rotated_words.sort(key=lambda item: (float(item.get("x0", 0)), float(item.get("top", 0))))

    candidates: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for index, word in enumerate(rotated_words):
        if index in consumed:
            continue

        text = reverse_rotated_text(str(word.get("text", ""))).strip()
        if not text:
            continue

        phrase_words = [word]
        phrase_text = text
        if is_prefix_dimension_token(text):
            best_value_index: int | None = None
            best_gap = 999.0
            for other_index, other in enumerate(rotated_words):
                if other_index == index or other_index in consumed:
                    continue
                if abs(float(other.get("x0", 0)) - float(word.get("x0", 0))) > 2.5:
                    continue

                other_text = reverse_rotated_text(str(other.get("text", ""))).strip()
                ok, _, _ = looks_like_dimension(other_text)
                if not ok:
                    continue

                gap = abs(float(word.get("top", 0)) - float(other.get("bottom", 0)))
                if gap < best_gap and gap <= 8:
                    best_gap = gap
                    best_value_index = other_index

            if best_value_index is not None:
                phrase_words.append(rotated_words[best_value_index])
                phrase_text = f"{text} {reverse_rotated_text(str(rotated_words[best_value_index].get('text', ''))).strip()}"
                consumed.add(best_value_index)
        else:
            best_suffix_index: int | None = None
            best_suffix_gap = 999.0
            for other_index, other in enumerate(rotated_words):
                if other_index == index or other_index in consumed:
                    continue
                if abs(float(other.get("x0", 0)) - float(word.get("x0", 0))) > 2.5:
                    continue

                other_text = reverse_rotated_text(str(other.get("text", ""))).strip()
                if not is_suffix_dimension_token(other_text):
                    continue

                gap = abs(float(other.get("top", 0)) - float(word.get("bottom", 0)))
                if gap < best_suffix_gap and gap <= 9:
                    best_suffix_gap = gap
                    best_suffix_index = other_index

            if best_suffix_index is not None:
                phrase_words.append(rotated_words[best_suffix_index])
                phrase_text = f"{text} {reverse_rotated_text(str(rotated_words[best_suffix_index].get('text', ''))).strip()}"
                consumed.add(best_suffix_index)

            best_prefix_index: int | None = None
            best_gap = 999.0
            for other_index, other in enumerate(rotated_words):
                if other_index == index or other_index in consumed:
                    continue
                if abs(float(other.get("x0", 0)) - float(word.get("x0", 0))) > 2.5:
                    continue

                other_text = reverse_rotated_text(str(other.get("text", ""))).strip()
                if not is_prefix_dimension_token(other_text):
                    continue

                gap = abs(float(other.get("top", 0)) - float(word.get("bottom", 0)))
                if gap < best_gap and gap <= 8:
                    best_gap = gap
                    best_prefix_index = other_index

            if best_prefix_index is not None:
                phrase_words.append(rotated_words[best_prefix_index])
                phrase_text = f"{reverse_rotated_text(str(rotated_words[best_prefix_index].get('text', ''))).strip()} {text}"
                consumed.add(best_prefix_index)

        ok, confidence, reason = looks_like_dimension(phrase_text, phrase_text, phrase_text)
        if not ok:
            continue

        box = union_pdf_words(phrase_words)
        in_title_block = box["x"] >= page_width * 0.55 and box["y"] >= page_height * 0.86
        if in_title_block:
            continue

        consumed.add(index)
        candidates.append(
            {
                "page": page_index,
                "text": phrase_text,
                **box,
                "confidence": round(min(confidence + 0.04, 0.96), 3),
                "reason": f"rotated {reason}",
            }
        )

    return candidates


def is_prefix_dimension_token(text: str) -> bool:
    compact = normalize_text(text).upper()
    return bool(re.match(r"^[0-9]+X$", compact))


def is_suffix_dimension_token(text: str) -> bool:
    compact = normalize_text(text).upper().strip(",")
    return bool(
        compact
        in {
            "THRU",
            "ALL",
            "UNC",
            "UNF",
            "NEAR",
            "FAR",
            "SIDE",
            "WALL",
            "TYP",
            "MM",
            "CM",
            "IN",
            "APROX",
            "APROX.",
            "APPROX",
            "APPROX.",
            "EQ",
            "SP",
            "PLCS",
            "PLACES",
            "DEG",
            "°",
            "+",
            "-",
        }
        or re.match(r"^\([0-9]+\)$", compact)
    )


def expanded_dimension_phrase(
    ordered_words: list[dict[str, Any]],
    index: int,
) -> tuple[str, dict[str, float], list[int]]:
    start = index
    end = index

    while start > 0:
        previous = ordered_words[start - 1]
        current = ordered_words[start]
        gap = float(current["x0"]) - float(previous["x1"])
        if gap <= 18 and is_prefix_dimension_token(str(previous.get("text", ""))):
            start -= 1
            continue
        break

    while end + 1 < len(ordered_words):
        current = ordered_words[end]
        next_word = ordered_words[end + 1]
        gap = float(next_word["x0"]) - float(current["x1"])
        next_text = str(next_word.get("text", ""))
        current_text = normalize_text(str(current.get("text", ""))).upper().strip(",")
        next_is_new_dimension, _, _ = looks_like_dimension(next_text)

        include_next = gap <= 30 and (
            is_suffix_dimension_token(next_text)
            or (current_text == "X" and re.match(r"^[0-9]+°?,?$", normalize_text(next_text)))
        )
        if not include_next or (next_is_new_dimension and not is_suffix_dimension_token(next_text)):
            break

        end += 1
        if end - start >= 7:
            break

    phrase_words = ordered_words[start : end + 1]
    phrase = " ".join(str(word.get("text", "")).strip() for word in phrase_words if str(word.get("text", "")).strip())
    return phrase, union_pdf_words(phrase_words), list(range(start, end + 1))


def dedupe_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def boxes_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
        ax0, ay0 = a["x"], a["y"]
        ax1, ay1 = a["x"] + a["width"], a["y"] + a["height"]
        bx0, by0 = b["x"], b["y"]
        bx1, by1 = b["x"] + b["width"], b["y"] + b["height"]
        return ax0 <= bx1 and ax1 >= bx0 and ay0 <= by1 and ay1 >= by0

    unique: list[dict[str, Any]] = []
    for item in items:
        duplicate = False
        for existing in unique:
            if item["page"] != existing["page"]:
                continue

            same_text = normalize_text(item["text"]).lower() == normalize_text(existing["text"]).lower()
            near_x = abs(item["x"] - existing["x"]) <= 3
            near_y = abs(item["y"] - existing["y"]) <= 3
            close_x = abs(item["x"] - existing["x"]) <= 8
            close_y = abs(item["y"] - existing["y"]) <= 8
            item_center = item["x"] + item["width"] / 2
            existing_center = existing["x"] + existing["width"] / 2
            overlaps = abs(item_center - existing_center) <= 6
            if same_text and (boxes_overlap(item, existing) or (near_y and (near_x or overlaps)) or (close_x and close_y)):
                duplicate = True
                break

        if not duplicate:
            unique.append(item)

    return unique


def remove_grouped_digit_artifacts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in items:
        compact = re.sub(r"\s+", "", normalize_text(str(item.get("text", ""))))
        if item.get("reason") == "grouped digit fragments" and re.match(r"^[0-9]+$", compact):
            continue
        cleaned.append(item)
    return cleaned


def union_word_box(words: list[dict[str, Any]]) -> dict[str, float]:
    xs = [float(word["x"]) for word in words]
    ys = [float(word["y"]) for word in words]
    rights = [float(word["x"]) + float(word["width"]) for word in words]
    bottoms = [float(word["y"]) + float(word["height"]) for word in words]
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(rights) - min(xs),
        "height": max(bottoms) - min(ys),
    }


def merge_ocr_decimal_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_line: dict[str, list[dict[str, Any]]] = {}
    for word in words:
        by_line.setdefault(str(word.get("line", "")), []).append(word)

    for line, line_words in by_line.items():
        ordered = sorted(line_words, key=lambda item: float(item["x"]))
        used: set[int] = set()
        for index, word in enumerate(ordered):
            if index in used:
                continue

            text = normalize_text(str(word.get("text", "")))
            if (
                index + 1 < len(ordered)
                and re.match(r"^[0-9]{2,3}$", text)
                and re.match(r"^[0-9]$", normalize_text(str(ordered[index + 1].get("text", ""))))
            ):
                next_word = ordered[index + 1]
                gap = float(next_word["x"]) - (float(word["x"]) + float(word["width"]))
                same_baseline = abs(float(next_word["y"]) - float(word["y"])) <= 10
                if 0 <= gap <= 24 and same_baseline:
                    box = union_word_box([word, next_word])
                    merged.append(
                        {
                            **word,
                            **box,
                            "text": f"{text}.{normalize_text(str(next_word.get('text', '')))}",
                            "line": line,
                        }
                    )
                    used.update({index, index + 1})
                    continue

            merged.append(word)
            used.add(index)

    return merged


def merge_digit_fragments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    singles = [
        item
        for item in items
        if re.match(r"^[0-9]$", normalize_text(item["text"])) and item.get("confidence", 0) <= 0.75
    ]
    consumed: set[int] = set()
    merged: list[dict[str, Any]] = []

    def center(item: dict[str, Any]) -> tuple[float, float]:
        return (item["x"] + item["width"] / 2, item["y"] + item["height"] / 2)

    for index, item in enumerate(items):
        if index in consumed:
            continue

        if item not in singles:
            merged.append(item)
            continue

        x, y = center(item)
        group_indexes = [index]
        for other_index, other in enumerate(items):
            if other_index == index or other_index in consumed or other not in singles:
                continue
            if other["page"] != item["page"]:
                continue

            ox, oy = center(other)
            same_vertical_number = abs(ox - x) <= 8 and abs(oy - y) <= 42
            same_horizontal_number = abs(oy - y) <= 8 and abs(ox - x) <= 42
            if same_vertical_number or same_horizontal_number:
                group_indexes.append(other_index)

        if len(group_indexes) < 2:
            merged.append(item)
            continue

        group = [items[group_index] for group_index in group_indexes]
        xs = [entry["x"] for entry in group]
        ys = [entry["y"] for entry in group]
        rights = [entry["x"] + entry["width"] for entry in group]
        bottoms = [entry["y"] + entry["height"] for entry in group]
        vertical = (max(xs) - min(xs)) <= 8
        ordered = sorted(group, key=lambda entry: entry["y"] if vertical else entry["x"])

        for group_index in group_indexes:
            consumed.add(group_index)

        merged.append(
            {
                "page": item["page"],
                "text": "".join(entry["text"] for entry in ordered),
                "x": min(xs),
                "y": min(ys),
                "width": max(rights) - min(xs),
                "height": max(bottoms) - min(ys),
                "confidence": max(entry["confidence"] for entry in group),
                "reason": "grouped digit fragments",
            }
        )

    return merged


def preprocess_for_ocr(source: Path, target: Path, scale: int = 5) -> None:
    image = Image.open(source).convert("L")
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(2.2)
    image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
    image = image.filter(ImageFilter.SHARPEN)
    
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)


def build_ocr_variants(source: Path, output_dir: Path) -> list[tuple[Path, tuple[float, float, float, float], int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    variants: list[tuple[Path, tuple[float, float, float, float], int]] = []

    with Image.open(source) as original:
        width, height = original.size
        crops = [(0, 0, width, height)]

        # Overlapping zones help Windows OCR detect small drawing dimensions.
        half_w = width // 2
        half_h = height // 2
        overlap_x = max(40, width // 12)
        overlap_y = max(30, height // 12)
        crops.extend(
            [
                (0, 0, min(width, half_w + overlap_x), min(height, half_h + overlap_y)),
                (max(0, half_w - overlap_x), 0, width, min(height, half_h + overlap_y)),
                (0, max(0, half_h - overlap_y), min(width, half_w + overlap_x), height),
                (max(0, half_w - overlap_x), max(0, half_h - overlap_y), width, height),
            ]
        )

        if width > 700 or height > 400:
            third_w = width // 3
            third_h = height // 3
            for row in range(3):
                for col in range(3):
                    left = max(0, col * third_w - overlap_x)
                    top = max(0, row * third_h - overlap_y)
                    right = min(width, (col + 1) * third_w + overlap_x)
                    bottom = min(height, (row + 1) * third_h + overlap_y)
                    crops.append((left, top, right, bottom))

        seen: set[tuple[int, int, int, int]] = set()
        for index, crop_box in enumerate(crops):
            normalized = tuple(int(value) for value in crop_box)
            if normalized in seen:
                continue
            seen.add(normalized)

            crop = original.crop(normalized)
            rotations = (0, 90, 270) if index <= 4 else (0,)
            for rotation in rotations:
                rotated = crop if rotation == 0 else crop.rotate(rotation, expand=True)
                crop_path = output_dir / f"{source.stem}_crop{index}_rot{rotation}.png"
                raw_crop_path = output_dir / f"{source.stem}_crop{index}_rot{rotation}_raw.png"
                rotated.save(raw_crop_path)
                preprocess_for_ocr(raw_crop_path, crop_path)
                raw_crop_path.unlink(missing_ok=True)
                variants.append((crop_path, normalized, rotation))

    return variants


def run_windows_ocr(image_path: Path) -> list[dict[str, Any]]:
    script = Path(__file__).with_name("windows_ocr.ps1")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not script.exists() or not powershell:
        return []

    try:
        result = subprocess.run(
            [
                powershell,
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                str(image_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []

    payload = json.loads(result.stdout, strict=False)
    if isinstance(payload, dict):
        return [payload]
    return payload


def extracted_page_images(pdf_path: Path, output_dir: Path) -> dict[int, list[Path]]:
    reader = PdfReader(str(pdf_path))
    extracted: dict[int, list[Path]] = {}
    output_dir.mkdir(parents=True, exist_ok=True)

    for page_index, page in enumerate(reader.pages, start=1):
        page_images: list[Path] = []
        for image_index, image in enumerate(page.images, start=1):
            ext = Path(image.name).suffix or ".jpg"
            target = output_dir / f"page{page_index}_image{image_index}{ext}"
            target.write_bytes(image.data)
            page_images.append(target)
        extracted[page_index] = page_images

    return extracted


def extract_ocr_candidates(pdf_path: Path) -> list[DimensionCandidate]:
    raw_candidates: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="cotas_ocr_") as temp_name:
        temp_dir = Path(temp_name)
        images_by_page = extracted_page_images(pdf_path, temp_dir / "images")

        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                page_images = images_by_page.get(page_index, [])
                for image_number, page_image in enumerate(page_images):
                    if image_number >= len(page.images):
                        continue

                    image_meta = page.images[image_number]
                    pdf_x0 = float(image_meta["x0"])
                    pdf_top = float(image_meta["top"])
                    pdf_width = float(image_meta["width"])
                    pdf_height = float(image_meta["height"])
                    with Image.open(page_image) as original_image:
                        original_width, original_height = original_image.size

                    variants = build_ocr_variants(page_image, temp_dir / "ocr" / page_image.stem)
                    for ocr_image, crop_box, rotation in variants:
                        ocr_words = merge_ocr_decimal_words(run_windows_ocr(ocr_image))
                        if not ocr_words:
                            continue

                        crop_left, crop_top, crop_right, crop_bottom = crop_box
                        with Image.open(ocr_image) as processed:
                            image_width, image_height = processed.size
                        crop_width = crop_right - crop_left
                        crop_height = crop_bottom - crop_top

                        for word in ocr_words:
                            text = str(word.get("text", "")).strip()
                            line_text = str(word.get("line", ""))
                            ok, confidence, reason = looks_like_dimension(text, line_text, line_text)
                            if not ok:
                                continue

                            word_x = float(word["x"]) / image_width
                            word_y = float(word["y"]) / image_height
                            word_w = float(word["width"]) / image_width
                            word_h = float(word["height"]) / image_height

                            if rotation == 0:
                                original_x = crop_left + word_x * crop_width
                                original_y = crop_top + word_y * crop_height
                                original_w = word_w * crop_width
                                original_h = word_h * crop_height
                            elif rotation == 90:
                               original_x = crop_left + (1 - word_y - word_h) * crop_width
                               original_y = crop_top + word_x * crop_height
                               original_w = word_h * crop_width
                               original_h = word_w * crop_height
                            else:  # 270
                               original_x = crop_left + word_y * crop_width
                               original_y = crop_top + (1 - word_x - word_w) * crop_height
                               original_w = word_h * crop_width
                               original_h = word_w * crop_height

                            raw_candidates.append(
                                {
                                    "page": page_index,
                                    "text": text,
                                    "x": pdf_x0 + (original_x / original_width) * pdf_width,
                                    "y": pdf_top + (original_y / original_height) * pdf_height,
                                    "width": (original_w / original_width) * pdf_width,
                                    "height": (original_h / original_height) * pdf_height,
                                    "confidence": round(max(confidence - 0.1, 0.5), 3),
                                    "reason": f"ocr {reason}",
                                }
                            )

    raw_candidates = merge_digit_fragments(raw_candidates)
    raw_candidates = remove_grouped_digit_artifacts(raw_candidates)
    raw_candidates = dedupe_candidates(raw_candidates)
    raw_candidates.sort(key=lambda item: (item["page"], item["y"], item["x"]))
    return [
        DimensionCandidate(number=index, **item)
        for index, item in enumerate(raw_candidates, start=1)
    ]


def extract_candidates(pdf_path: Path, include_tables: bool = False) -> list[DimensionCandidate]:
    raw_candidates: list[dict[str, Any]] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            table_bboxes = [] if include_tables else detected_table_bboxes(page)
            all_words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=[],
            )
            raw_candidates.extend(
                extract_rotated_word_candidates(
                    all_words,
                    page_index,
                    float(page.width),
                    float(page.height),
                    table_bboxes,
                )
            )
            words = [
                word
                for word in all_words
                if bool(word.get("upright", True)) and not word_inside_any_bbox(word, table_bboxes)
            ]
            lines: list[list[dict[str, Any]]] = []
            for word in sorted(words, key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))):
                if not lines or abs(float(lines[-1][0].get("top", 0)) - float(word.get("top", 0))) >= 3:
                    lines.append([word])
                else:
                    lines[-1].append(word)

            for line_words in lines:
                ordered_line = sorted(line_words, key=lambda item: float(item.get("x0", 0)))
                used_indexes: set[int] = set()
                line_text = " ".join(str(peer.get("text", "")) for peer in ordered_line)
                for index, word in enumerate(ordered_line):
                    if index in used_indexes:
                        continue

                    text = str(word.get("text", "")).strip()
                    compact_check = re.sub(r"\s+", "", text)
                    if re.match(r"^[0-9]{1,2}$", compact_check):
                        margin = page.height * 0.03
                        if word["top"] <= margin or word["bottom"] >= page.height - margin:
                            continue
                    neighbor_text = nearby_context(ordered_line, word)
                    ok, confidence, reason = looks_like_dimension(text, line_text, neighbor_text)
                    if not ok:
                        continue

                    phrase, box, phrase_indexes = expanded_dimension_phrase(ordered_line, index)
                    if phrase and phrase != text:
                        reason = "dimension phrase"
                        confidence = min(confidence + 0.08, 0.96)
                        used_indexes.update(phrase_indexes)

                    in_title_block = (
                        box["x"] >= float(page.width) * 0.55
                        and box["y"] >= float(page.height) * 0.86
                    )
                    if in_title_block:
                        continue

                    compact_phrase = re.sub(r"\s+", "", normalize_text(phrase or text))
                    is_stacked_tolerance = bool(re.match(r"^\.(?:0{2,3}|00[1-9])$", compact_phrase)) and any(
                        str(peer.get("text", "")).strip() in {"+", "-"}
                        and abs(float(peer.get("x0", 0)) - box["x"]) <= 28
                        and abs(float(peer.get("top", 0)) - box["y"]) <= 18
                        for peer in words
                    )
                    if is_stacked_tolerance:
                        continue

                    raw_candidates.append(
                        {
                            "page": page_index,
                            "text": phrase or text,
                            **box,
                            "confidence": round(confidence, 3),
                            "reason": reason,
                        }
                    )

    if not raw_candidates:
        return extract_ocr_candidates(pdf_path)

    raw_candidates = merge_digit_fragments(raw_candidates)
    raw_candidates = remove_grouped_digit_artifacts(raw_candidates)
    raw_candidates = dedupe_candidates(raw_candidates)
    raw_candidates.sort(key=lambda item: (item["page"], item["y"], item["x"]))
    return [
        DimensionCandidate(number=index, **item)
        for index, item in enumerate(raw_candidates, start=1)
    ]


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def boxes_intersect(a: PdfBBox, b: PdfBBox, padding: float = 0.0) -> bool:
    ax0, at, ax1, ab = a
    bx0, bt, bx1, bb = b
    return (
        ax0 - padding <= bx1
        and ax1 + padding >= bx0
        and at - padding <= bb
        and ab + padding >= bt
    )


def box_area(box: PdfBBox) -> float:
    x0, top, x1, bottom = box
    return max(0.0, x1 - x0) * max(0.0, bottom - top)


def intersection_area(a: PdfBBox, b: PdfBBox, padding: float = 0.0) -> float:
    ax0, at, ax1, ab = a
    bx0, bt, bx1, bb = b
    left = max(ax0 - padding, bx0)
    top = max(at - padding, bt)
    right = min(ax1 + padding, bx1)
    bottom = min(ab + padding, bb)
    return max(0.0, right - left) * max(0.0, bottom - top)


def point_in_box(x: float, y: float, box: PdfBBox, padding: float = 0.0) -> bool:
    x0, top, x1, bottom = box
    return (x0 - padding) <= x <= (x1 + padding) and (top - padding) <= y <= (bottom + padding)


def segment_intersects_box(
    start: tuple[float, float],
    end: tuple[float, float],
    box: PdfBBox,
    padding: float = 0.0,
) -> bool:
    x0, y0 = start
    x1, y1 = end
    bx0, bt, bx1, bb = box
    bx0 -= padding
    bt -= padding
    bx1 += padding
    bb += padding

    if point_in_box(x0, y0, (bx0, bt, bx1, bb)) or point_in_box(x1, y1, (bx0, bt, bx1, bb)):
        return True

    dx = x1 - x0
    dy = y1 - y0
    checks: list[tuple[str, float]] = []
    if dx:
        checks.extend([("x", bx0), ("x", bx1)])
    if dy:
        checks.extend([("y", bt), ("y", bb)])

    for axis, value in checks:
        t = (value - x0) / dx if axis == "x" else (value - y0) / dy
        if 0.0 <= t <= 1.0:
            x = x0 + t * dx
            y = y0 + t * dy
            if bx0 <= x <= bx1 and bt <= y <= bb:
                return True
    return False


def line_start_outside_source_box(
    source_box: PdfBBox,
    label_point: tuple[float, float],
    page_height: float,
    padding: float = 3.0,
) -> tuple[float, float]:
    x0, top, x1, bottom = source_box
    center_x = (x0 + x1) / 2
    center_top_y = (top + bottom) / 2
    label_x, label_pdf_y = label_point
    label_top_y = page_height - label_pdf_y

    dx = label_x - center_x
    dy = label_top_y - center_top_y
    if abs(dx) >= abs(dy):
        start_x = x1 + padding if dx >= 0 else x0 - padding
        scale = (start_x - center_x) / dx if dx else 0
        start_top_y = center_top_y + dy * scale
        start_top_y = clamp(start_top_y, top - padding, bottom + padding)
    else:
        start_top_y = bottom + padding if dy >= 0 else top - padding
        scale = (start_top_y - center_top_y) / dy if dy else 0
        start_x = center_x + dx * scale
        start_x = clamp(start_x, x0 - padding, x1 + padding)

    return start_x, page_height - start_top_y


def page_text_bboxes(pdf_path: Path) -> dict[int, list[PdfBBox]]:
    occupied: dict[int, list[PdfBBox]] = {}
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                boxes = []
                for word in page.extract_words(keep_blank_chars=False, use_text_flow=True, extra_attrs=[]):
                    boxes.append(
                        (
                            float(word["x0"]),
                            float(word["top"]),
                            float(word["x1"]),
                            float(word["bottom"]),
                        )
                    )
                occupied[page_index] = boxes
    except Exception:
        return {}
    return occupied


def page_graphic_bboxes(pdf_path: Path) -> dict[int, list[PdfBBox]]:
    occupied: dict[int, list[PdfBBox]] = {}
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                boxes: list[PdfBBox] = []
                for item in [*page.lines, *page.curves, *page.rects]:
                    x0 = float(item.get("x0", 0))
                    x1 = float(item.get("x1", x0))
                    top = float(item.get("top", 0))
                    bottom = float(item.get("bottom", top))
                    if x1 < x0:
                        x0, x1 = x1, x0
                    if bottom < top:
                        top, bottom = bottom, top
                    width = x1 - x0
                    height = bottom - top
                    if width <= 0.1 and height <= 0.1:
                        continue

                    boxes.append((x0, top, x1, bottom))
                occupied[page_index] = boxes
    except Exception:
        return {}
    return occupied


def choose_label_position(
    candidate: DimensionCandidate,
    page_width: float,
    page_height: float,
    label_half_width: float,
    label_half_height: float,
    text_boxes: list[PdfBBox],
    graphic_boxes: list[PdfBBox],
    placed_label_boxes: list[PdfBBox],
) -> tuple[float, float]:
    center_x = candidate.x + candidate.width / 2
    center_top_y = candidate.y + candidate.height / 2
    source_box = (
        candidate.x - 3,
        candidate.y - 3,
        candidate.x + candidate.width + 3,
        candidate.y + candidate.height + 3,
    )
    rings = [24, 36, 52, 72, 96, 125, 165]
    directions = [
        (1.0, -0.8),
        (-1.0, -0.8),
        (1.0, 0.8),
        (-1.0, 0.8),
        (1.0, 0.0),
        (-1.0, 0.0),
        (0.0, -1.0),
        (0.0, 1.0),
        (1.5, -0.5),
        (-1.5, -0.5),
        (1.5, 0.5),
        (-1.5, 0.5),
        (0.5, -1.5),
        (-0.5, -1.5),
        (0.5, 1.5),
        (-0.5, 1.5),
    ]

    best_x = clamp(center_x + rings[0], label_half_width + 4, page_width - label_half_width - 4)
    best_top_y = clamp(center_top_y - rings[0], label_half_height + 4, page_height - label_half_height - 4)
    best_score = float("inf")
    candidate_points: list[tuple[float, float, float, str]] = []

    # Shop-friendly default: place numbers on the same horizontal line as the
    # dimension value, either to its left or right.
    is_rotated_dimension = "rotated" in candidate.reason
    min_horizontal_gap = 42 if is_rotated_dimension else 24
    for gap in [24, 38, 56, 78, 108, 145]:
        if gap < min_horizontal_gap:
            continue
        candidate_points.append((candidate.x - gap, center_top_y, gap, "horizontal"))
        candidate_points.append((candidate.x + candidate.width + gap, center_top_y, gap, "horizontal"))

    label_text_boxes = text_boxes
    leader_text_boxes = [
        box
        for box in text_boxes
        if not boxes_intersect(box, source_box, padding=2.0)
    ]

    for ring in rings:
        for dir_x, dir_y in directions:
            length = (dir_x * dir_x + dir_y * dir_y) ** 0.5 or 1.0
            dx = ring * dir_x / length
            dy = ring * dir_y / length
            candidate_points.append((center_x + dx, center_top_y + dy, abs(dx) + abs(dy), "local"))

    rails = [
        (center_x, page_height * 0.08),
        (center_x, page_height * 0.18),
        (center_x, page_height * 0.82),
        (center_x, page_height * 0.92),
        (center_x, page_height * 0.28),
        (center_x, page_height * 0.72),
        (page_width * 0.08, center_top_y),
        (page_width * 0.18, center_top_y),
        (page_width * 0.82, center_top_y),
        (page_width * 0.92, center_top_y),
        (page_width * 0.28, center_top_y),
        (page_width * 0.72, center_top_y),
    ]
    for x, top_y in rails:
        distance = abs(x - center_x) + abs(top_y - center_top_y)
        candidate_points.append((x, top_y, distance + 35, "rail"))

    for raw_x, raw_top_y, distance, placement_mode in candidate_points:
            x = clamp(raw_x, label_half_width + 4, page_width - label_half_width - 4)
            top_y = clamp(raw_top_y, label_half_height + 4, page_height - label_half_height - 4)
            label_box = (
                x - label_half_width - 2,
                top_y - label_half_height - 2,
                x + label_half_width + 2,
                top_y + label_half_height + 2,
            )

            text_overlap = sum(intersection_area(label_box, box, padding=3.0) for box in label_text_boxes)
            label_overlap = sum(intersection_area(label_box, box, padding=4.0) for box in placed_label_boxes)
            text_hits = sum(1 for box in label_text_boxes if boxes_intersect(label_box, box, padding=3.0))
            label_hits = sum(1 for box in placed_label_boxes if boxes_intersect(label_box, box, padding=4.0))
            graphic_hits = sum(1 for box in graphic_boxes if boxes_intersect(label_box, box, padding=2.0))

            source_pdf_box = (
                candidate.x,
                candidate.y,
                candidate.x + candidate.width,
                candidate.y + candidate.height,
            )
            line_start_x, line_start_pdf_y = line_start_outside_source_box(
                source_pdf_box,
                (x, page_height - top_y),
                page_height,
            )
            leader_start = (line_start_x, page_height - line_start_pdf_y)
            leader_end = (x, top_y)
            leader_length = ((leader_end[0] - leader_start[0]) ** 2 + (leader_end[1] - leader_start[1]) ** 2) ** 0.5
            leader_text_hits = sum(
                1
                for box in leader_text_boxes
                if segment_intersects_box(leader_start, leader_end, box, padding=1.2)
            )
            leader_label_hits = sum(
                1
                for box in placed_label_boxes
                if segment_intersects_box(leader_start, leader_end, box, padding=2.0)
            )
            leader_graphic_hits = sum(
                1
                for box in graphic_boxes
                if segment_intersects_box(leader_start, leader_end, box, padding=1.2)
            )

            edge_bonus = 0
            if x < page_width * 0.12 or x > page_width * 0.88 or top_y < page_height * 0.12 or top_y > page_height * 0.88:
                edge_bonus = -120
            min_leader = 30 if is_rotated_dimension else 18
            short_leader_penalty = (min_leader - leader_length) * 900 if leader_length < min_leader else 0
            horizontal = placement_mode == "horizontal" and abs(top_y - center_top_y) <= 2.5
            clean_horizontal = (
                horizontal
                and text_hits == 0
                and label_hits == 0
                and leader_text_hits == 0
                and leader_label_hits == 0
                and graphic_hits <= 1
                and leader_length >= min_leader
            )
            horizontal_bonus = 0
            if clean_horizontal:
                horizontal_bonus = -26000
                if is_rotated_dimension:
                    horizontal_bonus -= 3500
            elif horizontal and text_hits == 0 and label_hits == 0 and leader_text_hits == 0:
                horizontal_bonus = -2500

            distance_weight = 2 if placement_mode == "horizontal" else 4

            score = (
                text_hits * 25000
                + label_hits * 40000
                + graphic_hits * 6500
                + text_overlap * 200
                + label_overlap * 300
                + leader_text_hits * 4500
                + leader_label_hits * 4500
                + leader_graphic_hits * 1200
                + distance * distance_weight
                + edge_bonus
                + horizontal_bonus
                + short_leader_penalty
            )
            if score < best_score:
                best_score = score
                best_x = x
                best_top_y = top_y

    return best_x, page_height - best_top_y


def draw_numbered_overlay(
    original_pdf: Path,
    candidates: list[DimensionCandidate],
    output_pdf: Path,
) -> None:
    reader = PdfReader(str(original_pdf))
    writer = PdfWriter()
    occupied_by_page = page_text_bboxes(original_pdf)
    graphics_by_page = page_graphic_bboxes(original_pdf)
    by_page: dict[int, list[DimensionCandidate]] = {}
    for candidate in candidates:
        by_page.setdefault(candidate.page, []).append(candidate)

    tmp_dir = output_pdf.parent / "_tmp_overlay"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for page_index, page in enumerate(reader.pages, start=1):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        overlay_path = tmp_dir / f"overlay_{page_index}.pdf"

        c = canvas.Canvas(str(overlay_path), pagesize=(width, height))
        c.setLineWidth(0.55)
        placed_label_boxes: list[PdfBBox] = []

        for candidate in by_page.get(page_index, []):
            font_size = 7.0 if candidate.number < 100 else 6.4
            label = str(candidate.number)
            c.setFont("Helvetica-Bold", font_size)
            text_width = c.stringWidth(label, "Helvetica-Bold", font_size)
            label_half_width = text_width / 2 + 1.5
            label_half_height = font_size / 2 + 1.5
            text_x = candidate.x + candidate.width / 2
            text_y = height - candidate.y - (candidate.height / 2)
            pdf_x, pdf_y = choose_label_position(
                candidate,
                width,
                height,
                label_half_width,
                label_half_height,
                occupied_by_page.get(page_index, []),
                graphics_by_page.get(page_index, []),
                placed_label_boxes,
            )
            label_top_y = height - pdf_y
            placed_label_boxes.append(
                (
                    pdf_x - label_half_width - 2,
                    label_top_y - label_half_height - 2,
                    pdf_x + label_half_width + 2,
                    label_top_y + label_half_height + 2,
                )
            )

            c.setStrokeColor(HexColor("#dc2626"))
            c.setFillColor(HexColor("#dc2626"))
            c.setFont("Helvetica-Bold", font_size)
            c.drawString(pdf_x - text_width / 2, pdf_y - 2.2, label)

            c.setStrokeColor(HexColor("#dc2626"))
            source_box = (
                candidate.x,
                candidate.y,
                candidate.x + candidate.width,
                candidate.y + candidate.height,
            )
            line_start_x, line_start_y = line_start_outside_source_box(source_box, (pdf_x, pdf_y), height)
            vec_x = line_start_x - pdf_x
            vec_y = line_start_y - pdf_y
            vec_len = (vec_x * vec_x + vec_y * vec_y) ** 0.5 or 1.0
            label_edge_x = pdf_x + (vec_x / vec_len) * label_half_width
            label_edge_y = pdf_y + (vec_y / vec_len) * label_half_height
            c.line(line_start_x, line_start_y, label_edge_x, label_edge_y)

        c.save()

        overlay_reader = PdfReader(str(overlay_path))
        page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    with output_pdf.open("wb") as handle:
        writer.write(handle)

    shutil.rmtree(tmp_dir, ignore_errors=True)


def load_candidates(path: Path) -> list[DimensionCandidate]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[DimensionCandidate] = []
    for item in payload:
        candidates.append(
            DimensionCandidate(
                number=int(item["number"]),
                page=int(item["page"]),
                text=str(item.get("text", "")),
                x=float(item["x"]),
                y=float(item["y"]),
                width=float(item.get("width", 0)),
                height=float(item.get("height", 0)),
                confidence=float(item.get("confidence", 0)),
                reason=str(item.get("reason", "manual")),
            )
        )
    candidates.sort(key=lambda item: (item.page, item.number))
    return candidates


def init_history(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table if not exists jobs (
                id text primary key,
                client text,
                part_number text,
                drawing_number text,
                revision text,
                source_hash text not null,
                original_pdf text not null,
                numbered_pdf text not null,
                candidates_json text not null,
                created_at text not null
            )
            """
        )
        conn.execute(
            "create index if not exists idx_jobs_lookup on jobs(client, part_number, drawing_number, revision)"
        )


def save_history(db_path: Path, job: dict[str, Any]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert or replace into jobs (
                id, client, part_number, drawing_number, revision, source_hash,
                original_pdf, numbered_pdf, candidates_json, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job.get("client"),
                job.get("part_number"),
                job.get("drawing_number"),
                job.get("revision"),
                job["source_hash"],
                job["original_pdf"],
                job["numbered_pdf"],
                job["candidates_json"],
                job["created_at"],
            ),
        )


def analyze_command(args: argparse.Namespace) -> None:
    input_pdf = Path(args.input_pdf).resolve()
    if not input_pdf.exists():
        raise SystemExit(f"PDF not found: {input_pdf}")

    storage = Path(args.storage).resolve()
    source_hash = sha256_file(input_pdf)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    identity = "-".join(
        part for part in [args.client, args.part_number, args.drawing_number, args.revision] if part
    )
    safe_identity = re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-") or "plano"
    job_id = f"{safe_identity}-{stamp}-{source_hash[:8]}"
    job_dir = storage / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    original_pdf = job_dir / "original.pdf"
    candidates_json = job_dir / "candidates.json"
    numbered_pdf = job_dir / "numbered.pdf"
    job_json = job_dir / "job.json"

    shutil.copy2(input_pdf, original_pdf)
    candidates = extract_candidates(original_pdf, include_tables=args.include_tables)
    used_ocr = False
    if not candidates:
        ocr_candidates = extract_ocr_candidates(original_pdf)
        if ocr_candidates:
            candidates = ocr_candidates
            used_ocr = True
    draw_numbered_overlay(original_pdf, candidates, numbered_pdf)

    candidates_payload = [asdict(candidate) for candidate in candidates]
    candidates_json.write_text(json.dumps(candidates_payload, indent=2), encoding="utf-8")

    job = {
        "id": job_id,
        "used_ocr": used_ocr,
        "client": args.client,
        "part_number": args.part_number,
        "drawing_number": args.drawing_number,
        "revision": args.revision,
        "source_hash": source_hash,
        "original_pdf": str(original_pdf),
        "numbered_pdf": str(numbered_pdf),
        "candidates_json": str(candidates_json),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
    }
    job_json.write_text(json.dumps(job, indent=2), encoding="utf-8")

    db_path = storage / "history.sqlite"
    init_history(db_path)
    save_history(db_path, job)

    print(json.dumps(job, indent=2))


def search_command(args: argparse.Namespace) -> None:
    db_path = Path(args.storage).resolve() / "history.sqlite"
    init_history(db_path)
    params = {
        "client": args.client,
        "part_number": args.part_number,
        "drawing_number": args.drawing_number,
        "revision": args.revision,
    }
    clauses = []
    values = []
    for key, value in params.items():
        if value:
            clauses.append(f"{key} like ?")
            values.append(f"%{value}%")

    sql = "select * from jobs"
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by created_at desc limit ?"
    values.append(args.limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(sql, values)]

    print(json.dumps(rows, indent=2))


def render_command(args: argparse.Namespace) -> None:
    original_pdf = Path(args.original_pdf).resolve()
    candidates_json = Path(args.candidates_json).resolve()
    output_pdf = Path(args.output_pdf).resolve()
    if not original_pdf.exists():
        raise SystemExit(f"PDF not found: {original_pdf}")
    if not candidates_json.exists():
        raise SystemExit(f"Candidates JSON not found: {candidates_json}")

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    draw_numbered_overlay(original_pdf, load_candidates(candidates_json), output_pdf)
    print(json.dumps({"output_pdf": str(output_pdf)}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detecta y numera cotas en planos PDF.")
    parser.add_argument("--storage", default="storage", help="Carpeta de historial.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analiza y numera un PDF.")
    analyze.add_argument("input_pdf")
    analyze.add_argument("--client", default="")
    analyze.add_argument("--part-number", default="")
    analyze.add_argument("--drawing-number", default="")
    analyze.add_argument("--revision", default="")
    analyze.add_argument(
        "--include-tables",
        action="store_true",
        help="Incluye numeros dentro de tablas. Por defecto se excluyen.",
    )
    analyze.set_defaults(func=analyze_command)

    search = subparsers.add_parser("search", help="Busca trabajos guardados.")
    search.add_argument("--client", default="")
    search.add_argument("--part-number", default="")
    search.add_argument("--drawing-number", default="")
    search.add_argument("--revision", default="")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=search_command)

    render = subparsers.add_parser("render", help="Regenera un PDF numerado desde candidatos editados.")
    render.add_argument("original_pdf")
    render.add_argument("candidates_json")
    render.add_argument("output_pdf")
    render.set_defaults(func=render_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
