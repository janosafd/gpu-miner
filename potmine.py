#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MinerPotatos 常驻挖矿 — 每台机一个管理进程
显卡常驻算(ptd)；每 POLL 秒读一次 miningStatus()，prevWork/target 一变立刻推新题，锚点每 8 秒刷新(窗口 250 块≈25 秒)；
命中先本地复算、确认题目没变，立即提交 mine(nonce, anchorBlock)+当前价格。
不预估 gas（省一次往返，gas 极便宜）；nonce/gas 价格后台预取；交易同时发给所有节点。"""
import os,sys,time,random,subprocess,threading,queue,glob
from web3 import Web3
from eth_abi import encode,decode
from eth_account import Account

RPCS=[u for u in os.environ.get("RH_RPC","https://rpc.mainnet.chain.robinhood.com").split(",") if u]
C=Web3.to_checksum_address("0xb0db77c5d6ed578189609ecc72d25699a79f785b")
BIN=os.environ.get("PTD_BIN","/root/ptd").split()
DRY=os.environ.get("DRYRUN")=="1"          # 测试模式：只算不提交
TEST_TARGET=os.environ.get("TEST_TARGET")  # 测试用的简单难度（hex）
POLL=float(os.environ.get("POLL","1"))
ws=[Web3(Web3.HTTPProvider(u,request_kwargs={"timeout":10})) for u in RPCS]
if DRY:
    acct=None; M=Web3.to_checksum_address(os.environ["MINER_ADDR"])
else:
    PK=os.environ.pop("PRIVATE_KEY","").strip()
    if not PK: sys.exit("!! 没有私钥：用 Mac 上的 run.sh 启动")
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
        rr[0]+=1; w=ws[rr[0]%len(ws)]   # 轮流用各节点，分摊请求量
        try:
            v=decode([ST],w.eth.call({"to":C,"data":S_STATUS}))[0]
            return dict(ablk=v[0],anchor=bytes(v[1]),prev=bytes(v[2]),target=v[3],price=v[5],supply=v[6],maxs=v[7],
                        window=v[11],blk=v[15],epoch=v[16],streak=v[18])
        except Exception as e: last=e
    raise last

def ngpu():
    if os.environ.get("NGPU"): return int(os.environ["NGPU"])
    try: n=len([l for l in subprocess.run(["nvidia-smi","-L"],capture_output=True,text=True).stdout.splitlines() if l.startswith("GPU")])
    except Exception: n=0
    return n or max(1,len(glob.glob("/dev/nvidia[0-9]*")))

N=ngpu(); procs=[]; hr={}; fq=queue.Queue(); salts=[os.urandom(24) for _ in range(N)]
def reader(g,p):
    for line in p.stdout:
        s=line.split()
        if not s: continue
        if s[0]=="FOUND": fq.put((int(s[1]),int(s[2]),g))
        elif s[0]=="HR": hr[g]=float(s[1])
        elif s[0]=="ERR": log(f"!! GPU{g}",line.strip())
    log(f"!! GPU{g} 算力进程退出")
for g in range(N):
    p=subprocess.Popen(BIN+[str(random.getrandbits(62))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                       text=True,bufsize=1,env=dict(os.environ,CUDA_VISIBLE_DEVICES=str(g)))
    procs.append(p); threading.Thread(target=reader,args=(g,p),daemon=True).start()

LIVE=[None]; jobs={}; cur=[0]
def push(s):
    cur[0]+=1; jid=cur[0]; jobs[jid]=s
    tgt=int(TEST_TARGET,16) if TEST_TARGET else s["target"]
    for g,p in enumerate(procs):
        try: p.stdin.write(f"J {jid} {M[2:]} {s['prev'].hex()} {s['anchor'].hex()} {tgt:064x} {salts[g].hex()}\n"); p.stdin.flush()
        except Exception: pass
    for k in [k for k in jobs if k<jid-100]: del jobs[k]

pre={"nonce":None,"gp":None,"bal":None}
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

def do_submit(nonce,s,g):
    try:
        L=LIVE[0]; price=L["price"]
        if pre["bal"] is not None and pre["bal"]<price: log(f"!! 余额 {pre['bal']/1e18:.5f} ETH 不够付铸造价 {price/1e18} ETH，请充值")
        tx={"to":C,"data":S_MINE+encode(["uint256","uint256"],[nonce,s["ablk"]]),"from":M,"nonce":pre["nonce"],
            "chainId":4663,"value":price,"gas":400000,"maxFeePerGas":max((pre["gp"] or 10**8)*3,10**8),"maxPriorityFeePerGas":0}
        pre["nonce"]+=1
        raw=acct.sign_transaction(tx).raw_transaction; hs=[]
        def send(w):
            try: hs.append(w.eth.send_raw_transaction(raw).hex())
            except Exception as e: log("[发送]",w.provider.endpoint_uri[:30],str(e)[:80])
        ts=[threading.Thread(target=send,args=(w,)) for w in ws]; [t.start() for t in ts]; [t.join(8) for t in ts]
        h=Web3.keccak(raw).hex(); log(f"[tx] {h} 付 {price/1e18} ETH")
        rc=ws[0].eth.wait_for_transaction_receipt(h,timeout=60,poll_latency=0.3)
        if rc.status==1: log(f"✅✅ 挖到土豆！块 {rc.blockNumber} ✅✅")
        else: log(f"❌ 上链失败：{reason(tx,rc.blockNumber)}  gasUsed {rc.gasUsed}")
    except Exception as e:
        log("[提交异常]",str(e)[:120]); pre["nonce"]=None

sent=set()
def submitter():
    while True:
        jid,cnt,g=fq.get(); s=jobs.get(jid)
        if not s: continue
        nonce=int.from_bytes(salts[g]+cnt.to_bytes(8,"big"),"big")
        h=int.from_bytes(Web3.keccak(MB+s["prev"]+s["anchor"]+nonce.to_bytes(32,"big")),"big")
        L=LIVE[0]; tgt=int(TEST_TARGET,16) if TEST_TARGET else L["target"]
        ok=h<=tgt
        fresh=s["prev"]==L["prev"] and L["blk"]-s["ablk"]<L["window"]-30
        log(f"[命中] GPU{g} 题{jid} 本地校验{'✅' if ok else '❌'} {'题目没变' if fresh else '题目已变/锚点太旧'} work={h:064x}")
        if DRY and ok:   # 测试：让合约自己算一遍对拍
            w=int.from_bytes(ws[0].eth.call({"to":C,"data":S_WORK+encode(["address","bytes32","bytes32","uint256"],[M,s["prev"],s["anchor"],nonce])}),"big")
            log(f"   合约 workFor 对拍 {'✅ 一致' if w==h else '❌ 不一致 '+hex(w)}")
        if not ok or not fresh or DRY: continue
        if (s["prev"],nonce) in sent: continue
        sent.add((s["prev"],nonce))
        if pre["nonce"] is None: log("!! 还没拿到钱包 nonce，跳过"); continue
        threading.Thread(target=do_submit,args=(nonce,s,g),daemon=True).start()

log(f"[矿工] {M}  显卡 {N} 张  {'【测试模式，不提交】' if DRY else ''}")
LIVE[0]=status(); L=LIVE[0]
log(f"[链] 块{L['blk']} 已挖 {L['supply']}/{L['maxs']} 第{L['epoch']}期 价格 {L['price']/1e18} ETH 余额 {ws[0].eth.get_balance(M)/1e18:.5f} ETH")
threading.Thread(target=prefetch,daemon=True).start(); threading.Thread(target=submitter,daemon=True).start()
push(L); last_push=time.time(); switches=0; last_log=time.time()
while True:
    try:
        s=status(); old=LIVE[0]; LIVE[0]=s
        if s["prev"]!=old["prev"] or s["target"]!=old["target"] or time.time()-last_push>8:
            switches+=s["prev"]!=old["prev"]; push(s); last_push=time.time()
        if s["supply"]>=s["maxs"]: log("挖完了，退出"); sys.exit(0)
    except Exception as e: log("[RPC]",str(e)[:80])
    dead=[g for g,p in enumerate(procs) if p.poll() is not None]
    if dead: log(f"!! GPU{dead} 算力进程挂了，退出"); sys.exit(1)
    if time.time()-last_log>=60:
        L=LIVE[0]; tot=sum(hr.values()); diff=2**256//(L["target"]+1)
        eta=f"{diff/(tot*1e9)/3600:.1f} 小时/个" if tot else "-"
        log(f"[状态] 算力 {tot:.2f} GH/s {[hr.get(g) for g in range(N)]} | 已挖 {L['supply']}/{L['maxs']} 第{L['epoch']}期 价格 {L['price']/1e18} | 难度 {diff:.2e} streak {L['streak']} | 本机预计 {eta} | 余额 {(pre['bal'] or 0)/1e18:.4f} | 换题 {switches}")
        last_log=time.time()
    time.sleep(POLL)
