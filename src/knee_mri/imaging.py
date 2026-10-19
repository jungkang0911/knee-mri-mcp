from __future__ import annotations
from io import BytesIO
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.fft import dctn
from .common import SafeError

MAX_IMAGE_BYTES = 1_000_000
MAX_PIXELS = 16_000_000

def window_pixels(pixels: np.ndarray, window: list[float], invert: bool=False, mask: np.ndarray | None=None) -> Image.Image:
    if len(window) != 2 or not all(math.isfinite(float(n)) for n in window) or window[1]<1:
        raise SafeError("INVALID_WINDOW")
    level,width = map(float,window)
    if width == 1:
        out = np.where(pixels > level-.5,255.,0.)
    else:
        out = np.clip((pixels-(level-.5))/(width-1)+.5,0,1)*255
    if invert:
        out=255-out
    out=np.nan_to_num(out,nan=0,posinf=255,neginf=0).astype(np.uint8)
    if mask is not None:
        out[mask.astype(bool)]=0  # Apply AFTER window and MONOCHROME1 inversion, every time.
    return Image.fromarray(out)

def apply_crop_zoom(image: Image.Image, crop: list[int] | None, zoom: float) -> Image.Image:
    if isinstance(zoom,bool) or not isinstance(zoom,(int,float)) or not math.isfinite(zoom) or not 1<=zoom<=4:
        raise SafeError("ZOOM_MUST_BE_BETWEEN_1_AND_4")
    if crop is not None:
        if len(crop)!=4 or any(type(v) is not int for v in crop):
            raise SafeError("INVALID_CROP")
        x0,y0,x1,y1=crop
        if not (0<=x0<x1<=image.width and 0<=y0<y1<=image.height):
            raise SafeError("CROP_OUT_OF_BOUNDS")
        image=image.crop((x0,y0,x1,y1))
    size=(round(image.width*zoom),round(image.height*zoom))
    if size[0]*size[1]>MAX_PIXELS:
        raise SafeError("IMAGE_TOO_LARGE_USE_A_SMALLER_CROP")
    return image.resize(size,Image.Resampling.NEAREST) if size!=image.size else image.copy()

def png_bytes(image: Image.Image, limit: int=MAX_IMAGE_BYTES) -> bytes:
    stream=BytesIO()
    image.save(stream,format="PNG",optimize=True)
    if stream.tell()>limit:
        raise SafeError("PNG_EXCEEDS_LIMIT_USE_A_SMALLER_CROP_OR_ZOOM")
    return stream.getvalue()

def phash(image: Image.Image, mask: np.ndarray | None=None) -> np.ndarray:
    a=np.asarray(image.convert("L")).copy()
    if mask is not None: a[mask]=0
    a=np.asarray(Image.fromarray(a).resize((32,32),Image.Resampling.LANCZOS),dtype=float)
    low=dctn(a,type=2,norm="ortho")[:8,:8]
    return (low > np.median(low.flatten()[1:])).flatten()

def similarity(a: Image.Image,b: Image.Image,ma: np.ndarray,mb: np.ndarray) -> dict:
    """No mirroring/rotation/registration. pHash alone never deletes a near-adjacent slice."""
    if a.size!=b.size:
        return {"phash_distance":64,"ncc":0.,"rmse":1.,"match":False}
    from .deid import tissue_region
    from scipy import ndimage as ndi
    roi=ndi.binary_erosion(tissue_region(a)&tissue_region(b),iterations=max(1,round(min(a.size)*.015)))
    mask=ma|mb
    if roi.mean()>=.10:mask=mask|~roi
    pa=phash(a,mask);pb=phash(b,mask)
    distance=int(np.count_nonzero(pa!=pb))
    av=np.asarray(a.convert("L"),dtype=float)[~mask]
    bv=np.asarray(b.convert("L"),dtype=float)[~mask]
    if len(av)<.08*a.width*a.height or len(av)<1000 or np.std(av)<1 or np.std(bv)<1:
        return {"phash_distance":distance,"ncc":0.,"rmse":1.,"match":False}
    # At most 2% high-difference outliers (e.g. residual burned-in text) are excluded for matching only.
    # pHash is still computed on the entire masked ROI. Multiple acceptable matches remain ambiguous.
    delta=np.abs(av-bv)
    keep=delta<=np.percentile(delta,98)
    av,bv=av[keep],bv[keep]
    ncc=float(np.corrcoef(av,bv)[0,1])
    rmse=float(np.sqrt(np.mean((av-bv)**2))/255)
    # Conservative enough to avoid treating neighbouring anatomical levels as duplicates.
    match=distance<=4 and ncc>=.995 and rmse<=.015
    return {"phash_distance":distance,"ncc":round(ncc,8),"rmse":round(rmse,8),"match":match}

def contact_sheet(images: list[tuple[int,Image.Image]], cell: int=144) -> Image.Image:
    if not images or len(images)>2000: raise SafeError("INVALID_SERIES_SIZE")
    cols=min(8,math.ceil(math.sqrt(len(images))))
    rows=math.ceil(len(images)/cols)
    canvas=Image.new("L",(cols*cell,rows*(cell+24)),0)
    draw=ImageDraw.Draw(canvas)
    for k,(idx,image) in enumerate(images):
        image=image.copy(); image.thumbnail((cell-8,cell-8),Image.Resampling.LANCZOS)
        x=(k%cols)*cell;y=(k//cols)*(cell+24)
        canvas.paste(image,(x+(cell-image.width)//2,y+24+(cell-image.height)//2))
        draw.text((x+6,y+4),str(idx),fill=255)
    return canvas

def side_by_side(a: Image.Image,b: Image.Image,label_a:str,label_b:str) -> Image.Image:
    width=a.width+b.width+12;height=max(a.height,b.height)+36
    if width*height>MAX_PIXELS: raise SafeError("IMAGE_TOO_LARGE_USE_A_SMALLER_CROP")
    out=Image.new("L",(width,height),0); d=ImageDraw.Draw(out)
    out.paste(a,(0,36));out.paste(b,(a.width+12,36))
    d.text((6,8),label_a,fill=255);d.text((a.width+18,8),label_b,fill=255)
    return out
