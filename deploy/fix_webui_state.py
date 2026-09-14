"""把 Gradio WebUI 的模型状态重置到「界面默认值」，修复界面与后端脱节。

## 为什么需要这个脚本

`webui.py` 把当前模型存在**模块级全局变量**里（`talker` / `asr` / `llm`），
由界面下拉框触发 `*_model_change()` 去改。这带来一个坑：

**全局变量被改了、但下拉框没跟着变 → 界面显示的和后端实际用的不是同一个东西。**

后果分两种：

| 脱节项 | 症状 |
| --- | --- |
| `talker` | 点上「生成数字人视频」直接报错，页面上只显示两个字「错误」 |
| `llm` | 不报错，但行为与下拉框显示不符（选了「直接回复」却在真对话） |

第一类是实际踩到的：用 API 把 `talker` 切成了 `Wav2Lipv2` 之后，界面仍选着
`SadTalker`，`human_response` 就去调 `talker.test2()`——

```
AttributeError: 'Wav2Lipv2' object has no attribute 'test2'
    at webui.py:324 in human_response
```

**关键点**：页面上只显示「错误」两个字（Gradio 的中文文案），
真实报错只在 `logs/webui.log` 里。以后见到「错误」先去翻日志，别猜。

> ⚠️ 这个坑不只在用 API 时会踩：**任何让后端模型变化、但下拉框没重新选择的路径都会中招。**
> 正常用浏览器操作时，改下拉框会同步改全局，所以不会出问题——
> 但用 `gradio_client` / curl 直接调 `*_model_change` 接口就会。

用法（WebUI 须已在 6008 监听）：
    /root/autodl-tmp/conda/envs/linly/bin/python /root/autodl-tmp/deploy/fix_webui_state.py
"""
import sys
from pathlib import Path

from gradio_client import Client

BASE = "http://127.0.0.1:6008"
CRED = Path("/root/autodl-tmp/linly-webui/.webui_credentials").read_text().strip()
USER, _, PASSWORD = CRED.partition(":")

# 要恢复成什么，以及界面上对应怎么选（顺序敏感：先切回驱动模型）
WANT = [
    ("talker_model_change_1", "SadTalker",    "数字人模型"),
    ("asr_model_change_1",    "Whisper-base", "语音识别模型"),
    ("llm_model_change_1",    "Qwen",         "LLM 模型"),
]


def main() -> int:
    client = Client(BASE, auth=(USER, PASSWORD))
    print("=" * 64)
    for api, value, label in WANT:
        try:
            got = client.predict(value, api_name=f"/{api}")
            flag = "✅" if str(got) == value else "⚠️ "
            print(f"{flag} {label:<12s} → {got}")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {label:<12s} 切换失败: {type(exc).__name__}: {str(exc)[:120]}")
    print("=" * 64)
    print("界面上的下拉框请一并选成上面这些，两边一致才不会再次脱节。")
    print("· 数字人模型：SadTalker（想更省显存/更快可换 Wav2Lip，但两处都要改）")
    print("· LLM 模型  ：Qwen（默认的「直接回复」只会原样复读你的话）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
