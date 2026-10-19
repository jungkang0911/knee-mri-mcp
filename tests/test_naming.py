"""Derived filenames and provenance tests. All input images here are synthetic."""
import csv
from pathlib import Path

import numpy as np
from PIL import Image
import pytest
import yaml

from knee_mri.build import build
from knee_mri.common import SafeError, json_read, json_write, sha256
from knee_mri.dataset import Dataset, release
from knee_mri.naming import contact_sheet_filename, slice_filename


@pytest.mark.parametrize('study,series,desc,index,expected', [
    ('2019-03-20_TSGH', 7, 'COR_PD', 13, '2019-03-20_TSGH_RK_S07_COR_PD_I0013.png'),
    ('2024-12-03_VGHTPE', 3, 'AX_T2_FS_FSE', 12, '2024-12-03_VGHTPE_RK_S03_AX_T2_FS_FSE_I0012.png'),
    ('2024-12-03_VGHTPE', 8, 'SAG_PD_T2_FSE', 16, '2024-12-03_VGHTPE_RK_S08_SAG_PD_T2_FSE_I0016.png'),
    ('2024-12-03_VGHTPE', 5, 'COR_3D_MERGE', 2, '2024-12-03_VGHTPE_RK_S05_COR_3D_MERGE_I0002.png'),
])
def test_descriptive_filename(study, series, desc, index, expected):
    assert slice_filename(study, series, desc, index) == expected


def test_contact_name_and_no_collisions():
    assert contact_sheet_filename('2019-03-20_TSGH',7,'COR_PD') == '2019-03-20_TSGH_RK_S07_COR_PD_CONTACT.png'
    names = {slice_filename(study, series, 'COR_PD', index)
             for study in ['2019-03-20_TSGH','2024-12-03_VGHTPE']
             for series in [7,8] for index in [1,2]}
    assert len(names) == 8


@pytest.mark.parametrize('field,value', [
    ('study_id','../raw'), ('series_no',True), ('series_no',-1),
    ('index',True), ('index',0), ('index','13'),
    ('series_desc','COR_PD_edema'), ('series_desc','PATIENT_NAME'),
    ('series_desc','../COR_PD'), ('series_desc',''),
])
def test_reject_unsafe_or_invented_names(field, value):
    args = dict(study_id='2019-03-20_TSGH',series_no=7,series_desc='COR_PD',index=13)
    args[field]=value
    with pytest.raises(SafeError):
        slice_filename(**args)


def test_legacy_dataset_serves_descriptive_metadata(demo_dataset):
    # The fixture deliberately retains old 0001.png paths for backward compatibility.
    old = json_read(demo_dataset/'manifest.json')['slices'][0]['path']
    assert Path(old).name == '0001.png'
    ds = Dataset(demo_dataset)
    _, meta = ds.get_image('2019-03-20_TSGH',7,1)
    assert meta['filename'] == '2019-03-20_TSGH_RK_S07_COR_PD_I0001.png'
    assert meta['slice_id'] == Path(meta['filename']).stem
    assert 'source_path' not in meta
    assert (demo_dataset/old).is_file()  # The reader does not rename legacy data.


def test_build_names_duplicates_private_mapping_and_mcp_lookup(tmp_path, monkeypatch):
    raw=tmp_path/'raw';original=raw/'2019-03-20_TSGH';alias=raw/'Anonymized_20251126'
    original.mkdir(parents=True);alias.mkdir()
    a=np.random.default_rng(18).integers(0,256,(128,128),dtype=np.uint8)
    b=np.random.default_rng(77).integers(0,256,(128,128),dtype=np.uint8)
    Image.fromarray(a).save(original/'img-00007-00001.png')
    Image.fromarray(b).save(original/'img-00007-00002.png')
    Image.fromarray(a).save(alias/'image-00000.png')
    before={p:sha256(p) for p in raw.rglob('*.png')}

    # Unit fixture only: simulated verified metadata, not a real OCR/privacy pass.
    monkeypatch.setattr('knee_mri.build.ocr_tokens',lambda im:[])
    def parse(tokens,name,date):
        index=2 if name.endswith('00002.png') else 1
        return dict(series_no=7,series_desc='COR_PD',plane='coronal',weighting='PD',fat_sat=False,
                    index=index,count=2,slice_thickness_mm=4.,slice_location_mm=None,window=None,date=date),[]
    monkeypatch.setattr('knee_mri.build.parse_jpeg_metadata',parse)
    def clean(image,tokens,*args,**kwargs):
        mask=np.zeros((image.height,image.width),dtype=bool)
        return image.copy(),mask,mask,[],[]
    monkeypatch.setattr('knee_mri.build.redact',clean)
    cfg={'studies':[{'study_id':'2019-03-20_TSGH','date':'2019-03-20','hospital':'TSGH',
                    'pre_or_post_op':'pre_op','path_regex':'2019-03-20','expected_series_counts':{7:2}}],
         'aliases':[{'path_regex':'Anonymized','canonical_study_id':'2019-03-20_TSGH'}]}
    cp=tmp_path/'config.yaml';cp.write_text(yaml.safe_dump(cfg))
    out=tmp_path/'dataset';private=tmp_path/'private'
    m=build(raw,out,cp,private)
    assert m['naming_version']=='descriptive-v1'
    assert m['statistics']['unique_slices']==2
    assert m['statistics']['duplicates']==1 and m['statistics']['errors']==0
    assert all(sha256(p)==digest for p,digest in before.items())
    for row in m['slices']:
        assert row['filename']==slice_filename(row['study_id'],7,'COR_PD',row['index'])
        assert Path(row['path']).name==row['filename']
        assert row['slice_id']==Path(row['filename']).stem
        with Image.open(out/row['path']) as im: im.verify()
    series=m['series'][0]
    assert Path(series['contact_sheet_path']).name=='2019-03-20_TSGH_RK_S07_COR_PD_CONTACT.png'
    with Image.open(out/series['contact_sheet_path']) as im: im.verify()
    duplicate=next(s for s in m['sources'] if s['status']=='duplicate')
    target=next(s for s in m['slices'] if s['source_id']==duplicate['duplicate_of'])
    assert duplicate['output_filename']==target['filename']
    assert duplicate['canonical_slice_id']==target['slice_id']
    with (private/'rename_map.csv').open(encoding='utf-8-sig',newline='') as f:
        mapping=list(csv.DictReader(f))
    assert len(mapping)==3
    assert any(r['original_relative_path'].startswith('Anonymized_20251126/') and
               r['output_filename']==target['filename'] for r in mapping)
    assert (private/'rename_map.csv').stat().st_mode & 0o777 == 0o600
    assert not (out/'rename_map.csv').exists()
    assert 'Anonymized' not in (out/'manifest.json').read_text()
    # Synthetic data can be approved by its own test. No real image is approved.
    review=json_read(out/'review.json')
    for item in review['slices'].values():
        item.update(approved=True,phi_free=True,anatomy_preserved=True)
    json_write(out/'review.json',review);release(out)
    ds=Dataset(out)
    im,meta=ds.get_image('2019-03-20_TSGH',7,1)
    assert meta['filename'].endswith('_I0001.png')
    assert im.size==(128,128)
    assert len(ds.get_slices('2019-03-20_TSGH',7,1,2))==2
    assert ds.get_contact_sheet('2019-03-20_TSGH',7).width>0


def test_dicom_sidecar_uses_same_new_stem(tmp_path,monkeypatch):
    raw=tmp_path/'raw'/'2019-03-20_TSGH';raw.mkdir(parents=True)
    (raw/'sample.dcm').write_bytes(b'synthetic-marker-only')
    a=np.random.default_rng(45).integers(0,256,(128,128),dtype=np.uint8)
    meta=dict(series_no=7,series_desc='COR_PD',plane='coronal',weighting='PD',fat_sat=False,
              index=1,count=1,slice_thickness_mm=4.,slice_location_mm=None,window=[128,256],
              date='2019-03-20',_laterality='R',_series_uid='synthetic-uid')
    # Exercise the sidecar writer without pretending to decode a DICOM file.
    monkeypatch.setattr('knee_mri.build.read_dicom',lambda p:[dict(image=Image.fromarray(a),pixels=a.astype(np.float32),
                                                               invert=False,metadata=meta.copy(),issues=[])])
    monkeypatch.setattr('knee_mri.build.ocr_tokens',lambda im:[])
    def clean(image,tokens,*args,**kwargs):
        mask=np.zeros((image.height,image.width),dtype=bool)
        return image.copy(),mask,mask,[],[]
    monkeypatch.setattr('knee_mri.build.redact',clean)
    cfg={'studies':[{'study_id':'2019-03-20_TSGH','date':'2019-03-20','hospital':'TSGH',
                    'pre_or_post_op':'pre_op','path_regex':'2019-03-20','expected_series_counts':{7:1}}]}
    cp=tmp_path/'config.yaml';cp.write_text(yaml.safe_dump(cfg))
    out=tmp_path/'dataset'
    m=build(raw.parent,out,cp,tmp_path/'private')
    row=m['slices'][0]
    assert Path(row['pixel_data_path']).name=='2019-03-20_TSGH_RK_S07_COR_PD_I0001.npz'
    assert (out/row['pixel_data_path']).is_file()
    with np.load(out/row['pixel_data_path'],allow_pickle=False) as z:
        assert z['pixels'].shape==(128,128)
