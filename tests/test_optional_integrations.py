import asyncio
import base64
from io import BytesIO
import pytest
from PIL import Image

def test_official_sdk_imagecontent_types(demo_dataset):
    pytest.importorskip('mcp',reason='Official MCP SDK not installed; NOT a passing MCP integration test')
    from knee_mri.server import image_content,create_app
    from mcp.types import ImageContent
    item=image_content(Image.new('L',(20,20),30))
    assert isinstance(item,ImageContent) and item.type=='image' and item.mimeType=='image/png'
    with Image.open(BytesIO(base64.b64decode(item.data))) as im:assert im.format=='PNG'
    app=create_app(demo_dataset,'t'*48,['testserver'],[])
    assert app is not None

def test_pydicom_classic_roundtrip(tmp_path):
    pydicom=pytest.importorskip('pydicom',reason='pydicom not installed; DICOM runtime validation NOT completed')
    import numpy as np
    from pydicom.dataset import FileDataset,FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian,MRImageStorage,generate_uid
    from knee_mri.dicom_io import read_dicom
    fm=FileMetaDataset();fm.TransferSyntaxUID=ExplicitVRLittleEndian;fm.MediaStorageSOPClassUID=MRImageStorage;fm.MediaStorageSOPInstanceUID=generate_uid()
    ds=FileDataset('',{},file_meta=fm,preamble=b'\0'*128)
    ds.Modality='MR';ds.StudyDate='20190320';ds.SeriesNumber=7;ds.InstanceNumber=1
    ds.SeriesDescription='COR PD FS';ds.SeriesInstanceUID=generate_uid();ds.StudyInstanceUID=generate_uid()
    ds.Rows=16;ds.Columns=16;ds.SamplesPerPixel=1;ds.PhotometricInterpretation='MONOCHROME2'
    ds.BitsAllocated=16;ds.BitsStored=16;ds.HighBit=15;ds.PixelRepresentation=0
    ds.PixelData=np.arange(256,dtype=np.uint16).tobytes();ds.RescaleSlope=2;ds.RescaleIntercept=-4
    ds.WindowCenter=150;ds.WindowWidth=300;ds.ImageOrientationPatient=[1,0,0,0,0,-1];ds.ImagePositionPatient=[0,12,0]
    ds.SliceThickness=4;ds.PatientName='SYNTHETIC^ONLY';ds.ImageLaterality='R'
    path=tmp_path/'scan.dcm';ds.save_as(path,enforce_file_format=True)
    frames=read_dicom(path);m=frames[0]['metadata']
    assert m['plane']=='coronal' and m['date']=='2019-03-20'
    assert frames[0]['pixels'][0,0]==-4
    assert 'PatientName' not in m
