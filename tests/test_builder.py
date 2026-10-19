import json
from pathlib import Path
import numpy as np
from PIL import Image
import yaml
from knee_mri.build import build
from knee_mri.common import sha256
from knee_mri.dataset import release
import pytest
from knee_mri.common import SafeError

def test_builder_inventory_alias_and_raw_readonly(tmp_path,monkeypatch):
    raw=tmp_path/'raw';original=raw/'2019-03-20';alias=raw/'Anonymized_20251126'
    original.mkdir(parents=True);alias.mkdir()
    a=np.random.default_rng(12).integers(0,256,(128,128),dtype=np.uint8)
    Image.fromarray(a).save(original/'img-00007-00001.png')
    Image.fromarray(a).save(alias/'image-00000.png')
    Image.fromarray(np.roll(a,5,axis=0)).save(original/'img-00007-00002.png')
    before={p:sha256(p) for p in raw.rglob('*.png')}
    # Deterministic OCR stub only for this unit test; not evidence of a real OCR pass.
    monkeypatch.setattr('knee_mri.build.ocr_tokens',lambda im:[])
    monkeypatch.setattr('knee_mri.deid.ocr_tokens',lambda im:[])
    config={'studies':[{'study_id':'2019-03-20_TSGH','date':'2019-03-20','hospital':'TSGH','pre_or_post_op':'pre_op','path_regex':'2019-03-20','expected_series_counts':{7:2}}],
            'aliases':[{'path_regex':'Anonymized','canonical_study_id':'2019-03-20_TSGH'}]}
    path=tmp_path/'config.yaml';path.write_text(yaml.safe_dump(config))
    m=build(raw,tmp_path/'dataset',path,tmp_path/'private')
    assert m['statistics']['source_image_frames']==3
    assert m['statistics']['duplicates']==1
    assert all(sha256(p)==h for p,h in before.items())
    assert m['statistics']['errors']>0  # Missing technical metadata is not invented.
    assert len(m['studies'])==0  # No guessed/invalid metadata permitted in published slice records.
    with pytest.raises(SafeError):release(tmp_path/'dataset')
