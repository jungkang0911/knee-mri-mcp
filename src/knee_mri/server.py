from __future__ import annotations
import base64
import json
import logging
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent,TextContent,ToolAnnotations
from .auth import PrivateBoundary
from .common import SafeError
from .dataset import Dataset
from .imaging import png_bytes

# Explicit SDK content objects. Never json.dumps(base64), a URL-only image, or a text-wrapped content list.
def image_content(image)->ImageContent:
    return ImageContent(type="image",mimeType="image/png",data=base64.b64encode(png_bytes(image)).decode("ascii"))
def metadata_content(metadata)->TextContent:
    return TextContent(type="text",text=json.dumps(metadata,ensure_ascii=False,separators=(",",":")))

def create_app(dataset:Path,token:str,hosts:list[str],origins:list[str]):
    store=Dataset(dataset)
    mcp=FastMCP("Right Knee MRI — read-only",
                instructions="Retrieve only requested de-identified slices. Not a diagnostic system. Check metadata and orientation; no assumption of longitudinal worsening.",
                stateless_http=True,json_response=True,streamable_http_path="/mcp",
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                             allowed_hosts=hosts,allowed_origins=origins))
    annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    def invoke(fn):
        try:return fn()
        except SafeError as e:raise ValueError(str(e)) from None
        except Exception:raise ValueError("IMAGE_REQUEST_FAILED") from None
    @mcp.tool(annotations=annotations,structured_output=False)
    def list_studies()->str:
        """List configured examination dates and pre/post operation metadata, without patient identifiers."""
        return invoke(lambda:json.dumps(store.list_studies(),ensure_ascii=False))
    @mcp.tool(annotations=annotations,structured_output=False)
    def list_series(study_id:str)->str:
        """List series number, plane, weighting, fat suppression, count and slice thickness."""
        return invoke(lambda:json.dumps(store.list_series(study_id),ensure_ascii=False))
    @mcp.tool(annotations=annotations,structured_output=False)
    def get_contact_sheet(study_id:str,series_no:int)->list[ImageContent|TextContent]:
        """Return a PNG overview; index labels do not imply anatomical equivalence across examinations."""
        return invoke(lambda:[image_content(store.get_contact_sheet(study_id,series_no))])
    @mcp.tool(annotations=annotations,structured_output=False)
    def get_slice(study_id:str,series_no:int,index:int,crop:list[int]|None=None,zoom:float=1,
                  window:list[float]|None=None)->list[ImageContent|TextContent]:
        """Return PNG ImageContent and one-line metadata. Crop=[x0,y0,x1,y1), native pixels.
        Zoom 1..4 is nearest-neighbour display only. window=[level,width] is DICOM-only.
        No silent downsizing; requests above 1 MB PNG must use a smaller crop or zoom.
        """
        def run():
            image,meta=store.get_image(study_id,series_no,index,crop,zoom,window)
            return [image_content(image),metadata_content(meta)]
        return invoke(run)
    @mcp.tool(annotations=annotations,structured_output=False)
    def get_slices(study_id:str,series_no:int,start:int,end:int)->list[ImageContent|TextContent]:
        """Return 1..8 consecutive PNG images, inclusive bounds, with metadata after EACH image."""
        def run():
            result=[]
            for image,meta in store.get_slices(study_id,series_no,start,end):
                result.extend([image_content(image),metadata_content(meta)])
            return result
        return invoke(run)
    @mcp.tool(annotations=annotations,structured_output=False)
    def compare(a:dict,b:dict)->list[ImageContent|TextContent]:
        """Side-by-side dated PNG. Each selector: {study,series,index,crop?}. No image registration."""
        def run():
            image,meta=store.compare(a,b)
            return [image_content(image),metadata_content(meta)]
        return invoke(run)
    return PrivateBoundary(mcp.streamable_http_app(),token,hosts,origins)

def main():
    import uvicorn
    # Prevent SDK exception logs from echoing tool arguments, filesystem traces, or credentials.
    logging.getLogger("mcp").disabled=True
    logging.getLogger("uvicorn.access").disabled=True
    try:
        token_file=os.environ.get("MRI_TOKEN_FILE")
        token=Path(token_file).read_text().strip() if token_file else os.environ.get("MRI_TOKEN","")
        hosts=[s.strip().lower() for s in os.environ.get("MRI_ALLOWED_HOSTS","127.0.0.1:8000,localhost:8000").split(",") if s.strip()]
        origins=[s.strip() for s in os.environ.get("MRI_ALLOWED_ORIGINS","http://localhost:6274,http://127.0.0.1:6274").split(",") if s.strip()]
        logging.disable(logging.CRITICAL)
        app=create_app(Path(os.environ.get("MRI_DATASET","./dataset")),token,hosts,origins)
        uvicorn.run(app,host=os.environ.get("MRI_BIND","127.0.0.1"),port=int(os.environ.get("MRI_PORT","8000")),
                    access_log=False,proxy_headers=False,log_level="warning")
    except Exception:
        raise SystemExit("STARTUP_FAILED: check dataset release, dependency installation and private configuration") from None
if __name__=="__main__":main()
