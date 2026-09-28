import base64,json,os,re
from pathlib import Path
from urllib.parse import urljoin
import requests

class ProviderError(Exception): pass

class WatchHentai:
    def __init__(self):
        self.base=os.getenv("WATCHHENTAI_BASE_URL","https://watchhentai.net").rstrip("/")
        self.ua="Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/140.0.0.0 Mobile Safari/537.36"

    def _headers(self,referer=None):
        return {"User-Agent":self.ua,"Referer":referer or self.base+"/","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    def _get(self,url,referer=None):
        r=requests.get(url,headers=self._headers(referer),timeout=30)
        if not r.ok: raise ProviderError(f"HTTP {r.status_code} for {url}")
        return r.text

    def _absolute(self,url): return urljoin(self.base+"/",url)

    @staticmethod
    def _clean(v):
        return re.sub(r"\s+"," ",re.sub(r"<[^>]+>","",v)).strip() if v else None

    @staticmethod
    def _decode(v):
        v=v.replace("-","+").replace("_","/"); v+="="*(-len(v)%4)
        x=base64.b64decode(v)
        x=bytes(b^((13+(i%17))&255) for i,b in enumerate(x))[::-1]
        return base64.b64decode(x).decode()

    def _sources(self,html):
        m=re.search(r"var\s+whJwSources\s*=\s*(\[[\s\S]*?\]);",html)
        if not m:return []
        data=json.loads(m.group(1)); out=[]
        for s in data:
            try: out.append({"label":s.get("label","Unknown"),"type":s.get("type","video/mp4"),"url":self._decode(s["file"])})
            except Exception: pass
        return out

    def _navigation(self,html):
        out={"previous":None,"next":None,"series":None}
        for key,pat in {
            "previous":r"class=['"]item item-prev['"][\s\S]*?<a[^>]+href=["']([^"']+)",
            "series":r"class=['"]item item-all['"][\s\S]*?<a[^>]+href=["']([^"']+)",
            "next":r"class=['"]item item-next['"][\s\S]*?<a[^>]+href=["']([^"']+)"
        }.items():
            m=re.search(pat,html,re.I)
            if m and m.group(1)!="#" and not (key=="next" and "nonex" in m.group(0).lower()):
                out[key]=self._absolute(m.group(1))
        return out

    def get_episode(self,page_url,resolve_sources=True):
        if not re.search(r"watchhentai\.net/videos/",page_url,re.I): raise ProviderError("Not a WatchHentai episode URL")
        html=self._get(page_url)
        m=re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',html,re.I)
        title=self._clean(m.group(1)) if m else None
        if not title:
            m=re.search(r"<title[^>]*>([\s\S]*?)</title>",html,re.I); title=self._clean(m.group(1)) if m else page_url
        m=re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',html,re.I); thumb=m.group(1) if m else None
        m=re.search(r'data-primary-player-url=["\']([^"\']+)',html,re.I)
        if not m: raise ProviderError("Primary player URL not found")
        player=self._absolute(m.group(1))
        sources=self._sources(self._get(player,page_url)) if resolve_sources else []
        em=re.search(r"episode[-\s]+(\d+)",title+" "+page_url,re.I)
        return {"provider":"watchhentai","title":title,"episode":int(em.group(1)) if em else None,"thumbnail":self._absolute(thumb) if thumb else None,"page_url":page_url,"player_url":player,"navigation":self._navigation(html),"sources":sources}

    def latest(self,page=1):
        html=self._get(self.base+"/" if page<=1 else f"{self.base}/page/{page}/")
        seen=set(); out=[]
        for m in re.finditer(r'href=["\']([^"\']*/videos/[^"\']+)["\']',html,re.I):
            u=self._absolute(m.group(1)).split("#")[0]
            if u.rstrip("/")==self.base+"/videos" or u in seen: continue
            seen.add(u); out.append({"provider":"watchhentai","page_url":u})
        return out

    def download(self,url,output):
        output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
        with requests.get(url,headers=self._headers(self.base+"/"),stream=True,timeout=(30,120)) as r:
            if not r.ok: raise ProviderError(f"Media HTTP {r.status_code}")
            if not r.headers.get("content-type","").startswith("video/"): raise ProviderError("Media response is not video")
            with output.open("wb") as f:
                for chunk in r.iter_content(1024*1024):
                    if chunk:f.write(chunk)
