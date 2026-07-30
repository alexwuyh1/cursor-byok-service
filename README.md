# cursor-byok-service

Cursor BYOK 代理服务。通过模型别名改写绕开 Cursor 内置模型名冲突，单代理路由多个 provider，自带 Web 控制台和公网隧道，开机自启。

## 快速开始

### 前置要求

- macOS（Linux 需自行替换 launchd 为 systemd）
- [Homebrew](https://brew.sh) 安装的 Python 3.10+ 和 cloudflared
- Cursor Pro 订阅（BYOK 需要付费版）
- 一个 Cloudflare 管理的域名（用于公网隧道）
- 一个 OpenAI 兼容的 API key（如阿里云百炼、DeepSeek 等）

```bash
brew install python@3.14 cloudflared
```

### 安装

```bash
git clone <repo-url> && cd cursor-byok-service
cp config.example.json config.json
# 编辑 config.json：填入 API key、域名、模型映射
./install.sh
```

`install.sh` 会交互式引导完成：Cloudflare 认证、创建隧道、DNS 路由、生成 launchd 配置并加载。

### 在 Cursor 中配置

1. Settings → Models → 打开 **OpenAI API Key**，填你的 API key
2. 打开 **Override OpenAI Base URL**，填 `https://cursor.your-domain.com/v1`
3. **Add Custom Model**，添加 config.json 里的模型别名（如 `bailian-glm-5.2`）
4. 选择该模型，开始对话

### Web 控制台

安装后访问 `http://127.0.0.1:8787/admin`，可视化查看运行状态、编辑配置、一键重启。仅本地可访问，公网请求被拒绝。

## 工作原理

```
Cursor IDE → Cursor 云端后端 → 公网隧道 → 本地代理 → 上游 Provider
                                  │
                          改写 model 别名
                          bailian-glm-5.2 → glm-5.2
```

Cursor BYOK 的请求从其云端后端发出，`localhost` 不可达，必须用公网隧道。代理在中间做模型名改写（别名 → 真实 model ID），然后转发给上游 provider。单进程同时管理代理、隧道和 Web 控制台，纯 Python 标准库实现，无需 pip/venv。

## 配置

`config.json` 是唯一配置源。编辑后重启服务即可生效，`cf-config.yml` 等下游配置由服务自动派生。

### 字段说明

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| `model_map` | 模型别名 → provider 真实 model ID 的映射 | `{"bailian-glm-5.2": "glm-5.2"}` |
| `bailian_base_url` | 默认上游 endpoint | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `bailian_api_key` | 默认 API key | `sk-xxx` |
| `hostname` | 公网访问域名 | `cursor.your-domain.com` |
| `listen_port` | 本地监听端口 | `8787` |
| `tunnel_name` | cloudflared 隧道名 | `bailian-proxy` |
| `cloudflared_bin` | cloudflared 路径 | `/opt/homebrew/bin/cloudflared` |
| `cf_credentials_dir` | 凭证目录（留空自动推导） | |
| `run_tunnel` | 是否启动隧道 | `true` |

### 多 Provider 路由

`model_map` 支持两种写法：

**字符串**（用全局默认地址和 key）：
```json
"bailian-glm-5.2": "glm-5.2"
```

**对象**（指定独立 provider）：
```json
"opencode-mimo-v2.5-free": {
  "model_id": "mimo-v2.5-free",
  "base_url": "https://opencode.ai/zen/v1",
  "api_key": "sk-another-key"
}
```

一个代理服务同时路由多个 provider，Cursor 里选不同模型名自动走不同后端。

## 日常管理

### Web 控制台（推荐）

访问 `http://127.0.0.1:8787/admin`：状态总览、模型管理、配置编辑、重启，全程可视化。

### 命令行

```bash
# 查看状态
launchctl list | grep cursor.byok

# 查看日志
tail -f service.log
tail -f tunnel.log

# 重启（改完 config.json 后执行）
launchctl unload ~/Library/LaunchAgents/com.cursor.byok.service.plist
launchctl load ~/Library/LaunchAgents/com.cursor.byok.service.plist
```

## 项目结构

```
cursor-byok-service/
├── cursor-byok-service.py   # 核心服务（代理 + 隧道 + 控制台 API）
├── admin.html               # Web 控制台前端
├── config.json              # 实际配置（gitignore，含 API key）
├── config.example.json      # 配置模板（安全，可提交）
├── install.sh               # 一键安装
├── cf-config.yml             # 自动生成（gitignore）
├── service.log              # 运行日志（gitignore）
└── tunnel.log               # 隧道日志（gitignore）
```

## 注意事项

- **模型别名加前缀即可**：如 `bailian-glm-5.2` 不会触发 Cursor 内置模型匹配，无需用无辨识度的名字。
- **localhost 不可达**：Cursor BYOK 请求从云端后端发出，必须用公网隧道。
- **Admin 仅本地**：控制台和 admin API 通过 Host 头校验，公网请求返回 403。
- **用 Homebrew Python**：`/opt/homebrew/bin/python3`，不要用 `/usr/bin/python3`（沙箱权限问题）。
- **config.json 不提交 git**：含 API key，已 gitignore，用 `config.example.json` 做模板。

## License

MIT
