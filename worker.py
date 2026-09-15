#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""算力机（Salad 等）：不持有私钥。
从提交机 GET /job 拿题 → 显卡算(ptd) → 出解先本地复算 → POST /sol 交给提交机，记录往返耗时。
环境变量：JOB_URL（如 http://1.2.3.4:40123）、SUBMIT_TOKEN、POLL、NGPU"""
import os,sys,time,json,random,subprocess,threading,queue,glob,urllib.request
from eth_utils import keccak

JOB=os.environ["JOB_URL"].rstrip("/"); TOKEN=os.environ["SUBMIT_TOKEN"]
BIN=os.environ.get("PTD_BIN","/app/ptd").split(); POLL=float(os.environ.get("POLL","1"))
WID=(os.environ.get("SALAD_MACHINE_ID") or os.environ.get("HOSTNAME") or "w")[:12]
def log(*a): print(time.strftime("%H:%M:%S"),*a,flush=True)
def http(method,path,body=None,timeout=5):
    url=JOB+path+(f"?t={TOKEN}" if method=="GET" else "")
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body else None,headers={"Content-Type":"application/json"},method=method)
    t=time.time(); r=json.loads(urllib.request.urlopen(req,timeout=timeout).read()); return r,(time.time()-t)*1000

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
        if s[0]=="FOUND": fq.put((int(s[1]),int(s[2]),g,time.time()))
        elif s[0]=="HR": hr[g]=float(s[1])
        elif s[0]=="ERR": log(f"!! GPU{g}",line.strip())
    log(f"!! GPU{g} 算力进程退出")
for g in range(N):
    p=subprocess.Popen(BIN+[str(random.getrandbits(62))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                       text=True,bufsize=1,env=dict(os.environ,CUDA_VISIBLE_DEVICES=str(g)))
    procs.append(p); threading.Thread(target=reader,args=(g,p),daemon=True).start()

jobs={}; cur=[0]; rtts=[]; up={"n":0,"ok":0}
def push(j):
    cur[0]+=1; jid=cur[0]; jobs[jid]=j
    for g,p in enumerate(procs):
        try: p.stdin.write(f"J {jid} {j['miner'][2:]} {j['prev']} {j['anchor']} {j['target']} {salts[g].hex()}\n"); p.stdin.flush()
        except Exception: pass
    for k in [k for k in jobs if k<jid-100]: del jobs[k]

def uploader():
    while True:
        jid,cnt,g,tf=fq.get(); j=jobs.get(jid)
        if not j: continue
        nonce=int.from_bytes(salts[g]+cnt.to_bytes(8,"big"),"big")
        h=int.from_bytes(keccak(bytes.fromhex(j["miner"][2:])+bytes.fromhex(j["prev"])+bytes.fromhex(j["anchor"])+nonce.to_bytes(32,"big")),"big")
        if h>int(j["target"],16): log(f"!! GPU{g} 本地复算不达标，丢弃"); continue
        try:
            r,ms=http("POST","/sol",{"t":TOKEN,"prev":j["prev"],"anchor":j["anchor"],"ablk":j["ablk"],"nonce":str(nonce),"worker":WID})
            wait=(time.time()-tf)*1000; rtts.append(ms); up["n"]+=1; up["ok"]+=bool(r.get("tx") or r.get("workFor"))
            log(f"[上交] 出解→交完 {wait:.0f}ms（往返 {ms:.0f}ms，提交机处理 {r.get('server_ms')}ms）结果 {json.dumps(r,ensure_ascii=False)}")
        except Exception as e: log("[上交失败]",str(e)[:100])
threading.Thread(target=uploader,daemon=True).start()

log(f"[算力机 {WID}] 显卡 {N} 张，提交机 {JOB}")
last=None; last_push=0; last_log=time.time()
while True:
    try:
        j,ms=http("GET","/job")
        if j.get("stop"): log("提交机已达上限，停止出题"); time.sleep(30); continue
        key=(j["prev"],j["target"],j["ablk"]//80)   # 题目或难度变了立即换；锚点约 8 秒刷新一次
        if key!=last: push(j); last=key; last_push=time.time()
    except Exception as e: log("[取题失败]",str(e)[:80])
    dead=[g for g,p in enumerate(procs) if p.poll() is not None]
    if dead: log(f"!! GPU{dead} 算力进程挂了，退出"); sys.exit(1)
    if time.time()-last_log>=60:
        avg=sum(rtts[-50:])/len(rtts[-50:]) if rtts else 0
        log(f"[状态] 算力 {sum(hr.values()):.2f} GH/s {[hr.get(g) for g in range(N)]}｜上交 {up['n']} 个 有效 {up['ok']}｜平均往返 {avg:.0f}ms")
        last_log=time.time()
    time.sleep(POLL)
