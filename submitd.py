#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交机：唯一持有私钥的机器（放在 vast 上，有公网端口）。
- 每 0.5 秒读一次 miningStatus()，维护当前题目
- HTTP :PORT
    GET  /job?t=TOKEN   给算力机发题（钱包地址、prevWork、锚点、难度）
    POST /sol           收算力机的解 → 本地复算校验 → 题目没变就立刻签名、同时广播给所有节点
- 算力机只拿钱包地址不拿私钥；解被截获也没用（哈希里包含钱包地址）
环境变量：PRIVATE_KEY（或 DRYRUN=1 + MINER_ADDR）、SUBMIT_TOKEN、RH_RPC、PORT、MAX_MINTS（0=不限）、TEST_TARGET"""
import os,sys,time,json,threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from web3 import Web3
from eth_abi import encode,decode
from eth_account import Account

RPCS=[u for u in os.environ.get("RH_RPC","https://rpc.mainnet.chain.robinhood.com").split(",") if u]
C=Web3.to_checksum_address(os.environ.get("CONTRACT","0xb0db77c5d6ed578189609ecc72d25699a79f785b"))
TOKEN=os.environ["SUBMIT_TOKEN"]; PORT=int(os.environ.get("PORT","8080"))
DRY=os.environ.get("DRYRUN")=="1"; TEST_TARGET=os.environ.get("TEST_TARGET")
MAX_MINTS=int(os.environ.get("MAX_MINTS","0"))
ws=[Web3(Web3.HTTPProvider(u,request_kwargs={"timeout":10})) for u in RPCS]
if DRY:
    acct=None; M=Web3.to_checksum_address(os.environ["MINER_ADDR"])
else:
    PK=os.environ.pop("PRIVATE_KEY","").strip()
    if not PK: sys.exit("!! 没有私钥")
    acct=Account.from_key(PK); M=acct.address; del PK
MB=bytes.fromhex(M[2:])
S_STATUS=Web3.keccak(text="miningStatus()")[:4]
S_MINE=Web3.keccak(text="mine(uint256,uint256)")[:4]
S_WORK=Web3.keccak(text="workFor(address,bytes32,bytes32,uint256)")[:4]
ST="("+",".join(["uint256","bytes32","bytes32"]+["uint256"]*21)+")"
ERR={"0d1fb381":"AnchorExpired 锚点过期","e26384a8":"AnchorInFuture","173a2971":"BadSolution 题目已变/被抢",
     "c365ff15":"BlockFull 这个块已有人挖到","6f312cbd":"NotStarted","52df9fe5":"SoldOut 挖完了","949ce241":"Underpaid 付款不足"}

def log(*a): print(time.strftime("%H:%M:%S"),*a,flush=True)
rr=[0]
def status():
    last=None
    for _ in range(len(ws)):
        rr[0]+=1; w=ws[rr[0]%len(ws)]
        try:
            v=decode([ST],w.eth.call({"to":C,"data":S_STATUS}))[0]
            return dict(ablk=v[0],anchor=bytes(v[1]),prev=bytes(v[2]),target=v[3],price=v[5],supply=v[6],maxs=v[7],
                        window=v[11],blk=v[15],epoch=v[16],streak=v[18])
        except Exception as e: last=e
    raise last

LIVE=[None]; pre={"nonce":None,"gp":None,"bal":None}
subs=[0]; mints=[0]; sent=set(); stats={"sol":0,"bad":0,"stale":0,"tx":0,"ok":0,"fail":0}; lock=threading.Lock()
VER=[0]; cond=threading.Condition()   # 题目版本号：一变就唤醒所有挂着等题的算力机

def jobkey(L): return (L["prev"],L["target"],L["ablk"]//80)   # 锚点约 8 秒刷新一次

def poller():
    while True:
        try:
            s=status(); old=LIVE[0]; LIVE[0]=s
            if old is None or jobkey(s)!=jobkey(old):
                with cond: VER[0]+=1; cond.notify_all()
        except Exception as e: log("[RPC]",str(e)[:80])
        time.sleep(0.3)

def prefetch():
    while True:
        try:
            if not DRY: pre["nonce"]=max(pre["nonce"] or 0,ws[0].eth.get_transaction_count(M,"pending"))
            pre["gp"]=ws[0].eth.gas_price; pre["bal"]=ws[0].eth.get_balance(M)
        except Exception as e: log("[预取]",str(e)[:80])
        time.sleep(3)

def reason(tx,blk):
    try: ws[0].eth.call({k:tx[k] for k in ("from","to","data","value")},blk); return "重放能过(可能是同块被抢)"
    except Exception as e:
        s=str(e)
        for k,v in ERR.items():
            if k in s: return v
        return s[:90]

def receipt(h,tx,t_recv,t_sent):
    try:
        rc=None
        while rc is None and time.time()-t_sent<60:
            try: rc=ws[0].eth.get_transaction_receipt(h)
            except Exception: time.sleep(0.2)
        t_seen=time.time()
        if rc is None: log(f"❌ 60 秒没上链 {h}"); return
        if rc.status==1:
            mints[0]+=1; stats["ok"]+=1
            log(f"✅✅ 挖到！块 {rc.blockNumber}｜收到解→广播 {(t_sent-t_recv)*1000:.0f}ms｜广播→查到回执 {(t_seen-t_sent)*1000:.0f}ms｜gas {rc.gasUsed}")
        else:
            stats["fail"]+=1
            with lock: subs[0]-=1
            log(f"❌ 上链失败：{reason(tx,rc.blockNumber)}  gasUsed {rc.gasUsed}")
    except Exception as e: log("[回执]",str(e)[:100])

def submit(nonce,ablk,t_recv,wk):
    L=LIVE[0]; price=L["price"]
    if pre["bal"] is not None and pre["bal"]<price: log(f"!! 余额 {pre['bal']/1e18:.5f} ETH 不够付 {price/1e18} ETH")
    tx={"to":C,"data":S_MINE+encode(["uint256","uint256"],[nonce,ablk]),"from":M,"nonce":pre["nonce"],
        "chainId":4663,"value":price,"gas":400000,"maxFeePerGas":max((pre["gp"] or 10**8)*3,10**8),"maxPriorityFeePerGas":0}
    pre["nonce"]+=1
    raw=acct.sign_transaction(tx).raw_transaction
    def send(w):
        try: w.eth.send_raw_transaction(raw)
        except Exception as e:
            if "known" not in str(e).lower(): log("[发送]",w.provider.endpoint_uri[:30],str(e)[:80])
    ts=[threading.Thread(target=send,args=(w,)) for w in ws]; [t.start() for t in ts]; [t.join(3) for t in ts]
    t_sent=time.time(); h=Web3.keccak(raw).hex(); stats["tx"]+=1
    log(f"[tx] {h} 付 {price/1e18} ETH｜来自 {wk}｜收到→广播 {(t_sent-t_recv)*1000:.0f}ms")
    threading.Thread(target=receipt,args=(h,tx,t_recv,t_sent),daemon=True).start()
    return h

class Hd(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"   # 保持连接，算力机每次交解省掉一次建连的往返
    def log_message(self,*a): pass
    def _send(self,code,obj):
        b=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u=urlparse(self.path); q=parse_qs(u.query)
        if q.get("t",[""])[0]!=TOKEN: return self._send(403,{"err":"token"})
        if u.path=="/job":
            # 长轮询：带 v=上次版本号就挂着等，题目一变立刻返回（最多等 10 秒）
            v=int(q.get("v",["-1"])[0])
            with cond:
                if v==VER[0]: cond.wait_for(lambda: VER[0]!=v, timeout=10)
                ver=VER[0]
            L=LIVE[0]; tgt=int(TEST_TARGET,16) if TEST_TARGET else L["target"]
            return self._send(200,{"v":ver,"miner":M,"prev":L["prev"].hex(),"anchor":L["anchor"].hex(),"ablk":L["ablk"],
                                   "target":f"{tgt:064x}","blk":L["blk"],"stop":bool(MAX_MINTS and mints[0]>=MAX_MINTS)})
        if u.path=="/ping": return self._send(200,{"t":time.time()})
        self._send(404,{})
    def do_POST(self):
        t_recv=time.time()
        try: d=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
        except Exception: return self._send(400,{"err":"json"})
        if d.get("t")!=TOKEN: return self._send(403,{"err":"token"})
        prev=bytes.fromhex(d["prev"]); anchor=bytes.fromhex(d["anchor"]); nonce=int(d["nonce"]); ablk=int(d["ablk"]); wk=str(d.get("worker","?"))[:16]
        h=int.from_bytes(Web3.keccak(MB+prev+anchor+nonce.to_bytes(32,"big")),"big")
        L=LIVE[0]; tgt=int(TEST_TARGET,16) if TEST_TARGET else L["target"]
        ok=h<=tgt; fresh=prev==L["prev"] and L["blk"]-ablk<L["window"]-20
        res={"ok":ok,"fresh":fresh}; stats["sol"]+=1
        if not ok: stats["bad"]+=1
        elif not fresh: stats["stale"]+=1
        elif DRY:
            wf=int.from_bytes(ws[0].eth.call({"to":C,"data":S_WORK+encode(["address","bytes32","bytes32","uint256"],[M,prev,anchor,nonce])}),"big")
            res["workFor"]=(wf==h)
        else:
            with lock:
                if (prev,nonce) in sent: res["dup"]=True
                elif MAX_MINTS and subs[0]>=MAX_MINTS: res["skip"]="已达上限"
                elif pre["nonce"] is None: res["skip"]="还没拿到钱包nonce"
                else: sent.add((prev,nonce)); subs[0]+=1; go=True
            if res.get("dup") is None and res.get("skip") is None: res["tx"]=submit(nonce,ablk,t_recv,wk)
        res["server_ms"]=round((time.time()-t_recv)*1000)
        log(f"[收解] {wk} 校验{'✅' if ok else '❌'} {'题目没变' if fresh else '题目已变/锚点旧'} 服务端处理 {res['server_ms']}ms"
            + (f" 合约对拍{'✅' if res.get('workFor') else '❌'}" if 'workFor' in res else "") + (f" {res.get('skip','')}" if res.get('skip') else ""))
        self._send(200,res)

log(f"[提交机] 钱包 {M} {'【测试模式，不提交】' if DRY else ''} 端口 {PORT} 上限 {MAX_MINTS or '不限'} 个")
# 节点体检：连不上/被拒的节点直接踢掉（实测 Robinhood 公共节点对 vast 机房 IP 返回 403）
good=[]
for w,u in zip(ws,RPCS):
    try:
        t=time.time(); w.eth.call({"to":C,"data":S_STATUS}); good.append(w)
        log(f"[节点] ✅ {u[:40]} {(time.time()-t)*1000:.0f}ms")
    except Exception as e: log(f"[节点] ❌ {u[:40]} 不能用，踢掉：{str(e)[:60]}")
if not good: sys.exit("!! 所有节点都不能用，不开挖")
ws=good
LIVE[0]=status(); L=LIVE[0]
log(f"[链] 块{L['blk']} 已挖 {L['supply']}/{L['maxs']} 第{L['epoch']}期 价格 {L['price']/1e18} ETH 余额 {ws[0].eth.get_balance(M)/1e18:.5f} ETH")
for f in (poller,prefetch): threading.Thread(target=f,daemon=True).start()
def reporter():
    while True:
        time.sleep(60); L=LIVE[0]
        log(f"[状态] 收解 {stats['sol']}（错{stats['bad']} 旧{stats['stale']}）发交易 {stats['tx']} 成功 {stats['ok']} 失败 {stats['fail']}｜已挖 {L['supply']} 价格 {L['price']/1e18}｜余额 {(pre['bal'] or 0)/1e18:.5f}")
threading.Thread(target=reporter,daemon=True).start()
ThreadingHTTPServer.daemon_threads=True
ThreadingHTTPServer(("0.0.0.0",PORT),Hd).serve_forever()
