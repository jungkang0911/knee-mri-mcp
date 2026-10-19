from pathlib import Path
import numpy as np
import pytest
from PIL import Image,ImageDraw
from knee_mri.common import json_write,sha256
from knee_mri.dataset import release
from knee_mri.imaging import contact_sheet
from knee_mri.metadata import laterality_note

@pytest.fixture
def demo_dataset(tmp_path):
    root=tmp_path/"dataset";root.mkdir()
    rows=[];sources=[]
    for index in range(1,4):
        y,x=np.mgrid[:256,:256]
        a=np.clip(150*np.exp(-((x-128)**2+(y-125)**2)/5000)+index*x/8,0,255).astype(np.uint8)
        im=Image.fromarray(a)
        rel=f"studies/2019-03-20_TSGH/s07_COR_PD/{index:04}.png"
        path=root/rel;path.parent.mkdir(parents=True,exist_ok=True);im.save(path)
        sid=f"synthetic_{index}"
        pixelpath=None;pixelhash=None
        if index==1:
            mask=np.zeros(a.shape,dtype=np.uint8);mask[:20,:80]=1
            pixels=a.astype(np.float32)*10;pixels[mask.astype(bool)]=0
            pp=path.with_suffix(".npz");np.savez_compressed(pp,pixels=pixels,mask=mask,invert=np.array(False))
            pixelpath=pp.relative_to(root).as_posix();pixelhash=sha256(pp)
        rows.append({"study_id":"2019-03-20_TSGH","date":"2019-03-20","hospital":"TSGH","pre_or_post_op":"pre_op",
                     "series_no":7,"series_desc":"COR_PD","plane":"coronal","weighting":"PD","fat_sat":False,
                     "index":index,"count":3,"slice_thickness_mm":4.,"slice_location_mm":index*4.,"window":[1280,2560],
                     "source_id":sid,"source_path":"source://"+sid,"sha256":"0"*64,"duplicate_of":None,
                     "dimensions":[256,256],"orientation_verified":True,"laterality_note":laterality_note("coronal",True),
                     "source_format":"DICOM" if index==1 else "JPEG","path":rel,"png_sha256":sha256(path),
                     "pixel_data_path":pixelpath,"pixel_data_sha256":pixelhash})
        sources.append({"source_id":sid,"duplicate_of":None,"status":"canonical"})
    sheet=contact_sheet([(s["index"],Image.open(root/s["path"])) for s in rows])
    sr="contact_sheets/2019-03-20_TSGH/s07_COR_PD.png";p=root/sr;p.parent.mkdir(parents=True);sheet.save(p)
    series=[{"study_id":"2019-03-20_TSGH","series_no":7,"series_desc":"COR_PD","plane":"coronal","weighting":"PD",
             "fat_sat":False,"count":3,"declared_count":3,"slice_thickness_mm":4.,"contact_sheet_path":sr,"contact_sheet_sha256":sha256(p)}]
    m={"schema_version":1,"studies":[{"study_id":"2019-03-20_TSGH","date":"2019-03-20","hospital":"TSGH","pre_or_post_op":"pre_op"}],
       "series":series,"slices":rows,"sources":sources,"issues":[],"statistics":{"source_image_frames":3,"duplicates":0,"raw_unchanged":True}}
    json_write(root/"manifest.json",m)
    # Deliberately synthetic images only: tests may approve their own non-clinical fixture.
    json_write(root/"review.json",{"slices":{s["source_id"]:{"approved":True,"phi_free":True,"anatomy_preserved":True,
                                  "png_sha256":s["png_sha256"],"pixel_data_sha256":s["pixel_data_sha256"],"all_dicom_windows_checked":True} for s in rows}})
    release(root)
    return root
