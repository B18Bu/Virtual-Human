"""`tools.i18n.i18n` 的最小替身。

## 为什么需要这个文件

Linly-Talker 仓库里的 GPT-SoVITS 是上游的**裁剪版**，`tools/` 整个包被摘掉了
（`git ls-files | grep "GPT_SoVITS/tools"` = 0，历史里从未存在）。但仓库里仍有
3 处引用它：

    process_ckpt.py:5      from tools.i18n.i18n import I18nAuto   ← 训练必经之路
    inference_webui.py:67  from tools.i18n.i18n import I18nAuto
    inference_gui.py:7     from tools.i18n.i18n import I18nAuto

其中 `process_ckpt.py` 是 `s2_train.py` 的依赖（`s2_train.py:33` → `from process_ckpt import savee`），
缺了它训练脚本连 import 都过不去。

## 为什么是「直接返回原字符串」

上游的 I18nAuto 会查 locale JSON 做翻译。但本仓库这份代码里：

1. `process_ckpt.py:7` 建了 `i18n = I18nAuto()` 之后**从未使用** —— 纯死导入；
2. Linly-Talker 自己的推理封装 `VITS/GPT_SoVITS.py:398` 的 `dict_language`
   **直接用中文当键**（适配层注释也写明了这点），并不走 i18n。

所以恒等映射既能解开导入阻塞，又不会改变任何既有行为 —— 是这里唯一安全的选择。
（若哪天要用上游原版，把本文件换成上游 `tools/i18n/i18n.py` + 补 locale/*.json 即可。）
"""

__all__ = ["I18nAuto", "scan_language_list"]


class I18nAuto:
    """翻译器的恒等替身：`i18n("中文")` 原样返回 `"中文"`。"""

    def __init__(self, language=None):
        self.language = language or "zh_CN"
        self.language_map: dict[str, str] = {}

    def __call__(self, key):
        # 恒等：本仓库的代码不曾依赖真正的翻译结果
        return self.language_map.get(key, key)

    def __repr__(self) -> str:
        return f"I18nAuto(identity, language={self.language!r})"


def scan_language_list() -> list[str]:
    """上游用它扫描可用语言；本替身没有 locale 目录，返回中文占位。"""
    return ["zh_CN"]
