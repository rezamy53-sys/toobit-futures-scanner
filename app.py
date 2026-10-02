import asyncio, math
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE="https://api.toobit.com"
app=FastAPI(title="Toobit Futures Scanner")
app.mount("/static",StaticFiles(directory="static"),name="static")
TIMEOUT=httpx.Timeout(20.0,connect=10.0)

@app.get("/")
async def home(): return FileResponse("static/index.html")

async def get_json(path,params=None):
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT,headers={"User-Agent":"PublicMarketScanner/1.0"}) as c:
            r=await c.get(BASE+path,params=params); r.raise_for_status(); return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502,f"Toobit HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502,f"ارتباط با Toobit برقرار نشد: {type(e).__name__}")

@app.get("/api/health")
async def health(): return {"ok":True,"toobit":await get_json("/api/v1/time")}

def get_symbols(info):
    rows=info.get("contracts",[]) if isinstance(info,dict) else []
    out=[]
    for x in rows:
        s=x.get("symbol") or x.get("contractCode") or x.get("s")
        q=x.get("quoteAsset") or x.get("quoteCoin")
        if s and (q=="USDT" or str(s).endswith("USDT")) and not x.get("inverse",False):
            out.append(s)
    return sorted(set(out))

@app.get("/api/contracts")
async def contracts():
    symbols=get_symbols(await get_json("/api/v1/exchangeInfo"))
    if not symbols: raise HTTPException(502,"فهرست قراردادهای USDT از پاسخ صرافی قابل استخراج نبود.")
    return {"count":len(symbols),"symbols":symbols}

def f(x):
    try:return float(x)
    except:return 0.0

def ema(a,n):
    if len(a)<n:return None
    v=sum(a[:n])/n;k=2/(n+1)
    for x in a[n:]:v=x*k+v*(1-k)
    return v

def rsi(a,n=14):
    if len(a)<=n:return None
    d=[a[i]-a[i-1] for i in range(1,len(a))]
    g=sum(max(x,0) for x in d[:n])/n;l=sum(max(-x,0) for x in d[:n])/n
    for x in d[n:]:
        g=(g*(n-1)+max(x,0))/n;l=(l*(n-1)+max(-x,0))/n
    return 100 if l==0 else 100-100/(1+g/l)

def analyze(raw):
    rows=[]
    for x in raw if isinstance(raw,list) else []:
        try:
            if isinstance(x,list) and len(x)>=6: rows.append([f(x[1]),f(x[2]),f(x[3]),f(x[4]),f(x[5])])
            elif isinstance(x,dict): rows.append([f(x.get("o")),f(x.get("h")),f(x.get("l")),f(x.get("c")),f(x.get("v"))])
        except: pass
    if len(rows)<60:return None
    o,h,l,c,v=map(list,zip(*rows)); price=c[-1]
    e20,e50=ema(c,20),ema(c,50); rv=rsi(c)
    # MACD histogram from EMA series
    macd=[]
    for i in range(len(c)):
        a,b=ema(c[:i+1],12),ema(c[:i+1],26)
        if a is not None and b is not None:macd.append(a-b)
    mh=None
    if len(macd)>=9:
        sig=ema(macd,9); mh=macd[-1]-sig if sig is not None else None
    mid=sum(c[-20:])/20; sd=math.sqrt(sum((x-mid)**2 for x in c[-20:])/20)
    stlo,sthi=min(l[-14:]),max(h[-14:])
    stoch=(c[-1]-stlo)/(sthi-stlo)*100 if sthi!=stlo else 50
    ten=(max(h[-9:])+min(l[-9:]))/2; kij=(max(h[-26:])+min(l[-26:]))/2
    obv=[0.0]
    for i in range(1,len(c)):
        obv.append(obv[-1]+(v[i] if c[i]>c[i-1] else -v[i] if c[i]<c[i-1] else 0))
    obvup=obv[-1]>obv[-6]
    ls=ss=0
    if e20 is not None and e50 is not None:
        ls+=e20>e50;ss+=e20<e50
    if mh is not None:ls+=mh>0;ss+=mh<0
    if rv is not None:ls+=50<rv<70;ss+=30<rv<50
    ls+=ten>kij;ss+=ten<kij;ls+=obvup;ss+=not obvup;ls+=price>mid;ss+=price<mid
    side="LONG" if ls>=5 and ls>ss else "SHORT" if ss>=5 and ss>ls else "WAIT"
    tr=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    atr=sum(tr[-14:])/min(14,len(tr)); entry=stop=None;targets=[]
    if side=="LONG":
        entry=price;stop=price-1.5*atr;targets=[entry+1.5*atr,entry+3*atr,entry+4.5*atr]
    elif side=="SHORT":
        entry=price;stop=price+1.5*atr;targets=[entry-1.5*atr,entry-3*atr,entry-4.5*atr]
    return {"signal":side,"price":price,"entry":entry,"stopLoss":stop,"targets":targets,
      "rsi":rv,"macdHistogram":mh,"ema20":e20,"ema50":e50,"stochasticK":stoch,
      "bollingerUpper":mid+2*sd,"bollingerLower":mid-2*sd,"ichimokuTenkan":ten,
      "ichimokuKijun":kij,"fib618":max(h[-50:])-(max(h[-50:])-min(l[-50:]))*.618,
      "obvTrend":"UP" if obvup else "DOWN","longScore":ls,"shortScore":ss}

@app.get("/api/scan")
async def scan(interval:str=Query("15m",pattern="^(15m|30m|1h)$"),offset:int=Query(0,ge=0),batch:int=Query(20,ge=1,le=30)):
    symbols=get_symbols(await get_json("/api/v1/exchangeInfo"))
    if not symbols:raise HTTPException(502,"فهرست نمادهای USDT پیدا نشد.")
    part=symbols[offset:offset+batch]; sem=asyncio.Semaphore(4)
    async def one(s):
        async with sem:
            try:
                raw=await get_json("/quote/v1/klines",{"symbol":s,"interval":interval,"limit":100})
                a=analyze(raw)
                return {"symbol":s,**a} if a else {"symbol":s,"signal":"INSUFFICIENT_DATA"}
            except Exception as e:return {"symbol":s,"signal":"ERROR","error":str(e)[:120]}
    results=await asyncio.gather(*(one(s) for s in part))
    nxt=offset+len(part)
    return {"interval":interval,"totalSymbols":len(symbols),"scanned":len(part),"nextOffset":nxt if nxt<len(symbols) else 0,"results":results}
