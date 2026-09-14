#!/bin/bash
# Linly-Talker 权重与目录校验（**只读**，不移动、不删除、不创建任何文件）
#
# 用途：用户拉完代码与权重后，先跑这个脚本拿到「真实目录结构 + 体积 + 完整性」事实，
#       再据此决定如何归位。避免照抄文档里的路径猜名字。
set -uo pipefail

ROOT=${1:-/root/autodl-tmp/Linly-Talker}
PROJ=${2:-/root/autodl-tmp}

line() { printf '%s\n' "----------------------------------------------------------------"; }

echo "=================== 1. 代码仓库 ==================="
if [ -d "$ROOT/.git" ]; then
  echo "仓库存在：$ROOT"
  git -C "$ROOT" remote -v
  git -C "$ROOT" log --oneline -1 2>/dev/null
  echo "子模块状态："
  git -C "$ROOT" submodule status 2>/dev/null || echo "  （无 .gitmodules 或未初始化）"
else
  echo "!! $ROOT 不是 git 仓库或不存在 —— 代码尚未就位"
fi

line
echo "=================== 2. 项目根目录实际内容 ==================="
if [ -d "$ROOT" ]; then
  ls -la "$ROOT"
else
  echo "（目录不存在）"
fi

line
echo "=================== 3. requirements 文件实况 ==================="
if [ -d "$ROOT" ]; then
  # 部署方案要求：不要自行创建同名文件，以实际文件名为准
  ls -la "$ROOT"/requirements*.txt 2>/dev/null || echo "!! 未找到任何 requirements*.txt"
  [ -f "$ROOT/VITS/requirements.txt" ] && echo "存在 VITS/requirements.txt" || echo "!! 缺少 VITS/requirements.txt"
else
  echo "（目录不存在）"
fi

line
echo "=================== 4. 关键权重目录体积 ==================="
for d in checkpoints GPT_SoVITS Qwen MuseTalk Whisper FunASR; do
  for base in "$PROJ" "$ROOT"; do
    if [ -d "$base/$d" ]; then
      printf '%-12s %s\n' "$d" "$(du -sh "$base/$d" 2>/dev/null | cut -f1)  ($base/$d)"
    fi
  done
done
echo
echo "提示：若某目录在 $PROJ 和 $ROOT 下都存在，说明尚未归位，需要 mv。"

line
echo "=================== 5. mapping 文件完整性校验（关键） ==================="
# 依据：官方文档正文写 174MB，实际文件约 149MB。偏小会导致 SadTalker
#       启动报 invalid load key, 'v'。阈值取 140MB 作为下限。
FOUND=0
for f in $(find "$PROJ" -maxdepth 4 -name "mapping_00*.pth.tar" 2>/dev/null); do
  FOUND=1
  SIZE=$(stat -c %s "$f")
  MB=$((SIZE / 1024 / 1024))
  if [ "$MB" -ge 140 ]; then
    printf '  OK    %4s MB  %s\n' "$MB" "$f"
  else
    printf '  BAD   %4s MB  %s  <-- 疑似下载不完整，需重下\n' "$MB" "$f"
  fi
done
[ "$FOUND" -eq 0 ] && echo "!! 未找到任何 mapping_00*.pth.tar"

line
echo "=================== 6. 其他常见必需权重抽查 ==================="
for pattern in "sadtalker" "wav2lip" "GFPGAN" "gpt-sovits" "chinese-hubert" "s2G" "s1" "MuseTalk"; do
  hits=$(find "$PROJ" -maxdepth 5 -iname "*${pattern}*" 2>/dev/null | head -n 3)
  if [ -n "$hits" ]; then
    echo "[$pattern]"
    echo "$hits" | sed 's/^/    /'
  else
    echo "[$pattern] 未找到"
  fi
done

line
echo "=================== 7. 磁盘占用 ==================="
df -h "$PROJ" | tail -1
df -h / | tail -1
echo
echo "数据盘各顶层目录："
du -sh "$PROJ"/* 2>/dev/null | sort -rh | head -n 15

line
echo "校验结束（本脚本未修改任何文件）"
