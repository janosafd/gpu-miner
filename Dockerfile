# 第一步：编译 GPU 内核（cudart 静态链接，运行时只需要宿主机的 NVIDIA 驱动）
FROM nvidia/cuda:12.8.1-devel-ubuntu22.04 AS build
WORKDIR /src
COPY ptd.cu .
RUN nvcc -O3 -o ptd ptd.cu \
    -gencode arch=compute_86,code=sm_86 \
    -gencode arch=compute_89,code=sm_89 \
    -gencode arch=compute_120,code=sm_120 \
    -gencode arch=compute_75,code=compute_75

# 第二步：精简运行镜像（只有 python3 + web3 + 编译好的内核）
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PTD_BIN=/app/ptd \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-pip ca-certificates \
 && python3 -m pip install --no-cache-dir -q web3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /src/ptd /app/ptd
COPY potmine.py /app/
LABEL org.opencontainers.image.source=https://github.com/janosafd/gpu-miner
CMD ["python3", "-u", "/app/potmine.py"]
