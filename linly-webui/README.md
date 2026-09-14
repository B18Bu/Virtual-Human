# Linly-Talker Gradio WebUI（数字人对话交互界面）

把 Linly-Talker **自带的** Gradio WebUI 拉起来，放在 **6008**。**不修改项目任何源码。**

## 为什么需要这一层

项目自己的 `webui.py` 直接跑不起来，两个原因：

1. **端口冲突**。`webui.py:15` 是 `from configs import *`，端口取自 `configs.py:2` 的
   `port = 6006`；而 6006 已被 `linly-api` 的 FastAPI 服务独占（AutoDL 只映射 6006/6008，
   两个都得用上），改 `configs.py` 又属部署硬性约束第 3 条明令禁止。
   → `launch.py` 在 `webui.py` 执行那行之前先把 `configs.port`（以及 `configs.ip`）改掉，
     `import *` 自然拿到新值。
2. **没有鉴权**。6008 同样映射公网，而 `webui.py` 调 `demo.launch()` 时没传 `auth`，
   裸奔等于把 GPU 免费送人。→ 启动前把 `gr.Blocks.launch` 包一层注入 `auth`。

## 启动 / 停止

```bash
# 启动（约 80 秒加载完模型）
nohup bash /root/autodl-tmp/linly-webui/run.sh > /root/autodl-tmp/logs/webui.log 2>&1 &

# 确认监听
ss -lntp | grep 6008

# 停止 —— ⚠️ 不要用 pkill -f "launch.py" 之外的写法：
#   run.sh 里是先 cd 再 exec python -u launch.py，进程命令行里**没有**完整路径，
#   按路径匹配会落空。按 PID 最稳：
kill $(pgrep -f '[l]aunch.py')
```

## 访问

```
https://<你的实例地址>　（6008 入口，前缀是双 u）
```

注意 6008 的公网入口是 6006 那个地址**前面多一个 `u`**。具体看在控制台「自定义服务」。
打开后会先弹登录框。

### ⚠️ 必须设 `GRADIO_ROOT_PATH`，否则登录页会「永远登不进去」

`launch.py` 里有一行把公网入口钉死成 Gradio 的 `root_path`：

```python
os.environ.setdefault("GRADIO_ROOT_PATH", os.getenv("AutoDLService6008URL", ""))
```

**不设会怎样**：Gradio 用 `route_utils.get_root_url()` 推断「我自己对外是什么地址」，
优先级是 ① `root_path` 是完整 URL → ② `x-forwarded-host` 头 → ③ 请求自身的 URL。
AutoDL 的网关**不转发 `x-forwarded-host`**，于是落到 ③，前端拿到的
`window.gradio_config.root` 变成 `http://localhost:6008`。浏览器随后把登录请求
POST 到**用户自己电脑的 6008 端口**——表现就是「登录页一直转，怎么点都进不去」，
而服务端日志里**一条登录请求都看不到**（因为请求根本没到服务器）。

设成完整 URL 后命中 ①，与请求头无关。验证方法：

```bash
curl -s localhost:6008/ | grep -o '"root":"[^"]*"'
# 期望输出公网地址，而不是 http://localhost:6008
```

> 这个坑值得记：**curl 能登进去不等于浏览器能登进去**。curl 直接打 `/login`
> 是路径请求，不经过前端拼 URL 这一步，所以完全测不出这个问题。

```bash
cat /root/autodl-tmp/linly-webui/.webui_credentials   # linly:<密码>
```

凭据首次启动时随机生成并落盘（0600）。**生成用的字符集刻意剔除了易混字符**
（`0 O 1 l I 2 Z 5 S 8 B`）—— 上一版用 `secrets.token_urlsafe()` 生成的密码里同时有
字母 `o` 和数字 `0`，照着抄必然看错，登录一直 400。

想换成自己好记的密码，直接覆盖这个文件再重启（文件优先于环境变量）：

```bash
echo "linly:你的密码" > /root/autodl-tmp/linly-webui/.webui_credentials
chmod 600 /root/autodl-tmp/linly-webui/.webui_credentials
kill $(pgrep -f '[l]aunch.py'); sleep 3
nohup bash /root/autodl-tmp/linly-webui/run.sh > /root/autodl-tmp/logs/webui.log 2>&1 &
```

> Gradio 4.44 用的是**表单登录 + cookie**，不认 HTTP Basic auth。
> 拿 curl 验证鉴权要这样（直接带 `-u` 会看到 401，那不是坏了）：
> ```bash
> curl -c /tmp/c.txt -X POST localhost:6008/login -d "username=linly&password=<密码>"
> curl -b /tmp/c.txt -o /dev/null -w '%{http_code}\n' localhost:6008/config   # 期望 200
> ```

## 三个界面

| 标签页 | 状态 |
| --- | --- |
| 个性化角色互动 | ✅ 可用（图片 + 文本 → 视频） |
| 数字人多轮智能对话 | ✅ 可用（语音 → ASR → LLM → TTS → 视频），实测见 `deploy/verify_webui.py` |
| MuseTalk 数字人实时对话 | ❌ **嘴不动**，详见交接文档 5.8。这是项目侧 MuseTalk 的问题，不是本启动器的 |

**LLM 默认是「直接回复」（原样回显你的话）**，要真对话得在界面上把 LLM 切成 `Qwen`
（`Qwen/Qwen-1_8B-Chat` 权重已在本地，加载约需几秒、占 3~4G 显存）。

**「preprocess」默认是 `crop`**，只会输出 256x256 的人脸裁切（约 63KB）。
想要整幅人像（640x1024）**请在界面上把它改成 `full`** —— 原因见交接文档 5.7。

## 资源

和 `linly-api`（6006）共用同一张卡。两者都常驻模型，实测合计约 8.4 GB / 23.5 GB。
显存紧张时优先停这个：它加载的模型更多。

## 复验

```bash
export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH
/root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_webui.py       # 默认「直接回复」
/root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_webui.py Qwen  # 真 LLM
```
