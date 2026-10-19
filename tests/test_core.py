import asyncio
import json
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
import httpx
from knee_mri.auth import PrivateBoundary
from knee_mri.common import SafeError,safe_path,json_read,json_write
from knee_mri.dataset import Dataset,release
from knee_mri.deid import redact,ocr_tokens
from knee_mri.imaging import window_pixels,similarity,phash,png_bytes,apply_crop_zoom
from knee_mri.metadata import parse_jpeg_metadata,technical_description

def token(text,x,y,line=1,confidence=90):
    return {"text":text,"box":[x,y,x+max(8,len(text)*8),y+12],"line":[1,1,line],"confidence":confidence}

def test_parse_and_cross_validation():
    tokens=[token("Axial T2 FS",0,0,1),token("Series:4",0,30,2),token("Images 8/18",0,60,3),
            token("ST:5 SL:-34.2",0,90,4),token("WL:201 WW:600",0,120,5),token("2019/03/20",0,140,6)]
    m,issues=parse_jpeg_metadata(tokens,"img-00004-00008.jpg","2019-03-20")
    assert (m['series_no'],m['index'],m['count'])==(4,8,18)
    assert m['plane']=='axial' and m['weighting']=='T2' and m['fat_sat'] is True
    assert m['slice_location_mm']==-34.2 and m['window']==[201,600]
    assert not issues

def test_ocr_conflict_and_dob_not_adopted():
    m,issues=parse_jpeg_metadata([token('Series 6 Images 3/20',0,0),token('1985/01/01',0,40,2)],'img-00007-00002.jpg','2019-03-20')
    assert m['date']=='2019-03-20'
    assert sum(i['code']=='OCR_FILENAME_CONFLICT' for i in issues)==2
    assert all('1985' not in json.dumps(i) for i in issues)

def test_mixed_weighting_not_guessed():
    assert technical_description('SAG PD+T2 FSE')['weighting'] is None
    assert technical_description('COR T2FS')['fat_sat'] is True

def test_deid_dynamic_box_and_direction():
    im=Image.new('L',(256,256),100)
    tokens=[token('JOHN TEST',30,35),token('R',3,128,2)]
    clean,mask,_,keep,issues=redact(im,tokens,verify=False)
    assert np.asarray(clean)[40,40]==0
    assert np.asarray(clean)[130,5]==100
    assert keep[0]['letter']=='R' and not issues

def test_patient_initial_not_preserved():
    im=Image.new('L',(256,256),100)
    tokens=[token('R',3,10),token('SMITH',18,10)]
    _,mask,_,keep,_=redact(im,tokens,verify=False)
    assert mask[15,5] and not keep

def test_residual_is_error_without_text(monkeypatch):
    monkeypatch.setattr('knee_mri.deid.ocr_tokens',lambda im:[token('SECRET123',12,12)])
    _,_,_,_,issues=redact(Image.new('L',(128,128)),[])
    assert issues[0]['code']=='RESIDUAL_TEXT_DETECTED'
    assert 'SECRET' not in json.dumps(issues)

def test_mask_union_duplicate_and_adjacent():
    rng=np.random.default_rng(4);a=rng.integers(0,256,(128,128),dtype=np.uint8);b=a.copy();b[:10]=0
    ma=np.zeros(a.shape,bool);mb=ma.copy();mb[:10]=True
    assert similarity(Image.fromarray(a),Image.fromarray(b),ma,mb)['match']
    shifted=np.roll(a,1,axis=0)
    assert not similarity(Image.fromarray(a),Image.fromarray(shifted),ma,ma)['match']

@pytest.mark.parametrize('crop',[[-1,0,10,10],[0,0,300,20],[10,10,5,5],[0,0,10.5,20]])
def test_bad_crop(crop):
    with pytest.raises(SafeError):apply_crop_zoom(Image.new('L',(256,256)),crop,1)

@pytest.mark.parametrize('zoom',[0,.5,5,float('nan'),float('inf'),True])
def test_bad_zoom(zoom):
    with pytest.raises(SafeError):apply_crop_zoom(Image.new('L',(10,10)),None,zoom)

def test_png_limit_is_not_silent_resize():
    im=Image.fromarray(np.random.default_rng(5).integers(0,256,(1200,1200),dtype=np.uint8))
    with pytest.raises(SafeError,match='EXCEEDS_LIMIT'):png_bytes(im)

def test_window_and_inversion_mask():
    pixels=np.array([[0.,100.],[500.,1000.]])
    mask=np.array([[1,0],[0,0]])
    for invert in (False,True):
        assert np.asarray(window_pixels(pixels,[400,800],invert,mask))[0,0]==0

def test_store_tools(demo_dataset):
    d=Dataset(demo_dataset)
    assert len(d.list_studies())==1 and d.list_series('2019-03-20_TSGH')[0]['count']==3
    image,meta=d.get_image('2019-03-20_TSGH',7,2,[10,20,110,120],2)
    assert image.size==(200,200) and 'source_path' not in meta
    assert len(d.get_slices('2019-03-20_TSGH',7,1,3))==3
    assert d.get_contact_sheet('2019-03-20_TSGH',7).height>0
    image,meta=d.compare({'study':'2019-03-20_TSGH','series':7,'index':1},{'study':'2019-03-20_TSGH','series':7,'index':2})
    assert image.width==524 and '未配準' in meta['warning']

def test_window_is_dicom_only_and_mask_stays(demo_dataset):
    d=Dataset(demo_dataset)
    with pytest.raises(SafeError,match='DICOM'):d.get_image('2019-03-20_TSGH',7,2,window=[200,1000])
    im,_=d.get_image('2019-03-20_TSGH',7,1,window=[0,10000])
    assert np.asarray(im)[0,0]==0

def test_batch_limit_and_missing(demo_dataset):
    d=Dataset(demo_dataset)
    with pytest.raises(SafeError):d.get_slices('2019-03-20_TSGH',7,1,9)
    with pytest.raises(SafeError,match='NOT_FOUND'):d.get_slices('2019-03-20_TSGH',7,2,4)

@pytest.mark.parametrize('p',['../raw/foo','/etc/passwd','../../dataset'])
def test_traversal(tmp_path,p):
    with pytest.raises(SafeError):safe_path(tmp_path,p)

def test_symlink(tmp_path):
    (tmp_path/'link').symlink_to('/etc/passwd')
    with pytest.raises(SafeError):safe_path(tmp_path,'link')

def test_release_requires_review(demo_dataset):
    r=json_read(demo_dataset/'review.json');r['slices']['synthetic_1']['approved']=False
    json_write(demo_dataset/'review.json',r)
    with pytest.raises(SafeError,match='HUMAN_REVIEW'):release(demo_dataset)

def test_seal_detects_image_tampering(demo_dataset):
    d=Dataset(demo_dataset)
    p=demo_dataset/'studies/2019-03-20_TSGH/s07_COR_PD/0002.png';p.write_bytes(b'changed')
    with pytest.raises(SafeError,match='DATASET_CHANGED'):d.get_image('2019-03-20_TSGH',7,2)

def test_seal_detects_manifest_tampering(demo_dataset):
    m=json_read(demo_dataset/'manifest.json');m['slices'][0]['path']='../raw/secret'
    json_write(demo_dataset/'manifest.json',m)
    with pytest.raises(SafeError):Dataset(demo_dataset)

def test_bearer_boundary():
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'ok'})
    wrapped=PrivateBoundary(app,'a'*48,['testserver'],['https://trusted.example'])
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped),base_url='http://testserver') as c:
            assert (await c.post('/mcp')).status_code==401
            assert (await c.post('/mcp?token='+'a'*48)).status_code==401
            good={'Authorization':'Bearer '+'a'*48}
            assert (await c.post('/mcp',headers=good)).status_code==200
            assert (await c.post('/mcp',headers={**good,'Origin':'https://evil.example'})).status_code==403
            assert (await c.post('/mcp',headers={**good,'Host':'evil.example'})).status_code==400
            assert (await c.get('/dataset/manifest.json',headers=good)).status_code==404
            assert (await c.post('/mcp',headers=good,content=b'x'*140000)).status_code==413
    asyncio.run(run())

def test_weak_token_rejected():
    with pytest.raises(SafeError):PrivateBoundary(None,'short',['test'],[])

def test_month_name_date_and_oblique_directions():
    from knee_mri.metadata import direction_label
    m,issues=parse_jpeg_metadata([token('20-March-2019',0,0)],'img-00004-00007.jpg','2019-03-20')
    assert not any(i['code']=='ACQUISITION_DATE_NOT_OCR_CONFIRMED' for i in issues)
    assert all(direction_label(s) for s in ('AR','PL','RP','LA','S','R'))
    assert not direction_label('MR') and not direction_label('AP')
