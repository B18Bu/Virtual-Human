#!/bin/bash
# 用 curl 把 PyTorch 全家桶 wheel 抓到本地，供 pip 离线安装。
#
# 背景（2026-09-13 实测定位）：Aliyun CDN 对 HTTP/1.1 单连接限速到约 400 KB/s，
# 而 HTTP/2 同 URL 可达 14~16 MB/s。pip 的 urllib3 只支持 HTTP/1.1，因此无论换
# 官方源还是国内镜像，pip 直连都卡在 0.2~0.6 MB/s（实测：官方 196 KB/s、Aliyun 469 KB/s）。
# 用 curl --http2 抓取后本地安装即可绕开，全程可控且可校验。
#
# 只抓「大件」：torch 三件套 + 11 个 nvidia CUDA 运行时 + triton。
# 纯 Python 小依赖（filelock/sympy/networkx/jinja2/fsspec/numpy/pillow 等）留给
# pip 从 PyPI 解析，体积合计约 20MB，走 HTTP/1.1 也在可接受范围内。
set -uo pipefail

PYTORCH_MIRROR="https://mirrors.aliyun.com/pytorch-wheels/cu121"
PYPI_MIRROR="https://mirrors.aliyun.com/pypi/simple"
WHEELDIR=/root/autodl-tmp/wheels
JOBS=4

mkdir -p "$WHEELDIR"
cd "$WHEELDIR" || exit 1

# torch 2.4.1+cu121 在 linux/x86_64/cp310 下的完整 CUDA 依赖闭包
WHEELS=(
  "torch-2.4.1+cu121-cp310-cp310-linux_x86_64.whl"
  "torchvision-0.19.1+cu121-cp310-cp310-linux_x86_64.whl"
  "torchaudio-2.4.1+cu121-cp310-cp310-linux_x86_64.whl"
  "nvidia_cuda_nvrtc_cu12-12.1.105-py3-none-manylinux1_x86_64.whl"
  "nvidia_cuda_runtime_cu12-12.1.105-py3-none-manylinux1_x86_64.whl"
  "nvidia_cuda_cupti_cu12-12.1.105-py3-none-manylinux1_x86_64.whl"
  "nvidia_cudnn_cu12-9.1.0.70-py3-none-manylinux2014_x86_64.whl"
  "nvidia_cublas_cu12-12.1.3.1-py3-none-manylinux1_x86_64.whl"
  "nvidia_cufft_cu12-11.0.2.54-py3-none-manylinux1_x86_64.whl"
  "nvidia_curand_cu12-10.3.2.106-py3-none-manylinux1_x86_64.whl"
  "nvidia_cusolver_cu12-11.4.5.107-py3-none-manylinux1_x86_64.whl"
  "nvidia_cusparse_cu12-12.1.0.106-py3-none-manylinux1_x86_64.whl"
  "nvidia_nccl_cu12-2.20.5-py3-none-manylinux2014_x86_64.whl"
  "nvidia_nvtx_cu12-12.1.105-py3-none-manylinux1_x86_64.whl"
)

echo "=== 1/3 从 Aliyun pytorch 镜像抓取 cu121 大件（并发 $JOBS，HTTP/2） ==="
for w in "${WHEELS[@]}"; do
  # URL 里的 '+' 需转义成 %2B
  enc=$(printf '%s' "$w" | sed 's/+/%2B/g')
  printf '%s\n' "$PYTORCH_MIRROR/$enc"
done | xargs -P "$JOBS" -I{} curl -sSL --http2 --retry 3 --retry-delay 2 -O {}

echo
echo "=== 2/3 从 PyPI 镜像解析并抓取 triton==3.0.0 ==="
# simple 页里的 href 是 ../../packages/xx/yy/<hash>/<file>#sha256=... 相对路径，
# 直接拼 /simple/triton/<file> 会 404（已实测）。这里解析出真实路径并顺带校验哈希。
TRITON_HREF=$(curl -s --max-time 20 "$PYPI_MIRROR/triton/" \
  | grep -o 'href="[^"]*triton-3\.0\.0-1-cp310-cp310-manylinux[^"]*\.whl[^"]*"' | head -1 \
  | sed 's/^href="//; s/"$//')
if [ -n "$TRITON_HREF" ]; then
  TRITON_SHA=$(printf '%s' "$TRITON_HREF" | sed -n 's/.*#sha256=//p')
  TRITON_URL=$(printf '%s' "$TRITON_HREF" | sed 's/#.*//' | sed 's|^\.\./\.\./|https://mirrors.aliyun.com/pypi/|')
  TRITON_FILE=$(basename "$TRITON_URL")
  echo ">> 命中 $TRITON_FILE"
  curl -sSL --http2 --retry 3 -o "$TRITON_FILE" "$TRITON_URL"
  if [ -n "$TRITON_SHA" ]; then
    echo "$TRITON_SHA  $TRITON_FILE" | sha256sum -c - \
      && echo ">> sha256 校验通过（对照 PyPI 公布值）" \
      || { echo "!! triton sha256 不匹配"; bad_triton=1; }
  fi
else
  echo "!! 未能在 PyPI 镜像解析到 triton 3.0.0 的 cp310 wheel，留给 pip 自行解析"
fi

echo
echo "=== 3/3 完整性校验（zip 结构 + 体积） ==="
bad=0
for f in *.whl; do
  sz=$(stat -c%s "$f")
  # 不用体积做判据：nvidia_nvtx_cu12 本身就只有 99KB（官方与镜像同尺寸），
  # 按体积卡会误报。只做「是否合法 wheel zip」的结构校验。
  if python -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); z.testzip(); assert any(n.endswith('METADATA') for n in z.namelist())" "$f" 2>/dev/null; then
    echo "  ✅ $(printf '%-8s' "$(( sz/1024/1024 ))MB") $f"
  else
    echo "  ❌ $f 不是合法 wheel（大小 $sz 字节）"
    bad=$((bad+1))
  fi
done

echo
echo "总计 $(( $(du -sm . | cut -f1) )) MB，异常 $bad 个"
[ "$bad" -eq 0 ] && echo "FETCH_WHEELS_DONE" || echo "FETCH_WHEELS_HAS_ERRORS"
