from __future__ import annotations
from pathlib import Path
import numpy as np
from PIL import Image
from .common import SafeError,identifier,json_read,json_write,safe_path,sha256
from .imaging import apply_crop_zoom,window_pixels,side_by_side,png_bytes

PUBLIC_FIELDS=("study_id","date","hospital","pre_or_post_op","series_no","series_desc","plane","weighting",
               "fat_sat","index","count","slice_thickness_mm","slice_location_mm","window","laterality_note",
               "dimensions","orientation_verified","source_format","slice_id","filename")

def artifact_hashes(manifest: dict) -> dict[str,str]:
    result={}
    for s in manifest["slices"]:
        result[s["path"]]=s["png_sha256"]
        if s.get("pixel_data_path"):result[s["pixel_data_path"]]=s["pixel_data_sha256"]
    for s in manifest["series"]:result[s["contact_sheet_path"]]=s["contact_sheet_sha256"]
    return result

def release(root: Path) -> dict:
    """Local human approval gate. No 'force' flag, no bulk automatic approval command."""
    root=root.resolve();m=json_read(root/"manifest.json");review=json_read(root/"review.json")
    if not m["slices"] or any(i["severity"]=="error" for i in m["issues"]):raise SafeError("VALIDATION_ERRORS_BLOCK_RELEASE")
    stats=m["statistics"]
    if stats["source_image_frames"]!=len(m["slices"])+stats["duplicates"] or not stats["raw_unchanged"]:
        raise SafeError("INVENTORY_NOT_RECONCILED")
    for s in m["slices"]:
        r=review.get("slices",{}).get(s["source_id"],{})
        if not all(r.get(k) is True for k in ("approved","phi_free","anatomy_preserved")):
            raise SafeError("HUMAN_REVIEW_REQUIRED")
        if r.get("png_sha256")!=s["png_sha256"] or r.get("pixel_data_sha256")!=s.get("pixel_data_sha256"):
            raise SafeError("REVIEW_HASH_MISMATCH")
        if s.get("pixel_data_path") and r.get("all_dicom_windows_checked") is not True:
            raise SafeError("DICOM_MULTIWINDOW_REVIEW_REQUIRED")
    hashes=artifact_hashes(m)
    for name,digest in hashes.items():
        if sha256(safe_path(root,name))!=digest:raise SafeError("ARTIFACT_HASH_MISMATCH")
    seal={"schema_version":1,"released":True,"manifest_sha256":sha256(root/"manifest.json"),
          "review_sha256":sha256(root/"review.json"),"artifacts":hashes}
    json_write(root/"release.json",seal)
    return seal

class Dataset:
    """Serving layer has no raw path/configuration/provenance dependency."""
    def __init__(self,root:Path):
        self.root=root.resolve()
        try:
            seal=json_read(safe_path(self.root,"release.json"))
            if not seal.get("released") or sha256(safe_path(self.root,"manifest.json"))!=seal["manifest_sha256"]:
                raise SafeError("DATASET_NOT_RELEASED")
            if sha256(safe_path(self.root,"review.json"))!=seal["review_sha256"]:raise SafeError("DATASET_CHANGED")
            self.manifest=json_read(safe_path(self.root,"manifest.json"));self.seal=seal
            if artifact_hashes(self.manifest)!=seal["artifacts"]:raise SafeError("DATASET_CHANGED")
            for name,digest in seal["artifacts"].items():
                if sha256(safe_path(self.root,name))!=digest:raise SafeError("DATASET_CHANGED")
            self.slices={}
            for s in self.manifest["slices"]:
                identifier(s["study_id"]);identifier(s["series_desc"]);identifier(s["hospital"])
                if s["plane"] not in ("axial","coronal","sagittal") or s["weighting"] not in ("T1","PD","T2"):
                    raise SafeError("INVALID_MANIFEST_METADATA")
                key=(s["study_id"],s["series_no"],s["index"])
                if key in self.slices:raise SafeError("DUPLICATE_SLICE_KEY")
                self.slices[key]=s
        except SafeError:raise
        except Exception:raise SafeError("DATASET_UNAVAILABLE") from None
    def _file(self,name:str)->Path:
        p=safe_path(self.root,name)
        try:
            if self.seal["artifacts"].get(name)!=sha256(p):raise SafeError("DATASET_CHANGED")
            return p
        except SafeError:raise
        except Exception:raise SafeError("DATASET_UNAVAILABLE") from None
    def _slice(self,study_id:str,series_no:int,index:int)->dict:
        identifier(study_id)
        if type(series_no) is not int or type(index) is not int or series_no<0 or index<1:
            raise SafeError("INVALID_SLICE_SELECTOR")
        row=self.slices.get((study_id,series_no,index))
        if row is None:raise SafeError("SLICE_NOT_FOUND")
        return row
    def list_studies(self)->list[dict]:
        return [{k:s[k] for k in ("study_id","date","hospital","pre_or_post_op")} for s in self.manifest["studies"]]
    def list_series(self,study_id:str)->list[dict]:
        identifier(study_id)
        result=[{k:s[k] for k in ("series_no","series_desc","plane","weighting","fat_sat","count","slice_thickness_mm")}
                for s in self.manifest["series"] if s["study_id"]==study_id]
        if not result:raise SafeError("STUDY_NOT_FOUND")
        return sorted(result,key=lambda s:s["series_no"])
    def get_image(self,study_id:str,series_no:int,index:int,crop:list[int]|None=None,zoom:float=1,
                  window:list[float]|None=None)->tuple[Image.Image,dict]:
        s=self._slice(study_id,series_no,index)
        if window is not None:
            if not s.get("pixel_data_path"):raise SafeError("WINDOW_REQUIRES_DICOM_PIXEL_DATA")
            with np.load(self._file(s["pixel_data_path"]),allow_pickle=False) as data:
                image=window_pixels(data["pixels"],window,bool(data["invert"]),data["mask"])
        else:
            with Image.open(self._file(s["path"])) as src:src.load();image=src.copy()
        metadata={k:s.get(k) for k in PUBLIC_FIELDS}
        if not metadata.get("filename") or not metadata.get("slice_id"):
            from .naming import slice_filename
            filename=slice_filename(s["study_id"],s["series_no"],s["series_desc"],s["index"])
            metadata.update(filename=filename,slice_id=Path(filename).stem)
        metadata.update(crop=crop,zoom=zoom,display_window=window or s.get("window"),
                        source_resolution=s["dimensions"],display_transform="nearest-neighbour zoom; no AI enhancement")
        return apply_crop_zoom(image,crop,zoom),metadata
    def get_contact_sheet(self,study_id:str,series_no:int)->Image.Image:
        identifier(study_id)
        if type(series_no) is not int:raise SafeError("INVALID_SERIES")
        s=next((s for s in self.manifest["series"] if s["study_id"]==study_id and s["series_no"]==series_no),None)
        if s is None:raise SafeError("SERIES_NOT_FOUND")
        with Image.open(self._file(s["contact_sheet_path"])) as src:src.load();return src.copy()
    def get_slices(self,study_id:str,series_no:int,start:int,end:int)->list[tuple[Image.Image,dict]]:
        if any(type(n) is not int for n in (start,end)) or not (1<=start<=end) or end-start+1>8:
            raise SafeError("BATCH_MUST_CONTAIN_1_TO_8_SLICES")
        # Fail the entire batch if any requested image is missing; never silently return fewer.
        return [self.get_image(study_id,series_no,i) for i in range(start,end+1)]
    def compare(self,a:dict,b:dict)->tuple[Image.Image,dict]:
        def parse(v):
            if not isinstance(v,dict) or set(v)-{"study","series","index","crop"}:raise SafeError("INVALID_COMPARE_SELECTOR")
            try:return self.get_image(v["study"],v["series"],v["index"],v.get("crop"))
            except KeyError:raise SafeError("INVALID_COMPARE_SELECTOR") from None
        ai,am=parse(a);bi,bm=parse(b)
        image=side_by_side(ai,bi,f"{am['date']} S{am['series_no']} I{am['index']}",f"{bm['date']} S{bm['series_no']} I{bm['index']}")
        return image,{"a":am,"b":bm,"warning":"未配準、未正規化；相同編號不代表同一解剖位置。不可僅憑亮度差判定惡化。"}
