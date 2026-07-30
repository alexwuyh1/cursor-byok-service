# cursor-byok-service

在 Cursor IDE 中通过 BYOK（Bring Your Own Key）调用自有 API key 的模型，同时绕开 Cursor 内置模型名冲突导致的请求格式篡改问题。

## 解决什么问题

Cursor 的 BYOK 是全局二元开关。当你添加的自定义模型名与 Cursor 内置模型名重名（如 `glm-5.2`、`gpt-5.5` 等），Cursor 后端会做模糊匹配，将自定义模型解析为内置模型，导致：

- 附加 Cursor 专属参数（`reasoning.effort`、`store` 等）
- 使用 Responses API 格式（`input` 而非 `messages`）
- 你的 provider 收到非标准请求后拒绝，Cursor 误报为 "Invalid API key" 或 "Provider Error"

本服务用模型别名绕过内置解析，在代理层改回真实 model ID，只改一个字符串，开销极小。

## 工作原理

```
Cursor IDE → Cursor 云端 → 你的公网代理 (本服务) → 百炼 API
                ↑                              ↑
           发送别名 bl-llm-1              改写为 glm-5.2
           标准 Chat 格式                转发给真实 provider
```

服务脚本用 Python 标准库实现（无需 venv/pip），单进程同时管理：

1. **HTTP 代理**：接收 `/v1/chat/completions` 请求，改写 `model` 字段别名→真实 ID，转发给百炼，支持 SSE 流式
2. **cloudflared 隧道子进程**：启动时从 `config.json` 自动派生 `cf-config.yml`（查找隧道 UUID、写入 hostname/credentials），崩溃自动重启

`config.json` 是唯一配置源。编辑一个文件，重启服务，一切生效。

## 快速开始

### 前置要求

- macOS（用了 launchd 做开机自启；Linux 可改用 systemd）
- [Homebrew](https://brew.sh) 安装的 Python 3.10+ 和 cloudflared
- Cursor Pro 订阅（BYOK 需要付费版）
- 阿里云百炼 API key（或任何 OpenAI 兼容的 provider）
- Cloudflare 管理的域名（用于命名隧道固定地址）

```bash
brew install python@3.14 cloudflared
```

### 安装

```bash
cd cursor-byok-service
cp config.example.json config.json
# 编辑 config.json：填入你的 API key、域名、模型映射
./install.sh
```

`install.sh` 会交互式引导你完成：Cloudflare 认证、创建隧道、DNS 路由、写 config.json、生成 launchd plist 并加载。

### 在 Cursor 中配置

1. Settings → Models → 打开 **OpenAI API Key**，填百炼 key
2. 打开 **Override OpenAI Base URL**，填 `https://cursor.your-domain.com/v1`
3. **Add Custom Model**，添加 `bl-llm-1`、`bl-llm-2`（与 config.json 的 model_map key 一致）
4. 在模型选择器选 `bl-llm-1` 测试

## 日常管理

```bash
# 查看状态
launchctl list | grep cursor.byok

# 查看日志
tail -f service.log
tail -f tunnel.log

# 修改配置后重启（cf-config.yml 会自动重新生成）
launchctl unload ~/Library/LaunchAgents/com.cursor.byok.service.plist
launchctl load ~/Library/LaunchAgents/com.cursor.byok.service.plist
```

## config.json 字段说明

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| `model_map` | 模型别名 → provider 真实 model ID | `{"bl-llm-1": "glm-5.2"}` |
| `bailian_base_url` | provider 的 OpenAI 兼容 endpoint | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `bailian_api_key` | provider 的 API key | `sk-xxx` |
| `listen_port` | 本地代理监听端口 | `8787` |
| `tunnel_name` | cloudflared 命名隧道名称 | `bailian-proxy` |
| `hostname` | 公网访问域名 | `cursor.your-domain.com` |
| `cloudflared_bin` | cloudflared 二进制路径 | `/opt/homebrew/bin/cloudflared` |
| `cf_credentials_dir` | 隧道凭证目录（留空自动推导） | |
| `run_tunnel` | 是否启动隧道子进程 | `true` |

## 关键注意事项

- **模型别名不能包含内置模型名片段**：`bailian-glm-5.2` 不行（包含 `glm-5.2`），`bl-llm-1` 可以。Cursor 做子串模糊匹配，加前缀没用。
- **localhost 不可达**：BYOK 请求从 Cursor 云端后端发出，必须用公网隧道。
- **多 provider 支持**：`model_map` 值为对象时可指定独立 `base_url` 和 `api_key`，实现一个代理服务路由到多个 provider。
- **用 Homebrew Python**：`/opt/homebrew/bin/python3`，不要用 `/usr/bin/python3`（Xcode 沙箱版无权访问某些目录）。
- **config.json 不提交 git**：里面有 API key，已加入 `.gitignore`，用 `config.example.json` 做模板。

## 项目结构

```
cursor-byok-service/
├── cursor-byok-service.py   # 统一服务（纯标准库，无需 venv/pip）
├── config.example.json       # 配置模板（无真实 key）
├── config.json               # 实际配置（gitignore，install.sh 生成）
├── cf-config.yml             # 自动生成（启动时从 config.json 派生，gitignore）
├── install.sh                # 一键安装（交互式）
├── .gitignore
├── README.md
├── LICENSE
├── service.log               # 运行日志（gitignore）
└── tunnel.log                # 隧道日志（gitignore）
```

## License

MIT
- `bl-llm-3`，用对象格式指定独立的 `base_url` 和 `api_key`，走不同 provider

`model_map` 的值支持两种格式：

**字符串**（用全局默认 base_url 和 api_key）：
```json
"bl-llm-1": "glm-5.2"
```

**对象**（指定独立 provider）：
```json
"bl-llm-3": {
  "model_id": "deepseek-chat",
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-another-key"
}
```

对象格式中 `base_url` 和 `api_key` 可省略，省略时回退到全局默认值。

## 关键注意事项
