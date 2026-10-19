#!/usr/bin/env python3
"""Run all six tools through the REAL MCP Inspector CLI. Store metadata-only evidence.
Requires npm/network on first run. Never substitutes mocks or treats skipped runs as passes.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,os,shutil,subprocess,tempfile
from io import BytesIO
from pathlib import Path
from PIL import Image

TOOLS={"list_studies","list_series","get_contact_sheet","get_slice","get_slices","compare"}

def parse_json(text:str):
    decoder=json.JSONDecoder()
    for i,c in enumerate(text):
        if c=='{':
            try:
                obj,_=decoder.raw_decode(text[i:])
                if isinstance(obj,dict):return obj
            except ValueError:pass
    raise RuntimeError('INSPECTOR_DID_NOT_RETURN_JSON')

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True)
    p.add_argument('--study',required=True);p.add_argument('--series',type=int,required=True)
    p.add_argument('--start',type=int,default=1);p.add_argument('--end',type=int,default=2)
    p.add_argument('--token-file',type=Path,required=True);p.add_argument('--out',type=Path,default=Path('evidence/inspector.json'))
    args=p.parse_args();results=[]
    if not shutil.which('npx'):raise SystemExit('NPX_NOT_INSTALLED: Inspector acceptance not run')
    token=args.token_file.read_text().strip()
    with tempfile.TemporaryDirectory(prefix='mri-inspector-') as temp:
        config=Path(temp)/'config.json'
        config.write_text(json.dumps({'mcpServers':{'mri':{'type':'streamable-http','url':args.url,
                                                          'headers':{'Authorization':'Bearer '+token}}}}));config.chmod(0o600)
        def run(method,tool=None,arguments=None):
            cmd=['npx','--yes','@modelcontextprotocol/inspector@v1-latest','--cli','--config',str(config),'--server','mri','--method',method]
            if tool:cmd+=['--tool-name',tool]
            for key,value in (arguments or {}).items():
                cmd+=['--tool-arg',key+'='+ (value if isinstance(value,str) else json.dumps(value,separators=(',',':')))]
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=180)
            if result.returncode:raise RuntimeError('INSPECTOR_FAILED: output suppressed to protect credentials and image data')
            obj=parse_json(result.stdout)
            if 'result' in obj:obj=obj['result']
            if obj.get('isError'):raise RuntimeError('MCP_TOOL_RETURNED_ERROR')
            return obj
        schema=run('tools/list');names={t['name'] for t in schema['tools']}
        if names!=TOOLS:raise RuntimeError('UNEXPECTED_TOOL_SURFACE')
        selectors={'study_id':args.study,'series_no':args.series}
        a={'study':args.study,'series':args.series,'index':args.start}
        b={'study':args.study,'series':args.series,'index':args.end}
        calls=[('list_studies',{},0),('list_series',{'study_id':args.study},0),
               ('get_contact_sheet',selectors,1),('get_slice',{**selectors,'index':args.start},1),
               ('get_slices',{**selectors,'start':args.start,'end':args.end},args.end-args.start+1),
               ('compare',{'a':a,'b':b},1)]
        for name,arguments,expected in calls:
            result=run('tools/call',name,arguments);content=result.get('content',[])
            pictures=[x for x in content if x.get('type')=='image']
            if len(pictures)!=expected:raise RuntimeError('IMAGECONTENT_COUNT_MISMATCH_'+name)
            evidence=[]
            for pic in pictures:
                assert pic['mimeType']=='image/png'
                data=base64.b64decode(pic['data'],validate=True)
                assert len(data)<=1_000_000 and data.startswith(b'\x89PNG\r\n\x1a\n')
                with Image.open(BytesIO(data)) as im:im.verify()
                evidence.append({'mimeType':'image/png','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            for block in content:
                if block['type']=='text':
                    text=block['text']
                    assert 'iVBOR' not in text and '"type":"image"' not in text.replace(' ','')
            results.append({'tool':name,'status':'PASS','content_types':[x['type'] for x in content],'images':evidence})
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps({'client':'official MCP Inspector CLI v1-latest','url':args.url,'results':results},ensure_ascii=False,indent=2))
    print('PASS: all six tools verified with MCP Inspector, including PNG ImageContent')
if __name__=='__main__':
    try:main()
    except Exception:
        raise SystemExit('INSPECTOR_ACCEPTANCE_FAILED: no success certificate created; check local client/server setup') from None
