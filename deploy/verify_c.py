"""阶段 C / 验收清单第 4、5 项：经 HTTP 提交任务 → 轮询 → 取产物，做一次真正的端到端。

与 verify_b3.py 的分工：
· verify_b3.py 直调适配层，验的是**生成链路**（TTS → 口型驱动 → 产物）；
· 本脚本走**完整的服务链路**——鉴权 → base64 上传 → 入队 → 单 worker 串行推理 →
  产物改名落 outputs/<task_id>.mp4 → GET /api/v1/files 下发。

结论行统一用 '>>>' 前缀。全部通过时退出码为 0。

用法（服务须已在 6006 监听）：
    export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH   # 脚本要调 ffprobe
    /root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_c.py
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _probe import video_checks  # noqa: E402

BASE = "http://127.0.0.1:6006"
KEY_FILE = Path("/root/autodl-tmp/linly-api/.api_key")
IMAGE = Path("/root/autodl-tmp/Linly-Talker/inputs/girl.png")
TEXT = "大家好，我是数字人小美，很高兴认识你。"
OUT = Path("/root/autodl-tmp/linly-api/outputs/verify/c_http_e2e.mp4")
MIN_BYTES = 100 * 1024
POLL_TIMEOUT = 900  # 首个任务要等模型加载，给足余量

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f">>> 验收（{name:<10s}）: {'通过' if ok else '不通过'}   {detail}", flush=True)
    if not ok:
        fails.append(name)


def call(method: str, path: str, *, key: str | None = None, body: bytes | None = None,
         ctype: str = "application/json") -> tuple[int, bytes]:
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", ctype)
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------- 0. 鉴权
key = KEY_FILE.read_text().strip()
# /api/v1/health 与 / 是**故意公开**的（便于探活），要验鉴权得挑一个需要 key 的端点。
status, _ = call("GET", "/api/v1/tasks")
check("裸请求被拒", status == 401, f"HTTP {status}")

# ---------------------------------------------------------------- 1. 健康检查
status, raw = call("GET", "/api/v1/health")
health = json.loads(raw)
print(f">>> health          : {json.dumps(health, ensure_ascii=False)}", flush=True)
check("服务就绪", status == 200 and health["pipeline_ready"], f"HTTP {status}")
check("真实现已生效", health["pipeline_impl"] == "linly-talker", health["pipeline_impl"])

# ---------------------------------------------------------------- 2. 提交任务
payload = json.dumps({
    "text": TEXT,
    "image_base64": base64.b64encode(IMAGE.read_bytes()).decode(),
    "mode": "sadtalker",
}).encode()
t0 = time.time()
status, raw = call("POST", "/api/v1/tasks", key=key, body=payload)
task = json.loads(raw)
print(f">>> 提交            : HTTP {status} {json.dumps(task, ensure_ascii=False)}", flush=True)
check("任务受理", status in (200, 202) and "task_id" in task, f"HTTP {status}")
task_id = task["task_id"]

# ---------------------------------------------------------------- 3. 轮询
last = ""
info: dict = {}
while time.time() - t0 < POLL_TIMEOUT:
    _, raw = call("GET", f"/api/v1/tasks/{task_id}", key=key)
    info = json.loads(raw)
    line = f"{info['status']} {info['progress']:.2f} {info.get('stage', '')}"
    if line != last:
        print(f">>> 轮询 [{time.time()-t0:6.1f}s]: {line}", flush=True)
        last = line
    if info["status"] in ("succeeded", "failed", "canceled"):
        break
    time.sleep(2)
elapsed = time.time() - t0

check("任务成功", info.get("status") == "succeeded", f"{info.get('status')} {info.get('error') or ''}")
if info.get("status") != "succeeded":
    print(f">>> 验收结论        : 未通过：{fails}", flush=True)
    sys.exit(1)

# ---------------------------------------------------------------- 4. 取产物
# result_url 是公网入口（AutoDLService6006URL），容器内解析不到那个域名，
# 所以只取 path 走本机回环——对外的可用性由「公网入口」一节单独说明。
# ⚠️ **查询串必须保留**：产物下载要签名（?e=…&s=…），只取 .path 会 404。
url = info["result_url"]
_u = urllib.parse.urlparse(url)
path = (_u.path + (f"?{_u.query}" if _u.query else "")) or url
status, blob = call("GET", path, key=key)
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_bytes(blob)
size = OUT.stat().st_size
print(f">>> 产物            : {url}", flush=True)
print(f">>> 产物落盘        : {OUT}  {size} bytes ({size/1024:.0f} KB)  端到端耗时 {elapsed:.1f}s", flush=True)

check("产物可下载", status == 200 and size > 0, f"HTTP {status}")
check("字节数一致", size == info.get("result_bytes"), f"{size} vs {info.get('result_bytes')}")

checks, summary = video_checks(OUT, min_bytes=MIN_BYTES)
print(f">>> ffprobe         : {summary}", flush=True)
for name, (ok, detail) in checks.items():
    check(name, ok, detail)

print(f">>> 验收结论        : {'全部通过' if not fails else '未通过：' + '、'.join(fails)}", flush=True)
sys.exit(1 if fails else 0)
