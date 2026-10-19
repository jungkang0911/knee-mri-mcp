"""PII masking for burned-in overlay text.

Policy (allowlist, fail closed):
  * Every OCR'd word is masked unless it is an orientation letter (R/L/A/P/S/I), a sequence/plane word
    (COR, SAG, AX, PD, T2, FS ...), a technical key (SL, ST, TR, TE ...) or a short number right after a
    technical key. Names, chart numbers, birth dates and doctor names therefore never need to be recognised
    as such: anything not on the list is blacked out.
  * After masking, the image is OCR'd again. If any word that could carry identity survives (3+ letters or
    digits not on the allowlist, a 4+ digit run, CJK text, or one of KNEE_PII_TERMS), the image is withheld.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps

from .errors import RedactionFailed
from .templates import Rect, apply_template

REDACTION_VERSION = "9"
TEXT_ZONE_MARGIN = 0.03  # how far around a template rectangle stray overlay text may still sit
MAX_MASK_PASSES = 6

ORIENTATION = {"R", "L", "A", "P", "S", "I"}
TECH_KEYS = {
    # Deliberately no 1-2 letter keys that are also common surnames (MA, HO, LI ...).
    "SL", "ST", "TR", "TE", "TI", "ET", "FA", "FOV", "NEX", "THK", "SP", "MM", "MS", "WW", "WL",
    "ZOOM", "MAG", "SE", "SER", "IM", "IMA", "IMG", "KVP",
}
SEQ_WORDS = {
    "COR", "CORONAL", "SAG", "SAGITTAL", "AX", "AXI", "AXIAL", "TRA", "TRANS", "TRANSVERSE",
    "PD", "PDW", "PDFS", "T1", "T2", "T1W", "T2W", "T1FS", "T2FS", "FS", "FAT", "SAT", "FSE", "TSE", "GRE",
    "STIR", "SPIR", "SPAIR", "DESS", "MEDIC", "FLASH", "TIRM", "FLAIR", "IR", "3D", "2D", "FIESTA",
    "MERGE", "DIXON", "WATER", "SPGR", "CUBE", "VIBE", "HR", "PROPELLER", "BLADE", "MRI", "MR", "KNEE",
}
_PLANE = {
    "COR": "COR", "CORONAL": "COR", "SAG": "SAG", "SAGITTAL": "SAG",
    "AX": "Axial", "AXI": "Axial", "AXIAL": "Axial", "TRA": "Axial", "TRANS": "Axial", "TRANSVERSE": "Axial",
}
_NUM_RE = re.compile(r"^[+-]?\d{1,3}(?:\.\d{1,2})?(?:MM|MS)?$")
_KEYNUM_RE = re.compile(r"^(SL|ST|TR|TE|TI|FA|FOV|THK|SP)[:=]?[+-]?\d{1,4}(?:\.\d{1,2})?(?:MM|MS)?$")
# Texture inside the anatomy OCRs as short low-confidence "words" (seen up to ~50); real overlay text is 80+.
CENTRAL_CONF = 60
# Verification floor inside the anatomy area: real overlay text scores 80+, texture misreads up to ~70.
CENTRAL_VERIFY_CONF = 75
_STRIP = ":;,=[](){}<>|'\"`*"
_CJK_RE = re.compile(r"[　-鿿가-힯＀-￯]")


@dataclass
class Word:
    text: str
    conf: float
    box: tuple[int, int, int, int]
    line: tuple[int, int, int, int]  # (variant, block, par, line)


def _norm(text: str) -> str:
    return text.strip().strip(_STRIP).upper()


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text)


def ocr_words(img: Image.Image, langs: str) -> list[Word]:
    import pytesseract

    gray = ImageOps.autocontrast(img.convert("L"))
    scale = max(1.0, min(3.0, 1600 / max(gray.size)))
    big = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.LANCZOS) if scale > 1 else gray
    words: list[Word] = []
    # Overlay text is usually light-on-dark; Tesseract reads dark-on-light best, so try both polarities.
    # Two page-segmentation modes: sparse text (11) finds scattered labels, uniform block (6) finds
    # tightly packed corner text that psm 11 misses (seen on real 2019 overlays: a name at the edge).
    runs = [(ImageOps.invert(big), "--psm 11"), (big, "--psm 11"), (ImageOps.invert(big), "--psm 6"), (big, "--psm 6")]
    for variant, (im, config) in enumerate(runs):
        data = pytesseract.image_to_data(im, lang=langs, config=config, output_type=pytesseract.Output.DICT)
        for i, raw in enumerate(data["text"]):
            text = (raw or "").strip()
            if not text:
                continue
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            box = (int(x / scale), int(y / scale), int((x + w) / scale) + 1, int((y + h) / scale) + 1)
            line = (variant, data["block_num"][i], data["par_num"][i], data["line_num"][i])
            words.append(Word(text, float(data["conf"][i]), box, line))
    return words


def _allowed_flags(words: list[Word]) -> list[bool]:
    """Per word: True when the word is allowed to stay visible."""
    out = [False] * len(words)
    prev_by_line: dict[tuple, str] = {}
    for i in sorted(range(len(words)), key=lambda i: (words[i].line, words[i].box[0])):
        word = words[i]
        t = _norm(word.text)
        prev = prev_by_line.get(word.line, "")
        out[i] = not _CJK_RE.search(word.text) and (
            (len(t) == 1 and t in ORIENTATION)
            or t in SEQ_WORDS
            or t in TECH_KEYS
            or bool(_KEYNUM_RE.match(t))
            or (bool(_NUM_RE.match(t)) and prev in TECH_KEYS)
        )
        prev_by_line[word.line] = t
    return out


def _is_border(box: tuple[int, int, int, int], size: tuple[int, int], zones: list[Rect] | None = None) -> bool:
    """Is this box where overlay text lives?

    Without a hospital template: the outer quarter of the image on every side. With a template: only near
    the template rectangles (expanded by TEXT_ZONE_MARGIN); everything else is anatomy, which is verified
    but never masked."""
    w, h = size
    if zones is not None:
        x0, y0, x1, y1 = box[0] / w, box[1] / h, box[2] / w, box[3] / h
        return any(x0 < z[2] and x1 > z[0] and y0 < z[3] and y1 > z[1] for z in zones)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return cx < 0.25 * w or cx > 0.75 * w or cy < 0.25 * h or cy > 0.75 * h


def text_zones(template: list[Rect] | None) -> list[Rect] | None:
    if not template:
        return None
    m = TEXT_ZONE_MARGIN
    return [(max(0.0, r[0] - m), max(0.0, r[1] - m), min(1.0, r[2] + m), min(1.0, r[3] + m)) for r in template]


def _should_mask(word: Word, allowed: bool, size: tuple[int, int], zones: list[Rect] | None = None) -> bool:
    if allowed:
        return False
    # Overlay text sits in the margins: mask anything there. In the central (anatomy) area, low-confidence
    # "words" are texture noise; masking them would black out anatomy, so only confident text is masked.
    # The verification pass uses the same confidence floor, so nothing legible can slip between the two.
    if _is_border(word.box, size, zones):
        return True
    if _CJK_RE.search(word.text):
        return word.conf >= CENTRAL_CONF
    a = _alnum(word.text)
    if len(a) < 2:  # lone symbols / single characters in the texture carry no identity
        return False
    return word.conf >= CENTRAL_CONF or bool(re.search(r"\d{4,}", a))


def _is_residual(
    word: Word, allowed: bool, size: tuple[int, int], pii_terms: list[str], zones: list[Rect] | None = None
) -> bool:
    if allowed:
        return False
    text = word.text
    if any(t and t.lower() in text.lower() for t in pii_terms):
        return True
    a = _alnum(text)
    border = _is_border(word.box, size, zones)
    if re.search(r"\d{4,}", a) and word.conf >= (30 if border else CENTRAL_CONF):
        return True
    floor = 50 if border else CENTRAL_VERIFY_CONF
    cjk = len(_CJK_RE.findall(text))
    # At the edges any CJK counts. In the anatomy area a lone "CJK character" is texture noise (names are
    # 2+ characters); single characters there are judged in pairs by _center_cjk_pairs instead.
    if cjk and word.conf >= floor and (border or cjk >= 2):
        return True
    return len(a) >= 3 and word.conf >= floor


def _center_cjk_pairs(
    words: list[Word], flags: list[bool], size: tuple[int, int], zones: list[Rect] | None = None
) -> bool:
    """Two single CJK characters side by side, both text-sized and confident, inside the anatomy area.

    OCR sometimes splits a name into one word per character; specks of texture also read as single
    characters, but they are tiny (2-11 px on a 512 px image) and rarely sit in a neat row."""
    min_h = 0.015 * size[1]
    singles = [
        w for w, ok in zip(words, flags)
        if not ok and not _is_border(w.box, size, zones) and len(_CJK_RE.findall(w.text)) == 1
        and w.conf >= CENTRAL_VERIFY_CONF and (w.box[3] - w.box[1]) >= min_h
    ]
    for i, a in enumerate(singles):
        ah = a.box[3] - a.box[1]
        for b in singles[i + 1:]:
            bh = b.box[3] - b.box[1]
            if max(ah, bh) > 1.6 * min(ah, bh):
                continue
            same_row = abs((a.box[1] + a.box[3]) - (b.box[1] + b.box[3])) / 2 < 0.5 * max(ah, bh)
            gap = max(a.box[0], b.box[0]) - min(a.box[2], b.box[2])
            narrow = min(a.box[2] - a.box[0], b.box[2] - b.box[0])
            if gap < -0.5 * narrow:
                continue  # the same glyph seen by two OCR runs, not two characters
            if same_row and gap < 1.5 * max(ah, bh):
                return True
    return False


def _mask(
    img: Image.Image, words: list[Word], flags: list[bool], skip_center: bool = False, zones: list[Rect] | None = None
) -> int:
    """Black out maskable words. With a hospital template the text bands are already black, so the
    anatomy area is only verified, never masked: masking there makes black-box edges that OCR then reads
    as new "text", which cascaded into false redaction failures on real images."""
    draw = ImageDraw.Draw(img)
    fill = 0 if img.mode in ("L", "1", "I", "F") else (0,) * len(img.getbands())
    n = 0
    for w, ok in zip(words, flags):
        if not _should_mask(w, ok, img.size, zones):
            continue
        if skip_center and not _is_border(w.box, img.size, zones):
            continue
        x0, y0, x1, y1 = w.box
        hgt = max(4, y1 - y0)
        px, py = int(0.6 * hgt) + 2, int(0.3 * hgt) + 2
        draw.rectangle((max(0, x0 - px), max(0, y0 - py), min(img.width, x1 + px), min(img.height, y1 + py)), fill=fill)
        n += 1
    return n


@dataclass
class Overlay:
    plane: str  # e.g. "COR PD", "Axial T2 FS"; "" when not readable
    sl: str  # e.g. "-12.5"; "" when not readable
    st: str


def read_overlay(words: list[Word]) -> Overlay:
    """Plane / sequence and SL / ST from the original overlay; only allowlisted tokens can come out."""
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    best: list[str] = []
    sl = st = ""
    for ws in lines.values():
        ws.sort(key=lambda w: w.box[0])
        toks = [_norm(w.text) for w in ws]
        seq = [t for t in toks if t in SEQ_WORDS and t not in {"MRI", "MR", "KNEE"}]
        if any(t in _PLANE for t in seq) and len(seq) > len(best):
            best = seq
        joined = " ".join(toks)
        for key in ("SL", "ST"):
            m = re.search(rf"(?:^| ){key}[:=]? ?([+-]?\d{{1,4}}(?:\.\d{{1,2}})?)(?:MM)?(?= |$)", joined)
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
) -> tuple[Image.Image, Overlay]:
    """Return (masked image, overlay info). Raises RedactionFailed if identifying text survives."""
    terms = pii_terms or []
    if isinstance(raw, Image.Image):
        src = raw
    else:
        src = Image.open(io.BytesIO(raw))
        src.load()
    img = src.convert("L") if src.mode not in ("L", "RGB") else src.copy()

    zones = text_zones(template)
    words = ocr_words(img, langs)
    overlay = read_overlay(words)  # plane / SL / ST are read before anything is blacked out
    if template:
        # Known overlay layout: black out the text bands first, then let OCR handle whatever is left.
        apply_template(img, template)
        words = ocr_words(img, langs)
    # Some text (e.g. a name squeezed against the image edge next to other text) only becomes legible
    # once its neighbours are masked, so keep OCR-ing and masking until a pass finds nothing new.
    final = words
    for _ in range(MAX_MASK_PASSES):
        if _mask(img, final, _allowed_flags(final), skip_center=bool(template), zones=zones) == 0:
            break  # image unchanged since `final` was read: it doubles as the verification pass
        final = ocr_words(img, langs)
    flags = _allowed_flags(final)
    if any(_is_residual(w, ok, img.size, terms, zones) for w, ok in zip(final, flags)):
        raise RedactionFailed()
    if _center_cjk_pairs(final, flags, img.size, zones):
        raise RedactionFailed()
    if terms:
        flat = re.sub(r"\s+", "", " ".join(w.text for w in final)).lower()
        if any(re.sub(r"\s+", "", t).lower() in flat for t in terms):
            raise RedactionFailed()
    return img, overlay
