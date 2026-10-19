from __future__ import annotations
from pathlib import Path
import numpy as np
from .common import SafeError,finite_float
from .metadata import technical_description,laterality_note
from .imaging import window_pixels


def looks_like_dicom(path: Path) -> bool:
    if path.suffix.lower() in (".dcm",".dicom"): return True
    try:
        with path.open("rb") as f:
            f.seek(128)
            return f.read(4)==b"DICM"
    except OSError:
        return False


def read_dicom(path: Path) -> list[dict]:
    """Classic and Enhanced monochrome MR. Output no PHI tags, raw UIDs, overlays or DICOM file."""
    try:
        import pydicom
        from pydicom.pixels import apply_modality_lut
    except ImportError:
        raise SafeError("PYDICOM_NOT_INSTALLED") from None
    try:
        ds=pydicom.dcmread(path)
        if ds.Modality != "MR" or int(getattr(ds,"SamplesPerPixel",1))!=1:
            raise SafeError("UNSUPPORTED_DICOM_MODALITY")
        photo=str(ds.PhotometricInterpretation)
        if photo not in ("MONOCHROME1","MONOCHROME2"):
            raise SafeError("UNSUPPORTED_DICOM_PHOTOMETRIC")
        arr=ds.pixel_array
        frames=arr[None,...] if arr.ndim==2 else arr
        if frames.ndim!=3: raise SafeError("UNSUPPORTED_DICOM_DIMENSIONS")
        # Pixel values only: overlays are not rendered. Private tags/headers never leave this function.
        shared=getattr(ds,"SharedFunctionalGroupsSequence",[None])[0]
        perframes=getattr(ds,"PerFrameFunctionalGroupsSequence",[])
        def attribute(frame, sequence, field, default=None):
            for group in (frame,shared):
                if group is not None and hasattr(group,sequence):
                    item=getattr(group,sequence)[0]
                    if hasattr(item,field): return getattr(item,field)
            return getattr(ds,field,default)
        def first_float(value):
            if isinstance(value,(list,tuple)) or value.__class__.__name__=="MultiValue":
                value=value[0] if value else None
            return finite_float(value)
        result=[]
        for f,p in enumerate(frames):
            fg=perframes[f] if f<len(perframes) else None
            # Per-frame transforms take priority over shared/top-level transforms.
            slope=first_float(attribute(fg,"PixelValueTransformationSequence","RescaleSlope",1))
            intercept=first_float(attribute(fg,"PixelValueTransformationSequence","RescaleIntercept",0))
            if slope is None or intercept is None: raise SafeError("INVALID_DICOM_RESCALE")
            if getattr(ds,"ModalityLUTSequence",None) and fg is None and shared is None:
                pixels=np.asarray(apply_modality_lut(p,ds),dtype=np.float32)
            else:
                pixels=p.astype(np.float32)*slope+intercept
            wc=first_float(attribute(fg,"FrameVOILUTSequence","WindowCenter"))
            ww=first_float(attribute(fg,"FrameVOILUTSequence","WindowWidth"))
            if wc is None or ww is None or ww<1:
                lo,hi=np.percentile(pixels,[.5,99.5])
                wc=float((lo+hi)/2);ww=max(1.,float(hi-lo))
                window_source="pixel_percentiles_not_dicom_voi"
            else:
                window_source="dicom_window"
            iop=attribute(fg,"PlaneOrientationSequence","ImageOrientationPatient")
            ipp=attribute(fg,"PlanePositionSequence","ImagePositionPatient")
            iop=[float(x) for x in iop] if iop is not None else None
            ipp=[float(x) for x in ipp] if ipp is not None else None
            desc=technical_description(str(getattr(ds,"SeriesDescription","")))
            mixed=desc.pop("mixed_weighting")
            loc=first_float(getattr(ds,"SliceLocation",None))
            if iop and ipp:
                normal=np.cross(iop[:3],iop[3:])
                normal=normal/np.linalg.norm(normal)
                desc["plane"]=("sagittal","coronal","axial")[int(np.argmax(np.abs(normal)))]
                loc=float(np.dot(ipp,normal))
            date=str(getattr(ds,"StudyDate",""))
            date=f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date)==8 else None
            # Internal fields starting '_' are dropped by the builder.
            m={**desc,"date":date,"series_no":int(getattr(ds,"SeriesNumber",0)),
               "index":int(getattr(ds,"InstanceNumber",f+1)) if len(frames)==1 else f+1,
               "count":int(getattr(ds,"ImagesInAcquisition",0)) or (len(frames) if len(frames)>1 else None),
               "slice_thickness_mm":first_float(attribute(fg,"PixelMeasuresSequence","SliceThickness")),
               "slice_location_mm":loc,"window":[wc,ww],"window_source":window_source,
               "pixel_spacing_mm": [float(x) for x in attribute(fg,"PixelMeasuresSequence","PixelSpacing",[])] or None,
               "image_orientation_patient":iop,"image_position_patient":ipp,
               "orientation_verified":bool(iop and iop[0]>.95 and desc["plane"] in ("axial","coronal")),
               "metadata_origin":{"technical":"DICOM tags; per-frame/shared groups preferred"},
               "_series_uid":str(getattr(ds,"SeriesInstanceUID","")),"_study_uid":str(getattr(ds,"StudyInstanceUID","")),
               "_laterality":str(getattr(ds,"ImageLaterality",getattr(ds,"Laterality",""))),
               "_mixed":mixed,"_frame":f,
               "_voi_function":str(attribute(fg,"FrameVOILUTSequence","VOILUTFunction","LINEAR"))}
            issues=[]
            if m["_voi_function"] not in ("LINEAR","") or getattr(ds,"VOILUTSequence",None):
                issues.append({"code":"NONLINEAR_DICOM_VOI_REQUIRES_REVIEW", "severity":"error"})
            if mixed: issues.append({"code":"AMBIGUOUS_WEIGHTING","severity":"error"})
            image=window_pixels(pixels,[wc,ww],photo=="MONOCHROME1")
            result.append({"metadata":m,"image":image,"pixels":pixels,"invert":photo=="MONOCHROME1","issues":issues})
        return result
    except SafeError: raise
    except Exception:
        raise SafeError("DICOM_READ_OR_DECODE_FAILED") from None
