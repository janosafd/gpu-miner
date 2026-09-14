// MinerPotatos 常驻 GPU 矿机（改自 Hashcats hcd.cu：keccak 内核不变，只改消息布局）
// 哈希: keccak256(miner20 ‖ prevWork32 ‖ anchor32 ‖ nonce32) <= target
// nonce = salt24 ‖ counter8(大端)，salt 每张卡随机，避免同一钱包多卡重复算
// 输入行: J <jobid> <miner40hex> <prev64hex> <anchor64hex> <target64hex> <salt48hex>
// 输出行: FOUND <jobid> <counter>   /   HR <GH/s>(每30秒)
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>
#include <chrono>
#include <sys/select.h>
#include <unistd.h>
#include <cuda_runtime.h>

__constant__ uint64_t RC[24] = {
0x0000000000000001ULL,0x0000000000008082ULL,0x800000000000808aULL,0x8000000080008000ULL,
0x000000000000808bULL,0x0000000080000001ULL,0x8000000080008081ULL,0x8000000000008009ULL,
0x000000000000008aULL,0x0000000000000088ULL,0x0000000080008009ULL,0x000000008000000aULL,
0x000000008000808bULL,0x800000000000008bULL,0x8000000000008089ULL,0x8000000000008003ULL,
0x8000000000008002ULL,0x8000000000000080ULL,0x000000000000800aULL,0x800000008000000aULL,
0x8000000080008081ULL,0x8000000000008080ULL,0x0000000080000001ULL,0x8000000080008008ULL};

__device__ __forceinline__ uint64_t rotl(uint64_t x,int n){return (x<<n)|(x>>(64-n));}

__device__ void keccakf(uint64_t st[25]){
  for(int r=0;r<24;r++){
    uint64_t bc[5],t;
    for(int i=0;i<5;i++) bc[i]=st[i]^st[i+5]^st[i+10]^st[i+15]^st[i+20];
    for(int i=0;i<5;i++){t=bc[(i+4)%5]^rotl(bc[(i+1)%5],1);
      for(int j=0;j<25;j+=5) st[j+i]^=t;}
    int piln[24]={10,7,11,17,18,3,5,16,8,21,24,4,15,23,19,13,12,2,20,14,22,9,6,1};
    int rr[24]={1,3,6,10,15,21,28,36,45,55,2,14,27,41,56,8,25,43,62,18,39,61,20,44};
    uint64_t tmp=st[1];
    for(int i=0;i<24;i++){int j=piln[i];bc[0]=st[j];st[j]=rotl(tmp,rr[i]);tmp=bc[0];}
    for(int j=0;j<25;j+=5){
      for(int i=0;i<5;i++) bc[i]=st[j+i];
      for(int i=0;i<5;i++) st[j+i]^=(~bc[(i+1)%5])&bc[(i+2)%5];}
    st[0]^=RC[r];
  }
}

__device__ void keccak256_116(const uint8_t* msg, uint8_t* out){
  uint64_t st[25]; for(int i=0;i<25;i++) st[i]=0;
  uint8_t block[136];
  for(int i=0;i<116;i++) block[i]=msg[i];
  block[116]=0x01; for(int i=117;i<136;i++) block[i]=0; block[135]|=0x80;
  for(int i=0;i<17;i++){uint64_t v=0;for(int b=0;b<8;b++) v|=((uint64_t)block[i*8+b])<<(8*b); st[i]^=v;}
  keccakf(st);
  for(int i=0;i<4;i++) for(int b=0;b<8;b++) out[i*8+b]=(uint8_t)(st[i]>>(8*b));
}

// 合约是 work <= target
__device__ bool le(const uint8_t* h,const uint8_t* t){
  for(int i=0;i<32;i++){ if(h[i]!=t[i]) return h[i]<t[i]; } return true;
}

__global__ void mine(const uint8_t* base_msg,const uint8_t* target,uint64_t start,uint64_t stride,
                     uint64_t n_per,unsigned long long* found_nonce,int* found_flag){
  uint64_t gid=blockIdx.x*(uint64_t)blockDim.x+threadIdx.x;
  uint8_t msg[116]; for(int i=0;i<116;i++) msg[i]=base_msg[i];
  for(uint64_t k=0;k<n_per;k++){
    if(*found_flag) return;
    uint64_t c=start+gid+k*stride;
    for(int b=0;b<8;b++) msg[108+b]=(uint8_t)(c>>(56-8*b));
    uint8_t h[32]; keccak256_116(msg,h);
    if(le(h,target)){
      if(atomicCAS(found_flag,0,1)==0){ *found_nonce=c; }
      return;
    }
  }
}

int hexval(char c){return (c<='9')?c-'0':(c|32)-'a'+10;}
void parsehex(const char*s,uint8_t*out,int n){ if(s[0]=='0'&&s[1]=='x')s+=2; for(int i=0;i<n;i++)out[i]=(hexval(s[2*i])<<4)|hexval(s[2*i+1]); }

int main(int argc,char**argv){
  uint64_t counter=(argc>1)?strtoull(argv[1],0,10):0;
  uint8_t msg[116]={0}, target[32]={0};
  long job=-1;
  uint8_t *d_msg,*d_tgt; unsigned long long*d_nonce; int*d_flag;
  cudaMalloc(&d_msg,116);cudaMalloc(&d_tgt,32);cudaMalloc(&d_nonce,8);cudaMalloc(&d_flag,4);
  int blocks=1024,threads=256; uint64_t stride=(uint64_t)blocks*threads, per=64;
  std::string buf; char rb[4096];
  auto t0=std::chrono::steady_clock::now(); unsigned long long cnt=0;
  while(true){
    fd_set fs; FD_ZERO(&fs); FD_SET(0,&fs); timeval tv={0,0};
    int r=select(1,&fs,NULL,NULL,(job<0)?NULL:&tv);
    if(r>0){
      ssize_t n=read(0,rb,sizeof rb);
      if(n<=0) return 0;  // 管理进程没了就退出
      buf.append(rb,n);
      size_t p;
      while((p=buf.find('\n'))!=std::string::npos){
        std::string line=buf.substr(0,p); buf.erase(0,p+1);
        char m[96],pv[96],an[96],tg[96],sl[96]; long id;
        if(sscanf(line.c_str(),"J %ld %90s %90s %90s %90s %90s",&id,m,pv,an,tg,sl)==6){
          parsehex(m,msg,20); parsehex(pv,msg+20,32); parsehex(an,msg+52,32);
          parsehex(sl,msg+84,24); parsehex(tg,target,32);
          cudaMemcpy(d_msg,msg,116,cudaMemcpyHostToDevice);
          cudaMemcpy(d_tgt,target,32,cudaMemcpyHostToDevice);
          job=id;
        }
      }
    }
    if(job<0) continue;
    int flag=0; unsigned long long fn=0;
    cudaMemset(d_flag,0,4);
    mine<<<blocks,threads>>>(d_msg,d_tgt,counter,stride,per,d_nonce,d_flag);
    cudaError_t e=cudaMemcpy(&flag,d_flag,4,cudaMemcpyDeviceToHost);
    if(e!=cudaSuccess){ printf("ERR %s\n",cudaGetErrorString(e)); fflush(stdout); return 2; }
    if(flag){ cudaMemcpy(&fn,d_nonce,8,cudaMemcpyDeviceToHost); printf("FOUND %ld %llu\n",job,fn); fflush(stdout); }
    counter+=stride*per; cnt+=stride*per;
    double sec=std::chrono::duration<double>(std::chrono::steady_clock::now()-t0).count();
    if(sec>=30){ printf("HR %.2f\n",cnt/sec/1e9); fflush(stdout); cnt=0; t0=std::chrono::steady_clock::now(); }
  }
}
