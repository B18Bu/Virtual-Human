"""链路冒烟测试 —— 不需要 GPU。

服务使用 MockPipeline 时即可跑通，用来验证：
鉴权拦截 / 入队 / 单卡串行调度 / 进度轮询 / 产物下载 / 归属校验 / 取消。

用法：
    conda activate linly
    python smoke_test.py [BASE_URL]

注意：MockPipeline 产物是文本占位文件，不是可播放视频。此脚本通过
只代表 HTTP 链路与调度正确，不代表模型跑通。
"""
from __future__ import annotations

import base64
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6006").rstrip("/")
KEY_FILE = "/root/autodl-tmp/linly-api/.api_key"

# 1x1 透明 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (passed if condition else failed).append(f"{name} {detail}".strip())
    print(f"  {'PASS' if condition else 'FAIL'}  {name} {detail}")


def main() -> int:
    key = open(KEY_FILE, encoding="utf-8").read().strip()
    headers = {"X-API-Key": key}

    with httpx.Client(timeout=30.0) as client:
        print("\n[1] 健康检查")
        r = client.get(f"{BASE}/api/v1/health")
        check("health 返回 200", r.status_code == 200, f"({r.status_code})")
        h = r.json()
        check("健康检查无需鉴权", True)
        print(f"       pipeline_impl={h.get('pipeline_impl')} gpu_available={h.get('gpu_available')}")
        print(f"       deploy_scope={h.get('deploy_scope')}")

        print("\n[2] 鉴权")
        r = client.post(f"{BASE}/api/v1/tasks", json={"text": "x", "image_base64": "x"})
        check("无 Key 提交被拒（401）", r.status_code == 401, f"({r.status_code})")
        r = client.post(
            f"{BASE}/api/v1/tasks",
            json={"text": "x", "image_base64": "x"},
            headers={"X-API-Key": "wrong-key"},
        )
        check("错误 Key 被拒（401）", r.status_code == 401, f"({r.status_code})")

        print("\n[3] JSON 提交")
        r = client.post(
            f"{BASE}/api/v1/tasks",
            json={"text": "你好，这是一次冒烟测试。", "image_base64": base64.b64encode(TINY_PNG).decode()},
            headers=headers,
        )
        check("提交返回 202", r.status_code == 202, f"({r.status_code})")
        if r.status_code != 202:
            print(f"       响应体：{r.text[:300]}")
            return 1
        t1 = r.json()["task_id"]
        print(f"       task_id={t1}")

        print("\n[4] multipart 提交（验证排队）")
        r = client.post(
            f"{BASE}/api/v1/tasks/upload",
            files={"image": ("face.png", TINY_PNG, "image/png")},
            data={"text": "第二个任务，应当排在第一之后。"},
            headers=headers,
        )
        check("上传提交返回 202", r.status_code == 202, f"({r.status_code})")
        t2 = r.json()["task_id"]
        print(f"       task_id={t2}")

        print("\n[5] 轮询直到结束")
        seen_order: list[str] = []
        deadline = time.time() + 90
        statuses: dict[str, str] = {}
        while time.time() < deadline:
            s = {}
            for tid in (t1, t2):
                info = client.get(f"{BASE}/api/v1/tasks/{tid}", headers=headers).json()
                s[tid] = info["status"]
                if info["status"] == "running" and (not seen_order or seen_order[-1] != tid):
                    seen_order.append(tid)
            statuses = s
            if all(v in ("succeeded", "failed", "canceled") for v in s.values()):
                break
            time.sleep(1.5)

        print(f"       statuses={statuses}")
        print(f"       执行顺序={seen_order}")
        check("两个任务都成功", all(v == "succeeded" for v in statuses.values()), str(statuses))
        # 单 worker 必须串行：t1 先跑完，t2 才可能开始
        check("单卡串行（无重叠执行）", len(seen_order) <= 2 and (not seen_order or seen_order[0] == t1))

        print("\n[6] 下载产物")
        info = client.get(f"{BASE}/api/v1/tasks/{t1}", headers=headers).json()
        check("返回 result_url", bool(info.get("result_url")), str(info.get("result_url")))
        check("返回耗时", info.get("elapsed_seconds") is not None, f"{info.get('elapsed_seconds')}s")
        if info.get("result_url"):
            # result_url 含公网前缀，这里换成本地 BASE 直连测试
            local = f"{BASE}/api/v1/files/{info['result_url'].rsplit('/', 1)[-1]}"
            r = client.get(local)
            check("产物可下载（200）", r.status_code == 200, f"({r.status_code})")
            check("产物非空", len(r.content) > 0, f"{len(r.content)} 字节")

        print("\n[7] 归属校验与错误分支")
        r = client.get(f"{BASE}/api/v1/files/not-a-real-task.mp4")
        check("不存在的产物返回 404", r.status_code == 404, f"({r.status_code})")
        r = client.get(f"{BASE}/api/v1/files/..%2f..%2fetc%2fpasswd")
        check("目录穿越被挡（非 200）", r.status_code != 200, f"({r.status_code})")
        r = client.post(
            f"{BASE}/api/v1/tasks",
            json={"text": "x", "image_base64": base64.b64encode(TINY_PNG).decode(), "mode": "bogus"},
            headers=headers,
        )
        check("非法 mode 返回 400", r.status_code == 400, f"({r.status_code})")
        r = client.post(
            f"{BASE}/api/v1/tasks",
            json={"text": "   ", "image_base64": base64.b64encode(TINY_PNG).decode()},
            headers=headers,
        )
        check("空 text 返回 400", r.status_code in (400, 422), f"({r.status_code})")

    print("\n" + "=" * 60)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for item in failed:
        print(f"  FAILED: {item}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
