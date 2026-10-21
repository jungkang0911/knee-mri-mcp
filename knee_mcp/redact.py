"""PII masking for burned-in overlay text.

Policy: only two kinds of area may be blacked out, the anatomy is never touched.
  (a) the hospital's edge text bands (templates.py), applied first;
  (b) OCR text boxes in the edge area whose *content* looks like personal data: a CJK name (2+ CJK
      characters), a chart number (6+ digits), a date / birth date, a Taiwan ID number, a physician name
      (the words after "Dr"), or one of KNEE_PII_TERMS. Each box is blacked out tightly.
Orientation letters, sequence names, technical parameters and low-confidence reads are never masked.

Personal-data-like text found inside the anatomy area is not masked either: the image is marked for
manual review (redaction_status "review") with the box coordinates. If PII-like text in the edge area
is still readable after masking, the image is withheld (RedactionFailed).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageOps

from .errors import RedactionFailed
from .templates import Rect, apply_template

REDACTION_VERSION = "15"
MAX_WORD_H = 0.06  # an OCR "word" taller than this (fraction of image height) is not overlay text
MAX_WORD_W = 0.60
MAX_MASK_PASSES = 4
PII_MIN_CONF = 60  # reads below this are texture noise, never masked
REVIEW_MIN_CONF = 70  # PII-like text inside the anatomy area at or above this triggers manual review
# The edge area where overlay text lives; everything inside it is anatomy and is never masked.
EDGE_X = 0.22
EDGE_TOP = 0.20
EDGE_BOTTOM = 0.80

SEQ_WORDS = {
    "COR", "CORONAL", "SAG", "SAGITTAL", "AX", "AXI", "AXIAL", "TRA", "TRANS", "TRANSVERSE",
    "PD", "PDW", "PDFS", "T1", "T2", "T1W", "T2W", "T1FS", "T2FS", "FS", "FAT", "SAT", "FSE", "TSE", "GRE",
    "STIR", "SPIR", "SPAIR", "DESS", "MEDIC", "FLASH", "TIRM", "FLAIR", "IR", "3D", "2D", "FIESTA",
    "MERGE", "DIXON", "WATER", "SPGR", "CUBE", "VIBE", "HR", "PROPELLER", "BLADE", "MRI", "MR", "KNEE",
}
TECH_KEYS = {"SL", "ST", "TR", "TE", "TI", "ET", "FA", "FOV", "NEX", "THK", "SP", "WW", "WL", "SE", "IM", "KVP"}
# Contrast-enhanced (post-gadolinium) markers: "+C", "C+", "Gd", "post", "contrast". Kept as the token "+C".
CONTRAST_RE = re.compile(r"(?<![A-Z0-9])(\+C|C\+|GD|GAD|GADO|POST|POSTCONTRAST|CONTRAST|顯影)(?![A-Z])", re.IGNORECASE)
CONTRAST_TOKEN = "+C"
_PLANE = {
    "COR": "COR", "CORONAL": "COR", "SAG": "SAG", "SAGITTAL": "SAG",
    "AX": "Axial", "AXI": "Axial", "AXIAL": "Axial", "TRA": "Axial", "TRANS": "Axial", "TRANSVERSE": "Axial",
}
_STRIP = ":;,=[](){}<>|'\"`*"
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_MONTHS = (
    "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"
    "|JAN|FEB|MAR|APR|JUN|JUL|AUG|SEPT|SEP|OCT|NOV|DEC"
)
_PII_PATTERNS = [
    re.compile(r"[A-Z][12]\d{8}"),  # Taiwan national ID
    re.compile(r"(19|20)\d{2}\s?[-/.年]\s?\d{1,2}\s?[-/.月]\s?\d{1,2}"),  # 1975/04/12, 1975年4月12
    re.compile(r"(?<!\d)(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),  # 19750412
    re.compile(r"(?<!\d)\d{1,2}[-/.](0?[1-9]|1[0-2])[-/.](19|20)?\d{2}(?!\d)"),  # 12/04/1975
    re.compile(rf"\d{{1,2}}[- ]?({_MONTHS})[- ]?\d{{2,4}}", re.IGNORECASE),  # 12-APR-1975
    re.compile(r"\d[\d\-]{4,}\d"),  # chart / ID numbers: 6+ characters of digits and dashes
]


@dataclass
class Word:
    text: str
    conf: float
    box: tuple[int, int, int, int]
    line: tuple[int, int, int, int]  # (variant, block, par, line)


@dataclass
class Redacted:
    img: Image.Image
    overlay: "Overlay"
    mask_boxes: list[list[int]] = field(default_factory=list)  # every blacked-out rectangle, in pixels
    review_boxes: list[list[int]] = field(default_factory=list)  # PII-like text left in the anatomy area


def _norm(text: str) -> str:
    return text.strip().strip(_STRIP).upper()


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text)


def ocr_words(img: Image.Image, langs: str, fast: bool = False) -> list[Word]:
    """OCR words with pixel boxes. fast=True runs one pass (inverted, psm 11): enough for plane / SL / ST."""
    import pytesseract

    gray = ImageOps.autocontrast(img.convert("L"))
    scale = max(1.0, min(3.0, 1600 / max(gray.size)))
    big = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.LANCZOS) if scale > 1 else gray
    words: list[Word] = []
    # Overlay text is usually light-on-dark; Tesseract reads dark-on-light best, so try both polarities.
    # Two page-segmentation modes: sparse text (11) finds scattered labels, uniform block (6) finds
    # tightly packed corner text that psm 11 misses (seen on real 2019 overlays: a name at the edge).
    runs = [(ImageOps.invert(big), "--psm 11"), (big, "--psm 11"), (ImageOps.invert(big), "--psm 6"), (big, "--psm 6")]
    if fast:
        runs = runs[:1]
    for variant, (im, config) in enumerate(runs):
        data = pytesseract.image_to_data(im, lang=langs, config=config, output_type=pytesseract.Output.DICT)
        for i, raw in enumerate(data["text"]):
            text = (raw or "").strip()
            if not text:
                continue
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            box = (int(x / scale), int(y / scale), int((x + w) / scale) + 1, int((y + h) / scale) + 1)
            line = (variant, data["block_num"][i], data["par_num"][i], data["line_num"][i])
            if (box[3] - box[1]) > MAX_WORD_H * img.height or (box[2] - box[0]) > MAX_WORD_W * img.width:
                continue  # psm 6 sometimes lumps anatomy into one giant "word"; overlay text is ~3% tall
            words.append(Word(text, float(data["conf"][i]), box, line))
    return words


def in_edge(box: tuple[int, int, int, int] | list[int], size: tuple[int, int]) -> bool:
    """Is the box (by its center) in the edge area where overlay text lives?"""
    w, h = size
    cx, cy = (box[0] + box[2]) / 2 / w, (box[1] + box[3]) / 2 / h
    return cx < EDGE_X or cx > 1 - EDGE_X or cy < EDGE_TOP or cy > EDGE_BOTTOM


def looks_like_pii(text: str, pii_terms: list[str] | None = None) -> bool:
    """Content test only: CJK name, chart number, date / birth date, Taiwan ID, configured terms."""
    t = text.strip()
    low = t.lower()
    if any(term and term.lower() in low for term in pii_terms or []):
        return True
    if len(_CJK_RE.findall(t)) >= 2:
        return True
    up = t.upper()
    return any(p.search(up) for p in _PII_PATTERNS)


def pii_words(words: list[Word], pii_terms: list[str] | None = None, min_conf: float = PII_MIN_CONF) -> list[Word]:
    """Words whose content looks like personal data, plus the 1-3 words after a "Dr" title on the same line.

    Tesseract often returns a Chinese name as one word per character, so runs of adjacent CJK words on a
    line are judged together."""
    hits = [w for w in words if w.conf >= min_conf and looks_like_pii(w.text, pii_terms)]
    by_line: dict[tuple, list[Word]] = {}
    for w in words:
        by_line.setdefault(w.line, []).append(w)
    for ws in by_line.values():
        ws.sort(key=lambda w: w.box[0])
        run: list[Word] = []
        for w in ws + [None]:  # type: ignore[list-item]
            cjk_word = w is not None and w.conf >= min_conf and _CJK_RE.fullmatch(w.text.strip()[:1] or "x") is not None
            if cjk_word and run:
                prev = run[-1]
                h = max(prev.box[3] - prev.box[1], w.box[3] - w.box[1], 1)
                if w.box[0] - prev.box[2] > 1.5 * h:  # gap too wide: close the run, start a new one
                    if len(_CJK_RE.findall("".join(x.text for x in run))) >= 2:
                        hits.extend(run)
                    run = []
            if cjk_word:
                run.append(w)
                continue
            if len(_CJK_RE.findall("".join(x.text for x in run))) >= 2:
                hits.extend(run)
            run = []
        for i, w in enumerate(ws):
            if re.fullmatch(r"DR\.?|DR\.\w+", _norm(w.text)) and w.conf >= min_conf:
                hits.append(w)
                hits.extend(x for x in ws[i + 1:i + 4] if x.conf >= min_conf)
    return hits


def _box_for(word: Word, size: tuple[int, int]) -> list[int]:
    x0, y0, x1, y1 = word.box
    pad = 2 + int(0.15 * (y1 - y0))
    return [max(0, x0 - pad), max(0, y0 - pad), min(size[0], x1 + pad), min(size[1], y1 + pad)]


def _blackout(img: Image.Image, boxes: list[list[int]]) -> None:
    draw = ImageDraw.Draw(img)
    fill = 0 if img.mode in ("L", "1", "I", "F") else (0,) * len(img.getbands())
    for x0, y0, x1, y1 in boxes:
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fill)


def template_boxes(template: list[Rect] | None, size: tuple[int, int]) -> list[list[int]]:
    w, h = size
    return [[int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h)] for r in template or []]


@dataclass
class Overlay:
    plane: str  # e.g. "COR PD", "Axial T2 FS"; "" when not readable
    sl: str  # e.g. "-12.5"; "" when not readable
    st: str


def read_overlay(words: list[Word]) -> Overlay:
    """Plane / sequence and SL / ST from the original overlay; only known tokens and numbers come out."""
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    best: list[str] = []
    sl = st = ""
    for ws in lines.values():
        ws.sort(key=lambda w: w.box[0])
        toks = [_norm(w.text) for w in ws]
        parts = [x for t in toks for x in re.split(r"[_/\\]+", t) if x]  # "COR_T2_FS" -> COR T2 FS
        seq = [t for t in parts if t in SEQ_WORDS and t not in {"MRI", "MR", "KNEE"}]
        if seq and CONTRAST_RE.search(" ".join(parts)):
            seq.append(CONTRAST_TOKEN)  # "T1 +C FS", "T1 C+", "post Gd" -> contrast-enhanced
        if any(t in _PLANE for t in seq) and len(seq) > len(best):
            best = seq
        joined = " ".join(toks)
        for key in ("SL", "ST"):
            m = re.search(rf"(?:^| ){key}[:=]? ?([+-]?\d{{1,5}}(?:\.\d{{1,2}})?)\.?(?:MM)?(?= |$)", joined)
            if m:
                if key == "SL" and not sl:
                    sl = m.group(1)
                elif key == "ST" and not st:
                    st = m.group(1)
    plane = " ".join(_PLANE.get(t, t) for t in dict.fromkeys(best))
    return Overlay(plane, sl, st)


def redact(
    raw: bytes | Image.Image,
    langs: str = "eng",
    pii_terms: list[str] | None = None,
    template: list[Rect] | None = None,
) -> Redacted:
    """Black out the template bands and PII-like text at the edges; flag PII-like text in the anatomy.

    Raises RedactionFailed if PII-like edge text is still readable after masking."""
    terms = pii_terms or []
    if isinstance(raw, Image.Image):
        src = raw
    else:
        src = Image.open(io.BytesIO(raw))
        src.load()
    img = src.convert("L") if src.mode not in ("L", "RGB") else src.copy()
    size = img.size

    words = ocr_words(img, langs)
    overlay = read_overlay(words)  # plane / SL / ST are read before anything is blacked out
    mask_boxes = template_boxes(template, size)
    if template:
        apply_template(img, template)
        words = ocr_words(img, langs)

    for _ in range(MAX_MASK_PASSES):
        targets = [w for w in pii_words(words, terms) if in_edge(w.box, size)]
        if not targets:
            break
        boxes = [_box_for(w, size) for w in targets]
        _blackout(img, boxes)
        mask_boxes.extend(boxes)
        words = ocr_words(img, langs)
    else:
        if any(in_edge(w.box, size) for w in pii_words(words, terms)):
            raise RedactionFailed()

    review = [w for w in pii_words(words, terms, REVIEW_MIN_CONF) if not in_edge(w.box, size)]
    return Redacted(img, overlay, _dedupe(mask_boxes), _dedupe([list(w.box) for w in review]))


def _dedupe(boxes: list[list[int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for b in boxes:
        if b not in out:
            out.append(b)
    return out
