#!/bin/bash
# Linly-Talker 模型归位：把 ModelScope 快照缓存里的权重搬到仓库期望的路径
#
# 依据官方 scripts/download_models.sh 的映射表，但做了三处强化：
#   1) 归位前先做**冲突预检**，任何一个目标已存在就整体中止，绝不覆盖
#   2) 用 mv（同文件系统内是 rename），不额外占空间、不产生副本
#   3) 跳过 ttsfrd 的 unzip + pip install —— 方案明确规定不装，且该 wheel 是
#      cp38 专用，而 linly 环境是 Python 3.10，本就装不上（目录照官方搬过去）
#
# 幂等性：不幂等。已归位过再跑会因冲突预检直接中止（这是故意的）。
#
# 用法：bash /root/autodl-tmp/deploy/place_models.sh [--dry-run]
set -uo pipefail

SRC=${SRC:-/root/autodl-tmp/models/Kedreamix--Linly-Talker/snapshots/master}
DST=${DST:-/root/autodl-tmp/Linly-Talker}
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

die() { printf '!! %s\n' "$*" >&2; exit 1; }
[ -d "$SRC" ] || die "快照源目录不存在：$SRC"
[ -d "$DST/.git" ] || die "目标不是 Linly-Talker 仓库：$DST"

MOVED=0
CONFLICTS=()

# 映射表，格式 "类型|源|目标|标签"
#   merge = 把「源目录的内容」逐个搬进已存在的「目标目录」（用于需要与仓库原有文件合并的目录）
#   into  = 把「源路径本身」搬进「目标父目录」（用于整目录落位）
MAPPINGS=(
  "into|$SRC/checkpoints/CosyVoice_ckpt/CosyVoice-ttsfrd|$DST/CosyVoice/pretrained_models|CosyVoice/pretrained_models/CosyVoice-ttsfrd"
  "merge|$SRC/checkpoints|$DST/checkpoints|checkpoints"
  "merge|$SRC/GPT_SoVITS/pretrained_models|$DST/GPT_SoVITS/pretrained_models|GPT_SoVITS/pretrained_models"
  "merge|$SRC/MuseTalk|$DST/Musetalk/models|Musetalk/models"
  "into|$SRC/gfpgan|$DST|gfpgan"
  "into|$SRC/Qwen|$DST|Qwen"
  "into|$SRC/Whisper|$DST|Whisper"
  "into|$SRC/FunASR|$DST|FunASR"
)

# 某个「源条目 -> 目标路径」是否算冲突（目标同名空目录允许被顶替）
is_conflict() {
  local entry="$1" target="$2"
  [ -e "$target" ] || return 1
  if [ -d "$entry" ] && [ -d "$target" ] && [ -z "$(ls -A "$target" 2>/dev/null)" ]; then
    return 1
  fi
  return 0
}

# 列出该映射会产生的 (源条目, 目标路径) 对
pairs() {
  local kind="$1" src="$2" dst="$3"
  if [ "$kind" = "merge" ]; then
    local e
    for e in "$src"/*; do
      [ -e "$e" ] || continue
      printf '%s\t%s\n' "$e" "$dst/$(basename "$e")"
    done
  else
    printf '%s\t%s\n' "$src" "$dst/$(basename "$src")"
  fi
}

echo "快照源：$SRC"
echo "目标：  $DST"
[ "$DRY" = 1 ] && echo "*** DRY-RUN 模式，不会改动任何文件 ***"
echo

echo "=== 阶段一：冲突预检（只读，不建目录不移动）==="
for m in "${MAPPINGS[@]}"; do
  IFS='|' read -r kind s d label <<<"$m"
  [ -e "$s" ] || die "源不存在：$s"
  while IFS=$'\t' read -r entry target; do
    [ -n "$entry" ] || continue
    if is_conflict "$entry" "$target"; then
      CONFLICTS+=("$label: $(basename "$entry") 已存在于 $(dirname "$target")")
    fi
  done < <(pairs "$kind" "$s" "$d")
done

if [ ${#CONFLICTS[@]} -gt 0 ]; then
  echo "!! 检测到 ${#CONFLICTS[@]} 处冲突，整体中止（未改动任何文件）："
  printf '   - %s\n' "${CONFLICTS[@]}"
  exit 1
fi
echo "无冲突。"
echo

echo "=== 阶段二：执行归位 ==="
for m in "${MAPPINGS[@]}"; do
  IFS='|' read -r kind s d label <<<"$m"
  # dry-run 必须完全不落盘，连空目录都不建
  [ "$DRY" = 1 ] || mkdir -p "$d" || die "无法创建 $d"
  while IFS=$'\t' read -r entry target; do
    [ -n "$entry" ] || continue
    if [ "$DRY" = 1 ]; then
      printf '  [dry-run] %s  ->  %s\n' "$entry" "$target"
    else
      mv "$entry" "$target" || die "mv 失败：$entry -> $target"
      printf '  ✓ %s\n' "$target"
    fi
    MOVED=$((MOVED+1))
  done < <(pairs "$kind" "$s" "$d")
done

echo
echo "共搬运 $MOVED 个条目。"
echo
echo "=== 归位结果 ==="
for d in checkpoints gfpgan Qwen Whisper FunASR Musetalk/models GPT_SoVITS/pretrained_models CosyVoice/pretrained_models/CosyVoice-ttsfrd; do
  printf '  %-45s %s\n' "$d" "$(du -sh "$DST/$d" 2>/dev/null | cut -f1)"
done
echo
echo "=== 留在快照缓存里的（本就无需归位）==="
ls -A "$SRC" 2>/dev/null | sed 's/^/  /'
echo
echo "=== 快照缓存中已搬空的目录 ==="
find "$SRC" -type d -empty 2>/dev/null | sed "s|$SRC|  <cache>|"
