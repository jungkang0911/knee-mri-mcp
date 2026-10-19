from __future__ import annotations
from pathlib import Path
import math
import re
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from .common import SafeError
from .metadata import DIRECTIONS,direction_label

POLICY = "ocr-line-boxes-v1+human-release"

def ocr_tokens(image: Image.Image) -> list[dict]:
    """One Tesseract invocation per pass; local only, no cloud OCR."""
    import pytesseract
    scale = 2 if max(image.size) <= 1200 else 1
    enlarged = image.convert("L").resize((image.width*scale, image.height*scale), Image.Resampling.NEAREST)
    try:
        data = pytesseract.image_to_data(enlarged, lang="eng", config="--psm 11", timeout=30,
                                        output_type=pytesseract.Output.DICT)
    except Exception:
        raise SafeError("OCR_ENGINE_FAILED") from None
    out = []
    for k, word in enumerate(data["text"]):
        word = word.strip()
        confidence = float(data["conf"][k])
        if not word or confidence < 0:
            continue
        x, y = data["left"][k]/scale, data["top"][k]/scale
        w, h = data["width"][k]/scale, data["height"][k]/scale
        out.append({"text":word, "confidence":confidence,
                    "box":[int(x), int(y), math.ceil(x+w), math.ceil(y+h)],
                    "line":[data["block_num"][k], data["par_num"][k], data["line_num"][k]]})
    return out

def is_direction(token: dict, size: tuple[int,int], tokens: list[dict]) -> bool:
    """Preserve an isolated, confident direction letter at an image edge, never an initial in a name."""
    word = token["text"].upper()
    if not direction_label(word) or token["confidence"] < 55:
        return False
    x0,y0,x1,y1 = token["box"]
    w,h = size
    edge = (x0 < .12*w or x1 > .88*w or y0 < .10*h or y1 > .90*h)
    neighbours = [t for t in tokens if t is not token and t["line"] == token["line"]]
    return edge and not neighbours and y1-y0 < .09*h

def tissue_region(image: Image.Image) -> np.ndarray:
    """Adaptive connected-component heuristic, for guarding against OCR hallucinated in anatomy.
    It is NOT a segmentation for diagnosis. Ambiguous boxes require review rather than erasure.
    """
    a=np.asarray(image.convert("L"),dtype=float)
    if float(a.std())<1:return np.zeros(a.shape,bool)
    binary=a>max(12,float(np.percentile(a,95))*.12)
    labels,n=ndi.label(ndi.binary_closing(binary,iterations=2))
    if not n:return np.zeros(a.shape,bool)
    sizes=np.bincount(labels.ravel());sizes[0]=0
    return ndi.binary_fill_holes(labels==sizes.argmax())


def mask_from_tokens(image: Image.Image, tokens: list[dict]) -> tuple[np.ndarray,np.ndarray,list[dict]]:
    """Detect text by OCR; don't erase ambiguous OCR boxes in anatomical tissue.

    English OCR cannot certify Chinese text removal. Full-resolution privacy AND anatomy review
    remain required. No fixed corner redaction boxes are used.
    """
    mask = Image.new("L", image.size, 0)
    compare_mask = Image.new("L", image.size, 0)
    d, c = ImageDraw.Draw(mask), ImageDraw.Draw(compare_mask)
    tissue=tissue_region(image)
    rows: dict[tuple, list[dict]] = {}
    retained = []
    for t in tokens:rows.setdefault(tuple(t["line"]),[]).append(t)
    technical=re.compile(r"\b(?:ST|SL|RT|ET|TE|TR|FS|MR|WL|WW|IMAGES?|SERIES|AXIAL|COR|SAG|PD|T1|T2)\b\s*[:=/]?|ENDIAN|CONTRAST|KNEE",re.I)
    for row in rows.values():
        row_text=" ".join(t["text"] for t in row)
        known_technical=bool(technical.search(row_text))
        for token in row:
            x0,y0,x1,y1=token["box"]
            if is_direction(token,image.size,tokens):
                retained.append({"letter":token["text"].upper(),"box":token["box"]})
                c.rectangle((max(0,x0-2),max(0,y0-2),min(image.width-1,x1+2),min(image.height-1,y1+2)),fill=255)
                continue
            overlap=tissue[max(0,y0):min(image.height,y1),max(0,x0):min(image.width,x1)]
            # Tesseract can invent letters from cartilage/trabeculae. Never blindly black these out.
            if overlap.size and float(overlap.mean())>.25 and not known_technical:
                continue
            pad=max(2,math.ceil((y1-y0)*.15))
            rect=(max(0,x0-pad),max(0,y0-pad),min(image.width-1,x1+pad),min(image.height-1,y1+pad))
            d.rectangle(rect,fill=255);c.rectangle(rect,fill=255)
    return np.array(mask,dtype=bool),np.array(compare_mask,dtype=bool),retained


def redact(image: Image.Image, tokens: list[dict], extra_boxes: list | None = None,
           verify: bool = True, verifier=None, verified_nontext_boxes: list | None=None) -> tuple[Image.Image,np.ndarray,np.ndarray,list[dict],list[dict]]:
    mask, comparison_mask, retained = mask_from_tokens(image, tokens)
    for box in extra_boxes or []:
        if not isinstance(box,list) or len(box)!=4 or any(type(n) is not int for n in box):
            raise SafeError("INVALID_REDACTION_BOX")
        x0,y0,x1,y1=box
        if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
            raise SafeError("INVALID_REDACTION_BOX")
        mask[y0:y1,x0:x1]=True
        comparison_mask[y0:y1,x0:x1]=True
    # New pixel-only image: never copy EXIF, PNG text chunks, or ICC data from the source.
    pixels = np.asarray(image.convert("L")).copy()
    pixels[mask] = 0
    clean = Image.fromarray(pixels)
    issues=[]
    if verify:
        second = (verifier or ocr_tokens)(clean)
        nontext = verified_nontext_boxes or []
        def reviewed_nontext(t):
            x0,y0,x1,y1=t["box"]
            return any(len(b)==4 and b[0]<=x0 and b[1]<=y0 and x1<=b[2] and y1<=b[3] for b in nontext)
        residual = [t for t in second if t["confidence"] >= 20 and re.search(r"[A-Za-z0-9\u3400-\u9fff]",t["text"])
                    and not is_direction(t, image.size, second) and not reviewed_nontext(t)]
        if residual:
            # Never put the detected names or IDs in issues, filenames, logs, or tool responses.
            issues.append({"code":"RESIDUAL_TEXT_DETECTED", "severity":"error", "box_count":len(residual),
                           "boxes":[t["box"] for t in residual]})
    return clean, mask, comparison_mask, retained, issues
