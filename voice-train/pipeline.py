#!/usr/bin/env python3
"""音色训练流水线：从一段原始音频，跑出一套可被 Linly-Talker 加载的 GPT-SoVITS 权重。

## 为什么这个文件在仓库外

Linly-Talker 仓库自带的 GPT-SoVITS 是上游的**裁剪版**，`tools/` 包被整个摘掉了
（历史里从未存在），所以 `s2_train.py` 连 import 都过不去。但我们**不能**改用上游
仓库训练：

    Linly-Talker 的 s2G488k.pth 的 config 里**没有 version 键**，text/ 模块也没有
    v1/v2 符号表切换 —— 这是 **pre-v2 的老快照**。而上游现主分支默认 v2Pro/v2，
    音素符号表与 config 形状都不同，训出来的模型喂不进这个老推理封装。

所以正确做法是：**用本仓库自己的 train 脚本 + 自己的 configs + 自己的预训练权重**，
只把缺的几块补在仓库外（本目录），通过 PYTHONPATH 挂进去。仓库一个文件都不动。

## 补了什么

    tools/i18n/i18n.py    process_ckpt.py 需要的 I18nAuto（恒等替身，见该文件注释）
    slicer2.py            从上游移植的切片算法（纯 numpy）
    pipeline.py           本文件：把上游 WebUI 里「点六下」的流程串成一条命令

## 数据流

    原始音频
      └─[1] 切片        → slicer_opt/*.wav
          └─[2] ASR     → asr.list   (wav路径|说话人|语言|文本)
              └─[3] 1-get-text        → 2-name2text-0.txt + 3-bert/
                  └─[4] 合并          → 2-name2text.txt
                      └─[5] 2-get-hubert → 4-cnhubert/ + 5-wav32k/
                          └─[6] 3-get-semantic → 6-name2semantic-0.tsv
                              └─[7] 合并       → 6-name2semantic.tsv
                                  └─[8] s1_train (GPT)   → <exp>-e{N}.ckpt
                                      └─[9] s2_train (SoVITS) → <exp>_e{N}_s{N}.pth

## 用法

    python pipeline.py --input /path/to/audio.wav --voice myvoice \
        [--work-dir ...] [--skip-asr] [--transcript-file ...] \
        [--epochs-s1 8] [--epochs-s2 8] [--gpus 0]

产物落在 `/root/autodl-tmp/voices/<voice>/`（**刻意放在仓库外**，零污染）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------ 固定路径

BASE = Path("/root/autodl-tmp")
REPO = BASE / "Linly-Talker"                 # 项目仓库（只读，绝不写入）
GSV = REPO / "GPT_SoVITS"                    # GPT-SoVITS 子目录
SHIM_DIR = BASE / "voice-train"              # 本目录：PYTHONPATH 挂载点
VOICES_DIR = BASE / "voices"                 # 训练产物的最终归宿
PYTHON = BASE / "conda" / "envs" / "linly" / "bin" / "python"

# 仓库自带的底座权重（pre-v2 老快照，必须与训练配置配套）
PRETRAINED = GSV / "pretrained_models"
BERT_DIR = PRETRAINED / "chinese-roberta-wwm-ext-large"
HUBERT_DIR = PRETRAINED / "chinese-hubert-base"
S2G = PRETRAINED / "s2G488k.pth"
S2D = PRETRAINED / "s2D488k.pth"

# 切片参数：取自上游 WebUI 默认值（webui.py 的 slicer 控件默认）。
# 分成两组：SLICER_KWARGS 进 Slicer 构造函数，SLICE_POST 用于切片后的幅度归一化
# —— 上游 slice_audio.py 把两者混在一个签名里，直接铺给构造函数会 TypeError。
SLICER_KWARGS = dict(threshold=-34, min_length=4000, min_interval=300,
                     hop_size=10, max_sil_kept=500)
SLICE_POST = dict(_max=0.9, alpha=0.25)


class StepError(RuntimeError):
    """某个步骤失败。带上步骤名，便于上层把 stage 报给用户。"""


# ------------------------------------------------------------------ 基础工具

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def bootstrap_path() -> None:
    """把解释器同级 bin 前置到本进程的 PATH。

    `child_env()` 只管子进程；而切片/ASR 是**在进程内**跑的，它们会调裸 `ffmpeg`
    （`my_utils.load_audio` 走 ffmpeg-python，最终 subprocess 执行 `ffmpeg`）。
    本机 ffmpeg 只在 conda 环境里（`/usr/bin/ffmpeg` 不存在），不前置就 127 /
    FileNotFoundError。这个坑部署时已经踩过一次（见交接文档「坑 13」）。
    """
    bindir = str(PYTHON.parent)
    cur = os.environ.get("PATH", "")
    if bindir not in cur.split(os.pathsep):
        os.environ["PATH"] = os.pathsep.join([bindir, cur])
    os.environ.setdefault("OMP_NUM_THREADS", "8")
    os.environ.setdefault("MKL_NUM_THREADS", "8")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境。

    三个必须处理的事（都是本项目实测踩过的坑）：
      1. PYTHONPATH 要挂**两个**目录：
         - SHIM_DIR：`from tools.i18n.i18n import ...`（process_ckpt 需要）；
         - GSV：`1-get-text.py` 里是裸的 `from text.cleaner import clean_text`，
           而它**不像 2-/3- 那样自己 `sys.path.append(os.getcwd())`**。
           以绝对路径调用脚本时 sys.path[0] 是 prepare_datasets/ 而非 GPT_SoVITS/，
           不补就会 ModuleNotFoundError: No module named 'text'。
      2. PATH 前置解释器同级 bin —— 否则 my_utils.load_audio 调裸 `ffmpeg` 会 127；
      3. OMP/MKL_NUM_THREADS 钉死 8 —— 平台注入的是 0，对 libgomp 非法。
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SHIM_DIR), str(GSV)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["PATH"] = os.pathsep.join([str(PYTHON.parent)] + [env.get("PATH", "")])
    env["OMP_NUM_THREADS"] = "8"
    env["MKL_NUM_THREADS"] = "8"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run(cmd: list[str], cwd: Path, stage: str, env: dict[str, str] | None = None,
        log_file: Path | None = None) -> None:
    """跑一个子进程；失败时抛 StepError 并把尾部输出带出来（否则排查全靠猜）。"""
    log(f"▶ {stage}")
    log(f"  $ {' '.join(cmd)}   (cwd={cwd})")
    fh = open(log_file, "a", encoding="utf-8") if log_file else None
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env or child_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace")
    finally:
        if fh:
            fh.close()
    out = proc.stdout or ""
    if fh is not None:
        pass
    if proc.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-25:])
        raise StepError(f"{stage} 失败（退出码 {proc.returncode}）：\n{tail}")
    log(f"  ✓ {stage} 完成")


# ------------------------------------------------------------------ 步骤 1：切片

def step_slice(src: Path, out_dir: Path) -> list[Path]:
    """把长音频按静音切成小段。

    直接复用上游 `slice()` 的算法，但**不能 import 上游的 slice_audio.py**
    —— 那个文件末尾是裸的 `print(slice(*sys.argv[1:]))`，没有 __main__ 保护，
    一 import 就会拿当前进程的 argv 去切音频。所以这里重写这十几行。
    """
    # slicer2 在本目录，my_utils 在仓库的 GPT_SoVITS/ 下 —— 两个都要在 sys.path 上。
    # 注意 os.chdir() **不会**把新工作目录加进 sys.path，必须显式插入。
    for p in (str(GSV), str(SHIM_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from slicer2 import Slicer          # noqa: E402
    from my_utils import load_audio     # 仓库 GPT_SoVITS/my_utils.py

    import numpy as np
    from scipy.io import wavfile

    out_dir.mkdir(parents=True, exist_ok=True)
    slicer = Slicer(sr=32000, **SLICER_KWARGS)

    audio = load_audio(str(src), 32000)
    n = 0
    for chunk, start, end in slicer.slice(audio):
        tmp_max = np.abs(chunk).max()
        if tmp_max > 1:
            chunk = chunk / tmp_max
        chunk = (chunk / tmp_max * (SLICE_POST["_max"] * SLICE_POST["alpha"])) \
            + (1 - SLICE_POST["alpha"]) * chunk
        wavfile.write(str(out_dir / f"{src.stem}_{start:010d}_{end:010d}.wav"),
                      32000, (chunk * 32767).astype(np.int16))
        n += 1
    if n == 0:
        raise StepError("切片后得到 0 段音频。多半是输入全是静音，或音量阈值 -34dB 过滤掉了。")
    log(f"  ✓ 切出 {n} 段")
    return sorted(out_dir.glob("*.wav"))


# ------------------------------------------------------------------ 步骤 2：ASR 标注

def _load_funasr_class():
    """按文件路径加载仓库的 ASR/FunASR.py。

    不能写 `from ASR.FunASR import FunASR`——那会先执行 ASR/__init__.py，
    而它会连带 import Whisper 与 OmniSenseVoice（后者没装，只打印警告）。
    也不能写 `import FunASR`——仓库根有个同名的 FunASR/ **模型目录**会撞名。
    所以用 importlib 按路径直接加载。
    """
    asr_py = REPO / "ASR" / "FunASR.py"
    spec = importlib.util.spec_from_file_location("_linly_funasr", asr_py)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO))        # 它的 `from src.cost_time import ...` 需要
    spec.loader.exec_module(mod)
    return mod.FunASR


def step_asr(wavs: list[Path], out_list: Path, speaker: str) -> list[tuple[Path, str]]:
    """对每段音频做识别，产出 `路径|说话人|语言|文本` 四字段的 list。

    复用仓库自带的 ASR/FunASR.py，它的三个模型**已经在本地**
    （FunASR/speech_seaco_paraformer... 等），所以全程离线、零下载。
    """
    FunASR = _load_funasr_class()
    asr = FunASR()                       # 模型加载一次，循环复用

    rows: list[tuple[Path, str]] = []
    out_list.parent.mkdir(parents=True, exist_ok=True)
    for i, w in enumerate(wavs, 1):
        text = (asr.transcribe(str(w)) or "").strip()
        # 竖线是 list 的分隔符，文本里混进一个就会让那一行被静默丢弃（上游会 except 吞掉）
        text = text.replace("|", "，").replace("\n", " ").strip()
        if not text:
            log(f"  ! 第 {i}/{len(wavs)} 段识别为空，跳过：{w.name}")
            continue
        rows.append((w, text))
        log(f"  [{i}/{len(wavs)}] {w.name} → {text[:40]}")

    if not rows:
        raise StepError("ASR 没有产出任何有效标注，无法继续。")

    with open(out_list, "w", encoding="utf-8") as f:
        for w, text in rows:
            f.write(f"{w.resolve()}|{speaker}|ZH|{text}\n")
    log(f"  ✓ 标注 {len(rows)} 条 → {out_list.name}")
    return rows


# ------------------------------------------------------------------ 步骤 3~7：预处理与合并

def write_transcript_file(rows: list[tuple[Path, str]], path: Path) -> None:
    """把（可编辑的）标注写成人类可读的纯文本，供人工校对。

    格式刻意做得极简：每行一个 `文件名<TAB>文本`。用户改完，用
    `apply_transcript()` 读回来，就能带着修正继续训练 —— 这就是「人工校对」那一关。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 每行格式：文件名<TAB>识别文本\n")
        f.write("# 直接改右侧文本即可。**不要**增删行、不要动左侧文件名、不要把 TAB 换成空格。\n")
        f.write("# 改完保存，训练会自动读取本文件里的文本（而不是 ASR 原始结果）。\n")
        for w, text in rows:
            f.write(f"{w.name}\t{text}\n")


def apply_transcript(rows: list[tuple[Path, str]], path: Path) -> list[tuple[Path, str]]:
    """用人工校对过的文本覆盖 ASR 结果。按文件名匹配。"""
    edited: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if "\t" not in line:
                continue
            name, _, text = line.partition("\t")
            text = text.strip().replace("|", "，")
            if text:
                edited[name.strip()] = text
    if not edited:
        raise StepError(f"校对文件 {path} 里没解析出任何有效行。")
    out = [(w, edited.get(w.name, text)) for w, text in rows]
    changed = sum(1 for (w, t_old), (w2, t_new) in zip(rows, out) if t_old != t_new)
    log(f"  ✓ 应用校对文件：{len(edited)} 条，其中 {changed} 条被修改")
    return out


def run_prepare_scripts(work: Path, list_file: Path, exp: str, gpu: str,
                        log_file: Path) -> None:
    """依次跑仓库的三个预处理脚本（它们全靠环境变量驱动，没有 argparse）。

    三次调用必须 **cwd=GPT_SoVITS**：脚本里 `now_dir = os.getcwd()` 之后
    `sys.path.append(now_dir)`，靠它才能 `from my_utils import load_audio`
    和 `import utils`。
    """
    opt_dir = work / "dataset"
    opt_dir.mkdir(parents=True, exist_ok=True)
    wav_dir = work / "slicer_opt"

    common = {
        "inp_text": str(list_file),
        "inp_wav_dir": str(wav_dir),
        "exp_name": exp,
        "opt_dir": str(opt_dir),
        "i_part": "0",
        "all_parts": "1",
        "_CUDA_VISIBLE_DEVICES": gpu,
        "is_half": "True",
    }
    prep = GSV / "prepare_datasets"

    run([str(PYTHON), "-s", str(prep / "1-get-text.py")], GSV, "预处理 1/3：文本→音素",
        child_env({**common, "bert_pretrained_dir": str(BERT_DIR)}), log_file)

    # 合并 2-name2text-{i_part}.txt → 2-name2text.txt
    # 上游这段逻辑在 webui.py 里（不在任何子脚本里），必须自己复刻。
    parts = sorted(opt_dir.glob("2-name2text-*.txt"))
    if not parts:
        raise StepError("1-get-text 没有产出 2-name2text-*.txt，检查标注格式是否为四段竖线。")
    lines: list[str] = []
    for p in parts:
        lines += p.read_text(encoding="utf-8").strip("\n").split("\n")
        p.unlink()
    (opt_dir / "2-name2text.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"  ✓ 合并 2-name2text.txt：{len(lines)} 行")

    run([str(PYTHON), "-s", str(prep / "2-get-hubert-wav32k.py")], GSV,
        "预处理 2/3：HuBERT 特征 + 32k 音频",
        child_env({**common, "cnhubert_base_dir": str(HUBERT_DIR)}), log_file)

    run([str(PYTHON), "-s", str(prep / "3-get-semantic.py")], GSV,
        "预处理 3/3：语义 token",
        child_env({**common, "pretrained_s2G": str(S2G),
                   "s2config_path": str(GSV / "configs" / "s2.json")}), log_file)

    # 合并 6-name2semantic-{i_part}.tsv → 6-name2semantic.tsv
    # ⚠️ 表头 `item_name\tsemantic_audio` 必须加：下游 AR/data/dataset.py 用
    #    pandas.read_csv(delimiter="\t") 读，首行会被当列名，缺了表头就少一条数据。
    tparts = sorted(opt_dir.glob("6-name2semantic-*.tsv"))
    if not tparts:
        raise StepError("3-get-semantic 没有产出 6-name2semantic-*.tsv。")
    tlines = ["item_name\tsemantic_audio"]
    for p in tparts:
        tlines += p.read_text(encoding="utf-8").strip("\n").split("\n")
        p.unlink()
    (opt_dir / "6-name2semantic.tsv").write_text("\n".join(tlines) + "\n", encoding="utf-8")
    log(f"  ✓ 合并 6-name2semantic.tsv：{len(tlines) - 1} 条（含表头）")

    # 训练前自检：三份数据必须对得上，否则训练会以 ZeroDivisionError 之类的方式炸得很难看
    n_txt = len(lines)
    n_hub = len(list((opt_dir / "4-cnhubert").glob("*.pt"))) if (opt_dir / "4-cnhubert").is_dir() else 0
    n_sem = len(tlines) - 1
    log(f"  数据自检：文本 {n_txt} / HuBERT {n_hub} / 语义 {n_sem}")
    if min(n_txt, n_hub, n_sem) == 0:
        raise StepError(f"预处理产物为空（文本 {n_txt} / HuBERT {n_hub} / 语义 {n_sem}），无法训练。")


# ------------------------------------------------------------------ 步骤 8~9：配置生成与训练

def gen_s2_config(work: Path, exp: str, gpu: str, epochs: int, batch: int,
                  out_json: Path) -> None:
    """生成 s2(SoVITS) 训练配置。

    上游这份配置是 webui.py 的 `open1Ba()` 在运行时动态拼的（模板 + 补键），
    仓库里 configs/s2.json 缺 8 个训练必需的键，直接喂会 KeyError。
    这里按上游的赋值逻辑复刻，并把路径写成**绝对路径**以摆脱 cwd 依赖。
    """
    cfg = json.loads((GSV / "configs" / "s2.json").read_text(encoding="utf-8"))
    dataset = work / "dataset"
    cfg["train"].update({
        "fp16_run": True,
        "batch_size": batch,
        "epochs": epochs,
        "text_low_lr_rate": 0.4,
        "pretrained_s2G": str(S2G),
        "pretrained_s2D": str(S2D),
        "if_save_latest": True,
        "if_save_every_weights": True,
        "save_every_epoch": max(1, epochs // 2),
        "gpu_numbers": gpu,
        "grad_ckpt": False,
    })
    cfg["data"]["exp_dir"] = str(dataset)
    cfg["s2_ckpt_dir"] = str(dataset)
    cfg["save_weight_dir"] = str(work / "sovits_weights")
    cfg["name"] = exp
    # 这两行上游在 webui.py 里靠「启动时先 makedirs」保证存在，
    # 而 my_save/savee 内部**不做 makedirs**，不存在会直接崩。
    (work / "sovits_weights").mkdir(parents=True, exist_ok=True)
    (dataset / f"logs_s2").mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  ✓ 生成 {out_json.name}")


def gen_s1_config(work: Path, exp: str, gpu: str, epochs: int, batch: int,
                  out_yaml: Path) -> None:
    """生成 s1(GPT) 训练配置。对应上游 webui.py 的 `open1Bb()`。"""
    import yaml

    cfg = yaml.safe_load((GSV / "configs" / "s1longer.yaml").read_text(encoding="utf-8"))
    dataset = work / "dataset"
    cfg["train"].update({
        "batch_size": batch,
        "epochs": epochs,
        "save_every_n_epoch": max(1, epochs // 2),
        "if_save_every_weights": True,
        "if_save_latest": True,
        "exp_name": exp,
        "half_weights_save_dir": str(work / "gpt_weights"),
        "precision": "16-mixed",
    })
    cfg["train_semantic_path"] = str(dataset / "6-name2semantic.tsv")
    cfg["train_phoneme_path"] = str(dataset / "2-name2text.txt")
    cfg["output_dir"] = str(work / "logs_s1")
    cfg["pretrained_s1"] = str(
        PRETRAINED / "s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt")
    (work / "gpt_weights").mkdir(parents=True, exist_ok=True)
    (work / "logs_s1").mkdir(parents=True, exist_ok=True)
    out_yaml.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    log(f"  ✓ 生成 {out_yaml.name}")


def pick_batch(gpu: str, fallback: int = 4) -> int:
    """按空闲显存挑 batch size。上游的隐含假设是 batch ≈ 空闲显存GB / 2。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits",
             f"--id={gpu}"], capture_output=True, text=True, timeout=10)
        free_gb = int(out.stdout.strip().splitlines()[0]) / 1024
        return max(1, int(free_gb // 2))
    except Exception:
        return fallback


def run_training(work: Path, exp: str, gpu: str, epochs_s1: int, epochs_s2: int,
                 log_file: Path) -> tuple[Path, Path]:
    """跑 s1 和 s2 两段训练。返回 (s1_ckpt, s2_pth)。"""
    env = child_env({"_CUDA_VISIBLE_DEVICES": gpu, "hz": "25hz"})
    batch = pick_batch(gpu)
    log(f"  自动选定 batch_size={batch}（按空闲显存）")

    s2_json = work / "train_s2.json"
    s1_yaml = work / "train_s1.yaml"
    gen_s2_config(work, exp, gpu, epochs_s2, batch, s2_json)
    gen_s1_config(work, exp, gpu, epochs_s1, batch, s1_yaml)

    # 注意：s2_train.py 无 argparse，它的 get_hparams 在 **import 期**执行，
    # 参数解析藏在 GPT_SoVITS/utils.py 里，且键名是 `--config`（不是 -c）。
    run([str(PYTHON), "-s", str(GSV / "s2_train.py"), "--config", str(s2_json)],
        REPO, "训练 s2（SoVITS，决定音色）", env, log_file)

    # s1_train.py 用的是 --config_file（注意与上面不同名，写错会静默吃默认值）
    run([str(PYTHON), "-s", str(GSV / "s1_train.py"), "--config_file", str(s1_yaml)],
        REPO, "训练 s1（GPT，决定韵律）", env, log_file)

    s1_ckpts = sorted((work / "gpt_weights").glob(f"{exp}-e*.ckpt"))
    s2_pths = sorted((work / "sovits_weights").glob(f"{exp}_e*_s*.pth"))
    if not s1_ckpts:
        raise StepError(f"训练结束但没找到 s1 产物（{work}/gpt_weights/{exp}-e*.ckpt）")
    if not s2_pths:
        raise StepError(f"训练结束但没找到 s2 产物（{work}/sovits_weights/{exp}_e*_s*.pth）")

    def _ep(p: Path) -> int:
        m = re.search(r"-e(\d+)\.ckpt$", p.name) or re.search(r"_e(\d+)_", p.name)
        return int(m.group(1)) if m else 0

    return max(s1_ckpts, key=_ep), max(s2_pths, key=_ep)


# ------------------------------------------------------------------ 步骤 10：注册

def register_voice(voice: str, s1: Path, s2: Path, meta: dict) -> Path:
    """把训练产物登记成一个可用的音色。

    ⚠️ **刻意放在仓库之外**（/root/autodl-tmp/voices/），两个原因：
      1. 仓库 `git status` 保持干净，不留任何未跟踪文件；
      2. `VITS/GPT_SoVITS.py:351` 有个 `if ("pretrained" not in sovits_path): del vq_model.enc_q`
         —— 它是对**完整路径做子串匹配**。微调产物按约定不含 enc_q（process_ckpt 里
         `if "enc_q" in key: continue` 明确跳过），所以路径里**不能出现 "pretrained"**，
         否则会走错分支、白留一份用不上的后验编码器。放这里天然不含该子串。
    """
    dest = VOICES_DIR / voice
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s1, dest / "gpt.ckpt")
    shutil.copy2(s2, dest / "sovits.pth")
    meta = {**meta, "voice": voice, "s1_source": str(s1), "s2_source": str(s2),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  ✓ 音色已注册：{dest}")
    return dest


# ------------------------------------------------------------------ 编排

def train_voice(src: Path, voice: str, work: Path, *,
                skip_asr_review: bool = False,
                transcript_file: Path | None = None,
                epochs_s1: int = 8, epochs_s2: int = 8,
                gpu: str = "0", stop_after_asr: bool = False) -> dict:
    """跑完整条流水线。

    `stop_after_asr=True` 时，做完 ASR 就停，把标注写成 `transcript.txt` 返回
    —— 这就是「人工校对」那一关的挂起点。调用方拿到文件让人改，
    再用 `transcript_file=` 指回来继续。
    """
    bootstrap_path()
    work.mkdir(parents=True, exist_ok=True)
    log_file = work / "train.log"
    log(f"═══ 音色训练开始：{voice} ═══")
    log(f"  输入：{src}")
    log(f"  工作目录：{work}")

    if not src.exists():
        raise StepError(f"输入音频不存在：{src}")
    if not PYTHON.exists():
        raise StepError(f"训练解释器不存在：{PYTHON}")

    # cwd 必须是 GPT_SoVITS（prepare 脚本靠 os.getcwd() 找 my_utils）
    os.chdir(GSV)

    wavs = step_slice(src, work / "slicer_opt")
    transcript = work / "transcript.txt"

    # 若已经有校对文件，直接用（跳过 ASR，省一大截时间）
    if transcript_file and transcript_file.exists():
        rows = _rebuild_rows_from_transcript(transcript_file, work / "slicer_opt")
        log(f"  ✓ 使用既有校对文件：{transcript_file}（跳过 ASR）")
    else:
        list_probe = work / "asr.list"
        if list_probe.exists() and skip_asr_review:
            rows = _read_list(list_probe)
            log("  ✓ 复用既有 ASR 结果")
        else:
            rows = step_asr(wavs, work / "asr.list", voice)

    write_transcript_file(rows, transcript)

    if stop_after_asr:
        log(f"⏸ 已在 ASR 后停下，等待人工校对：{transcript}")
        return {"stage": "awaiting_review", "transcript": str(transcript),
                "segments": len(rows), "voice": voice}

    # 写最终 list（四字段；文本里的竖线已在 ASR 阶段清掉）
    list_file = work / "dataset.list"
    with open(list_file, "w", encoding="utf-8") as f:
        for w, text in rows:
            f.write(f"{w.resolve()}|{voice}|ZH|{text}\n")

    run_prepare_scripts(work, list_file, voice, gpu, log_file)
    s1, s2 = run_training(work, voice, gpu, epochs_s1, epochs_s2, log_file)
    dest = register_voice(voice, s1, s2, {
        "segments": len(rows), "epochs_s1": epochs_s1, "epochs_s2": epochs_s2,
        "source_audio": str(src), "work_dir": str(work),
    })
    log(f"═══ 训练完成：{dest} ═══")
    return {"stage": "done", "voice": voice, "path": str(dest),
            "gpt": str(dest / "gpt.ckpt"), "sovits": str(dest / "sovits.pth")}


def _read_list(list_file: Path) -> list[tuple[Path, str]]:
    rows = []
    for line in list_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 4:
            continue
        rows.append((Path(parts[0]), parts[3]))
    return rows


def _rebuild_rows_from_transcript(tf: Path, wav_dir: Path) -> list[tuple[Path, str]]:
    rows = []
    for line in tf.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "\t" not in line:
            continue
        name, _, text = line.partition("\t")
        w = wav_dir / name.strip()
        if w.exists() and text.strip():
            rows.append((w, text.strip().replace("|", "，")))
    if not rows:
        raise StepError(f"校对文件里没有能对应到音频的有效行：{tf}")
    return rows


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="音色训练流水线（GPT-SoVITS 微调）")
    ap.add_argument("--input", required=True, help="原始音频（wav/mp3/m4a 等，ffmpeg 能解的都行）")
    ap.add_argument("--voice", required=True, help="音色名（英文/数字，将作为模型名）")
    ap.add_argument("--work-dir", default=None, help="工作目录，默认 voice-train/work/<voice>")
    ap.add_argument("--transcript-file", default=None,
                    help="已校对好的标注文件；给了就跳过 ASR")
    ap.add_argument("--stop-after-asr", action="store_true",
                    help="只做到 ASR 就停，产出 transcript.txt 供人工校对")
    ap.add_argument("--skip-asr", action="store_true", help="复用已有 asr.list，跳过识别")
    ap.add_argument("--epochs-s1", type=int, default=8)
    ap.add_argument("--epochs-s2", type=int, default=8)
    ap.add_argument("--gpus", default="0", help="CUDA_VISIBLE_DEVICES 取值")
    args = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", args.voice):
        print("音色名只允许字母/数字/下划线，且不超过 32 字符", file=sys.stderr)
        return 2

    work = Path(args.work_dir) if args.work_dir else SHIM_DIR / "work" / args.voice
    try:
        result = train_voice(
            Path(args.input).resolve(), args.voice, work,
            skip_asr_review=args.skip_asr,
            transcript_file=Path(args.transcript_file) if args.transcript_file else None,
            epochs_s1=args.epochs_s1, epochs_s2=args.epochs_s2,
            gpu=args.gpus, stop_after_asr=args.stop_after_asr,
        )
    except StepError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
