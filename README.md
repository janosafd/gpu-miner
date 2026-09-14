# gpu-miner

Keccak proof-of-work GPU miner image (CUDA kernel + Python manager).

Image: `ghcr.io/janosafd/gpu-miner:latest` (built automatically by GitHub Actions on every push to `main`).

This repository contains **no keys or wallet data**. All secrets are supplied at runtime as environment variables:

| Variable | Meaning |
|---|---|
| `PRIVATE_KEY` | Wallet private key used to submit solutions (use a dedicated low-balance wallet) |
| `RH_RPC` | Comma-separated RPC URLs (default: public Robinhood Chain RPC) |
| `NGPU` | Number of GPUs in the container (default: auto-detect) |
| `DRYRUN` / `MINER_ADDR` | Test mode: hash but never submit |
