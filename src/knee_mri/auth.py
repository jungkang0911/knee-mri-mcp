from __future__ import annotations
import hashlib
import hmac
import json
from urllib.parse import urlsplit
from .common import SafeError

class PrivateBoundary:
    """ASGI bearer + Host/Origin + request-size boundary. Does not log credentials/body/path."""
    def __init__(self,app,token:str,allowed_hosts:list[str],allowed_origins:list[str],max_body:int=131072):
        if len(token)<32 or any(c.isspace() for c in token):raise SafeError("TOKEN_MUST_HAVE_AT_LEAST_32_NONSPACE_CHARACTERS")
        if not allowed_hosts or "*" in allowed_hosts or "*" in allowed_origins:raise SafeError("EXPLICIT_HOST_ALLOWLIST_REQUIRED")
        self.app=app;self.digest=hashlib.sha256(token.encode()).digest()
        self.hosts=set(allowed_hosts);self.origins=set(allowed_origins);self.max_body=max_body
    async def __call__(self,scope,receive,send):
        if scope["type"]!="http":return await self.app(scope,receive,send)
        headers={}
        for k,v in scope.get("headers",[]):headers.setdefault(k.lower(),[]).append(v)
        async def fail(status,code):
            body=json.dumps({"error":code}).encode()
            hs=[(b"content-type",b"application/json"),(b"cache-control",b"no-store")]
            if status==401:hs.append((b"www-authenticate",b'Bearer realm="knee-mri"'))
            await send({"type":"http.response.start","status":status,"headers":hs})
            await send({"type":"http.response.body","body":body})
        if len(headers.get(b"host",[]))!=1:return await fail(400,"INVALID_HOST")
        host=headers[b"host"][0].decode("latin-1").lower()
        if host not in self.hosts:return await fail(400,"INVALID_HOST")
        origin=headers.get(b"origin",[])
        if origin and (len(origin)!=1 or origin[0].decode("latin-1") not in self.origins):return await fail(403,"INVALID_ORIGIN")
        authorization=headers.get(b"authorization",[])
        valid=False
        if len(authorization)==1:
            parts=authorization[0].split(b" ",1)
            if len(parts)==2 and parts[0].lower()==b"bearer":
                valid=hmac.compare_digest(self.digest,hashlib.sha256(parts[1]).digest())
        if not valid:return await fail(401,"AUTHENTICATION_REQUIRED")
        # The header is the ONLY token channel. Query-string token fallback intentionally absent.
        if scope.get("query_string",b""):return await fail(400,"QUERY_STRING_NOT_ALLOWED")
        if scope.get("path") not in ("/mcp","/mcp/"):return await fail(404,"NOT_FOUND")
        # Bound authenticated request before any MCP JSON parsing, including chunked bodies.
        messages=[];total=0
        while True:
            msg=await receive()
            if msg["type"]=="http.disconnect":return
            total+=len(msg.get("body",b""))
            if total>self.max_body:return await fail(413,"REQUEST_TOO_LARGE")
            messages.append(msg)
            if not msg.get("more_body",False):break
        async def bounded_receive():
            if messages:return messages.pop(0)
            return await receive()
        started=False
        async def private_send(message):
            nonlocal started
            if message["type"]=="http.response.start":
                started=True
                message={**message,"headers":list(message.get("headers",[]))+[(b"cache-control",b"no-store"),(b"x-content-type-options",b"nosniff")]}
            await send(message)
        try:await self.app(scope,bounded_receive,private_send)
        except Exception:
            if not started:await fail(500,"INTERNAL_ERROR")
