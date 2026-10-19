from __future__ import annotations
from collections import defaultdict,Counter
from datetime import datetime,timezone
import hashlib
import csv
import html
import re
from pathlib import Path
import numpy as np
from PIL import Image
import yaml
from .common import SafeError,sha256,json_write,json_read,identifier
from .metadata import parse_jpeg_metadata,laterality_note,technical_description
from .deid import ocr_tokens,redact,POLICY
from .dicom_io import looks_like_dicom,read_dicom
from .imaging import similarity,phash,contact_sheet
from .naming import NAMING_VERSION,slice_filename,contact_sheet_filename

META_KEYS={"series_no","series_desc","plane","weighting","fat_sat","index","count",
           "slice_thickness_mm","slice_location_mm","window","orientation_verified"}
REQUIRED=("series_no","series_desc","plane","weighting","fat_sat","index","count","slice_thickness_mm")

def build(raw:Path,out:Path,config_path:Path,private:Path) -> dict:
    raw=raw.resolve();out=out.resolve();private=private.resolve()
    if not raw.is_dir(): raise SafeError("RAW_DIRECTORY_NOT_FOUND")
    if out==raw or out.is_relative_to(raw) or raw.is_relative_to(out): raise SafeError("RAW_OUTPUT_OVERLAP")
    if (private==out or private.is_relative_to(out) or private.is_relative_to(raw)
        or raw.is_relative_to(private) or out.is_relative_to(private)): raise SafeError("PRIVATE_DIRECTORY_UNSAFE")
    if out.exists(): raise SafeError("OUTPUT_EXISTS_USE_NEW_DIRECTORY")
    cfg=yaml.safe_load(config_path.read_text(encoding="utf-8"))
    studies={s["study_id"]:s for s in cfg["studies"]}
    for s in studies.values():
        identifier(s["study_id"]);identifier(s["hospital"])
        if s["pre_or_post_op"] not in ("pre_op","post_op","unknown"): raise SafeError("INVALID_STUDY_CONFIG")
    out.mkdir(parents=True);private.mkdir(parents=True,exist_ok=True);private.chmod(0o700)
    def cached_ocr(image):
        key=hashlib.sha256(b"eng-psm11-scale2-v1"+str(image.size).encode()+image.tobytes()).hexdigest()
        cache=private/"ocr_cache"/(key+".json")
        if cache.exists():return json_read(cache)
        tokens=ocr_tokens(image)
        json_write(cache,tokens);cache.chmod(0o600)
        return tokens
    sources=[];candidates=[];issues=[];provenance={};raw_before={};inventory=[]
    paths=sorted(raw.rglob("*"))
    for p in paths:
        if p.is_symlink():
            issues.append({"code":"RAW_SYMLINK_SKIPPED","severity":"error"});continue
        if not p.is_file(): continue
        rel=p.relative_to(raw).as_posix()
        if p.name.upper()=="DICOMDIR":
            inventory.append({"kind":"dicom_directory_index","source_id":hashlib.sha256(rel.encode()).hexdigest()[:20]})
            # All referenced files inside raw are already discovered recursively, including extensionless DICOM.
            continue
        dicom=looks_like_dicom(p)
        if not dicom and p.suffix.lower() not in (".jpg",".jpeg",".png"):
            continue
        digest=sha256(p);raw_before[p]=digest
        source_id=hashlib.sha256((rel+"\0"+digest).encode()).hexdigest()[:24]
        alias=next((a for a in cfg.get("aliases",[]) if re.search(a["path_regex"],rel)),None)
        matches=[s for s in studies.values() if re.search(s["path_regex"],rel)]
        study=studies.get(alias["canonical_study_id"]) if alias else matches[0] if len(matches)==1 else None
        try:
            if dicom:
                frames=read_dicom(p)
                if study is None:
                    matches=[s for s in studies.values() if s["date"]==frames[0]["metadata"]["date"]]
                    study=matches[0] if len(matches)==1 else None
            else:
                with Image.open(p) as im:
                    im.load()
                    if im.width*im.height>16_000_000: raise SafeError("SOURCE_IMAGE_TOO_LARGE")
                    if im.mode in ("RGB","RGBA"):
                        a=np.asarray(im)
                        if not (np.array_equal(a[...,0],a[...,1]) and np.array_equal(a[...,1],a[...,2])):
                            raise SafeError("COLOR_RASTER_NOT_SUPPORTED")
                        if im.mode=="RGBA" and not np.all(a[...,3]==255):raise SafeError("ALPHA_RASTER_NOT_SUPPORTED")
                    elif im.mode not in ("L","1"):
                        raise SafeError("RASTER_BIT_DEPTH_NOT_SUPPORTED_USE_DICOM")
                    image=im.convert("L")
                frames=[{"image":image,"pixels":None,"invert":False,"metadata":None,"issues":[]}]
            if study is None: raise SafeError("STUDY_UNRESOLVED")
            for f,frame in enumerate(frames):
                sid=source_id if len(frames)==1 else source_id+f"_f{f:05}"
                provenance[sid]={"source_path":rel,"sha256":digest,"frame":f if dicom else None}
                tokens=cached_ocr(frame["image"])
                m,local_issues=(parse_jpeg_metadata(tokens,p.name,study["date"]) if not dicom else (frame["metadata"],frame["issues"]))
                if dicom and m["date"]!=study["date"]:local_issues.append({"code":"STUDY_DATE_CONFLICT","severity":"error"})
                if dicom and m.get("_laterality") not in ("R",""):local_issues.append({"code":"NOT_RIGHT_KNEE","severity":"error"})
                override=cfg.get("overrides",{}).get(sid,{})
                for key in META_KEYS:
                    if key in override:m[key]=override[key]
                if override.get("metadata_reviewed"):
                    # Only explicitly supplied field overrides resolve corresponding OCR ambiguity.
                    local_issues=[i for i in local_issues if not (
                        i["code"]=="AMBIGUOUS_WEIGHTING" and "weighting" in override or
                        i["code"]=="OCR_FILENAME_CONFLICT" and i.get("field") in override)]
                clean,mask,match_mask,directions,deid_issues=redact(frame["image"],tokens,cfg.get("extra_redaction_boxes",{}).get(sid,[]),
                                                                       verifier=cached_ocr,verified_nontext_boxes=cfg.get("verified_nontext_boxes",{}).get(sid,[]))
                local_issues+=deid_issues
                m={k:v for k,v in m.items() if not k.startswith("_")}
                # Series labels never contain freeform source text, even after manual overrides.
                if m.get("series_desc"):
                    m["series_desc"]=technical_description(m["series_desc"])["series_desc"]
                m.update(study_id=study["study_id"],date=study["date"],hospital=study["hospital"],
                         pre_or_post_op=study["pre_or_post_op"],source_id=sid,
                         source_path="source://"+sid,sha256=digest,duplicate_of=None,
                         source_format="DICOM" if dicom else "JPEG" if p.suffix.lower()!=".png" else "PNG",
                         frame_index=f if dicom else None,dimensions=[clean.width,clean.height],
                         deid_policy=POLICY,retained_directions=directions,
                         orientation_verified=bool(m.get("orientation_verified",study.get("radiological_orientation_confirmed",False))))
                m["laterality_note"]=laterality_note(m.get("plane"),m["orientation_verified"])
                source={"source_id":sid,"source_path":"source://"+sid,"sha256":digest,
                        "source_format":m["source_format"],"frame_index":m["frame_index"],
                        "duplicate_of":None,"status":"pending"}
                sources.append(source)
                candidates.append({"m":m,"clean":clean,"mask":mask,"match_mask":match_mask,
                                   "original":frame["image"],"pixels":frame["pixels"],"invert":frame["invert"],
                                   "alias":bool(alias),"issues":local_issues,"source":source,
                                   "uid":frame["metadata"].get("_series_uid") if dicom else None})
        except SafeError as e:
            issues.append({"code":str(e),"source_id":source_id,"severity":"error"})
            sources.append({"source_id":source_id,"source_path":"source://"+source_id,"sha256":digest,
                            "duplicate_of":None,"status":"failed"})
            provenance[source_id]={"source_path":rel,"sha256":digest}
        except Exception:
            issues.append({"code":"SOURCE_PROCESSING_FAILED","source_id":source_id,"severity":"error"})
            sources.append({"source_id":source_id,"source_path":"source://"+source_id,"sha256":digest,
                            "duplicate_of":None,"status":"failed"})
    # DICOM first; anonymized copies last. Do not drop JPEG just because DICOM exists for a study.
    candidates.sort(key=lambda c:(c["alias"],c["m"]["source_format"]!="DICOM",c["m"]["source_id"]))
    canonical=[]
    for c in candidates:
        m=c["m"]; possible=[]; manual=cfg.get("duplicate_overrides",{}).get(m["source_id"])
        for target in canonical:
            t=target["m"]
            if t["study_id"]!=m["study_id"]:continue
            same_key=(t.get("series_no"),t.get("index"))==(m.get("series_no"),m.get("index"))
            if not (c["alias"] or same_key or manual==t["source_id"]):continue
            sim=similarity(c["original"],target["original"],c["match_mask"],target["match_mask"])
            if sim["match"] or manual==t["source_id"]: possible.append((target,sim))
        if len(possible)==1:
            t,sim=possible[0]
            c["source"].update(status="duplicate",duplicate_of=t["m"]["source_id"],match=sim,
                               match_method="human_explicit" if manual else "masked_phash+ncc+rmse")
            continue
        if c["alias"] or len(possible)>1 or manual:
            c["source"]["status"]="unresolved"
            issues.append({"code":"AMBIGUOUS_DUPLICATE" if len(possible)>1 else "UNMATCHED_ALIAS_OR_OVERRIDE",
                           "severity":"error","source_id":m["source_id"]})
            continue
        canonical.append(c)
    # DICOM series counts derive from complete inventoried frames; do not overwrite contradictory tags.
    groups=defaultdict(list)
    for c in canonical:groups[(c["m"]["study_id"],c["m"].get("series_no"))].append(c)
    slices=[];series=[];review={};used_keys=set()
    for (study_id,number),group in groups.items():
        group.sort(key=lambda c:c["m"].get("index") if isinstance(c["m"].get("index"),int) else -1)
        expected=studies[study_id].get("expected_series_counts",{}).get(number)
        dcm_uids={c["uid"] for c in group if c["uid"]}
        if len(dcm_uids)>1:issues.append({"code":"DICOM_SERIES_NUMBER_COLLISION","severity":"error","study_id":study_id,"series_no":number})
        for c in group:
            m=c["m"]
            if m["count"] is None and m["source_format"]=="DICOM":
                m["count"]=len(group);m["count_origin"]="inventoried_DICOM_frames_not_acquisition_guarantee"
            if m["count"] is None and expected:
                m["count"]=expected;m["count_origin"]="configured_expectation"
            for key in REQUIRED:
                if m.get(key) is None:c["issues"].append({"code":"METADATA_MISSING","field":key,"severity":"error"})
            for i in c["issues"]:issues.append({**i,"source_id":m["source_id"]})
            if any(m.get(k) is None for k in ("series_no","index","series_desc")):
                c["source"]["status"]="unresolved";continue
            key=(study_id,m["series_no"],m["index"])
            if key in used_keys:
                issues.append({"code":"SLICE_KEY_COLLISION","severity":"error","source_id":m["source_id"]});c["source"]["status"]="unresolved";continue
            used_keys.add(key)
            identifier(m["series_desc"])
            folder=f"s{int(number):02d}_{m['series_desc']}"
            try:
                filename=slice_filename(study_id,m["series_no"],m["series_desc"],m["index"])
            except SafeError as e:
                issues.append({"code":str(e),"severity":"error","source_id":m["source_id"]})
                c["source"]["status"]="unresolved";continue
            rel=f"studies/{study_id}/{folder}/{filename}"
            path=out/rel;path.parent.mkdir(parents=True,exist_ok=True)
            c["clean"].save(path,format="PNG",optimize=True)
            m.update(path=rel,filename=filename,slice_id=Path(filename).stem,
                     png_sha256=sha256(path),pixel_data_path=None,pixel_data_sha256=None)
            if c["pixels"] is not None:
                pixels=c["pixels"].copy();pixels[c["mask"]]=0
                pixelpath=path.with_suffix(".npz")
                # This contains no DICOM tag metadata and no unredacted pixel values in masked regions.
                np.savez_compressed(pixelpath,pixels=pixels,mask=c["mask"].astype(np.uint8),invert=np.array(c["invert"]))
                m["pixel_data_path"]=pixelpath.relative_to(out).as_posix();m["pixel_data_sha256"]=sha256(pixelpath)
            slices.append(m);c["source"]["status"]="canonical"
            review[m["source_id"]]={"png_sha256":m["png_sha256"],"pixel_data_sha256":m["pixel_data_sha256"],
                                      "phi_free":False,"anatomy_preserved":False,
                                      "all_dicom_windows_checked":False,"approved":False}
        usable=[c for c in group if c["source"]["status"]=="canonical"]
        if not usable:continue
        first=usable[0]["m"]
        for field in ("series_desc","plane","weighting","fat_sat"):
            if len({c["m"].get(field) for c in usable})>1:
                issues.append({"code":"INCONSISTENT_SERIES_METADATA","field":field,"severity":"error","study_id":study_id,"series_no":number})
        observed=[c["m"]["index"] for c in usable]
        declared={c["m"]["count"] for c in usable if c["m"].get("count") is not None}
        if expected:declared.add(expected)
        count=next(iter(declared)) if len(declared)==1 else None
        if count is None or sorted(observed)!=list(range(1,count+1)):
            issues.append({"code":"SERIES_COUNT_OR_INDEX_MISMATCH","severity":"error","study_id":study_id,"series_no":number,
                           "observed_count":len(observed),"declared_counts":sorted(declared)})
        sheet=contact_sheet([(c["m"]["index"],c["clean"]) for c in usable])
        sheetname=contact_sheet_filename(study_id,number,first["series_desc"])
        sheetrel=f"contact_sheets/{study_id}/{sheetname}"
        sheetpath=out/sheetrel;sheetpath.parent.mkdir(parents=True,exist_ok=True);sheet.save(sheetpath,optimize=True)
        series.append({"study_id":study_id,"series_no":number,"series_desc":first["series_desc"],"plane":first["plane"],
                       "weighting":first["weighting"],"fat_sat":first["fat_sat"],"count":len(usable),
                       "declared_count":count,"slice_thickness_mm":first["slice_thickness_mm"],
                       "contact_sheet_filename":sheetname,
                       "contact_sheet_path":sheetrel,"contact_sheet_sha256":sha256(sheetpath)})
    # Explicitly detect wholly missing expected series, not just gaps in a series that was found.
    for study in studies.values():
        if not any(s["study_id"]==study["study_id"] for s in slices):continue
        for n in study.get("expected_series_counts",{}):
            if (study["study_id"],n) not in groups:
                issues.append({"code":"EXPECTED_SERIES_MISSING","severity":"error","study_id":study["study_id"],"series_no":n})
    for p,digest in raw_before.items():
        if sha256(p)!=digest:issues.append({"code":"RAW_CHANGED_DURING_BUILD","severity":"error"})
    for p in out.rglob("*.png"):
        try:
            with Image.open(p) as im:im.verify()
            with Image.open(p) as im:im.load()
        except Exception:issues.append({"code":"PNG_DECODE_FAILED","severity":"error"})
    for s in sources:
        if s["status"]=="pending":s["status"]="unresolved"
    ndup=sum(s["status"]=="duplicate" for s in sources)
    stats={"source_image_files":len(raw_before),"source_image_frames":len(sources),"unique_slices":len(slices),
           "duplicates":ndup,"unresolved_or_failed":sum(s["status"] in ("unresolved","failed") for s in sources),
           "series":len(series),"errors":sum(i["severity"]=="error" for i in issues),
           "raw_unchanged":not any(i["code"]=="RAW_CHANGED_DURING_BUILD" for i in issues)}
    if not slices:issues.append({"code":"NO_USABLE_SLICES","severity":"error"});stats["errors"]+=1
    # Link all sources (including aliases) to the one derived canonical filename.
    # Original relative paths are kept only in the local private mapping below.
    by_source={s["source_id"]:s for s in slices}
    for source in sources:
        target=by_source.get(source.get("duplicate_of") or source["source_id"])
        source["canonical_slice_id"]=target["slice_id"] if target else None
        source["output_filename"]=target["filename"] if target else None
    manifest={"schema_version":1,"naming_version":NAMING_VERSION,"created_at":datetime.now(timezone.utc).isoformat(),
              "status":"requires_review","deid_policy":POLICY,
              "studies":[{k:s[k] for k in ("study_id","date","hospital","pre_or_post_op")} for s in studies.values()
                         if any(x["study_id"]==s["study_id"] for x in slices)],
              "series":series,"slices":slices,"sources":sources,"non_image_assets":inventory,"issues":issues,"statistics":stats}
    json_write(out/"manifest.json",manifest);json_write(out/"review.json",{"slices":review})
    json_write(private/"provenance.json",provenance);(private/"provenance.json").chmod(0o600)
    # This CSV may contain PHI in original filenames. Never deploy it or commit it.
    rename_map=private/"rename_map.csv"
    with rename_map.open("w",encoding="utf-8-sig",newline="") as f:
        rename_map.chmod(0o600)
        writer=csv.DictWriter(f,fieldnames=["source_id","original_relative_path","frame_index",
                                          "status","duplicate_of","canonical_slice_id",
                                          "output_filename","output_relative_path"])
        writer.writeheader()
        for source in sources:
            sid=source["source_id"]
            target=by_source.get(source.get("duplicate_of") or sid)
            origin=provenance.get(sid,{})
            # Neutralise formula-leading source paths when opened in a spreadsheet.
            original=origin.get("source_path","")
            if original.lstrip().startswith(("=","+","-","@")) or original.startswith(("\t","\r","\n")):
                original="'"+original
            writer.writerow({"source_id":sid,"original_relative_path":original,
                             "frame_index":source.get("frame_index"),"status":source["status"],
                             "duplicate_of":source.get("duplicate_of"),
                             "canonical_slice_id":source["canonical_slice_id"],
                             "output_filename":source["output_filename"],
                             "output_relative_path":target["path"] if target else ""})
    report=["# MRI dataset validation", "", "Status: NOT RELEASED — full-resolution human privacy/anatomy review required.",
            "", "OCR checks are screening only, especially with non-English names. This is not a diagnostic or regulatory de-identification certification.", "",
            "|Metric|Count|","|---|---:|"]+[f"|{k}|{v}|" for k,v in stats.items()]
    report += ["","## Series", "|Study|Series|Observed|Declared|","|---|---:|---:|---:|"]
    report += [f"|{s['study_id']}|{s['series_no']}|{s['count']}|{s['declared_count']}|" for s in series]
    report += ["","## Issues (never raw OCR text)"]+[f"- {i['severity']}: {i['code']} — {i.get('source_id',i.get('study_id','dataset'))}" for i in issues]
    report += ["","## Duplicate links"]+[f"- {s['source_id']} -> {s['duplicate_of']}" for s in sources if s["duplicate_of"]]
    (out/"validation_report.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    blocks=["<!doctype html><meta charset='utf-8'><title>Local privacy review — not diagnostic</title>",
            "<h1>Review every image at full resolution</h1><p>Do not publish this directory. Inspect names, IDs, Chinese text, retained direction letters, and anatomy masked by accident. Edit review.json only after review.</p>"]
    for s in slices:
        blocks.append(f"<h2>{html.escape(s['source_id'])}</h2><p>{html.escape(s['study_id'])} S{s['series_no']} I{s['index']}</p><img src='{html.escape(s['path'],quote=True)}' alt='sanitized candidate'>")
    (out/"review.html").write_text("\n".join(blocks),encoding="utf-8")
    return manifest
