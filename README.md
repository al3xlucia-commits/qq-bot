# QQ 机器人部署包（SnowLuma + AstrBot）

一台 Mac 上跑起来的 QQ AI 机器人：SnowLuma 负责「QQ ↔ OneBot」，AstrBot 负责「人格 + 模型 + 插件」，本机 Ollama 提供向量记忆。

> ⚠️ 这是一份**脱敏后的部署快照**。所有密钥、Token、密码哈希、聊天数据库、表情包图片、QQ 登录态都**不在**本仓库里，恢复时需要你自己填。

---

## 一、整体架构

```
腾讯服务器
   ↕  QQ 私有协议
Linux QQ（跑在 Docker 容器里，带 Xvfb + noVNC）
   ↕  hook 注入
SnowLuma（协议端，把 QQ 会话翻译成 OneBot v11）
   ↕  反向 WebSocket  ws://127.0.0.1:6199/ws
AstrBot（v4.29.1，人格 / DeepSeek / 插件 / 记忆）
   ↕  HTTP  http://127.0.0.1:11434
Ollama + bge-m3（本地向量模型，给 LivingMemory 用）
```

端口一览：

| 服务 | 端口 | 说明 |
|---|---|---|
| AstrBot 面板 | `15558`（每次启动可能变） | 由 AstrBot Launcher 随机分配 |
| AstrBot OneBot 反向 WS | `6199` | SnowLuma 连这里 |
| SnowLuma WebUI | `5099` | 面板（账号 admin） |
| SnowLuma noVNC | `6081` | 网页版 QQ 桌面，扫码用 |
| SnowLuma OneBot HTTP / WS | `3000` / `3001` | 备用正向接口 |
| Ollama | `11434` | 本地向量服务 |

---

## 二、目录结构

```
qq-bot-package/
├── README.md                 # 本文件
├── configs/
│   ├── cmd_config.example.json      # AstrBot 主配置（已脱敏）
│   ├── persona.md                   # 人格提示词（从数据库导出）
│   ├── plugins/*.json               # 各插件配置（已脱敏）
│   └── snowluma/
│       ├── onebot.example.json      # OneBot 网络配置（Token 已脱敏）
│       └── runtime.example.json     # SnowLuma 运行时配置
├── scripts/
│   ├── start-snowluma.sh     # 拉起 SnowLuma 容器
│   ├── status.sh             # 一键状态检查
│   └── com.alex.ollama.plist # Ollama 开机自启（LaunchAgent）
├── astrbot/                  # AstrBot 源码（v4.29.1，不含 data/）
└── plugins/                  # 第三方插件源码（不含数据）
```

运行时数据（**故意不打包**）：

| 路径 | 为什么排除 |
|---|---|
| `astrbot/data/` | 含聊天数据库、密码哈希、JWT 密钥、附件 |
| `plugins/*/data`、`plugin_data/` | 含群友聊天记录、表情包图片（约 300MB） |
| `venv/` | 657MB，可在本机重建 |
| `~/qq-bot/tools/ollama-models` | 1.2GB 模型文件，可重新 pull |
| Docker 数据卷 | QQ 登录态，删除后需重新扫码 |

---

## 三、从零恢复

### 1. 前置

- macOS（Apple Silicon 实测），Docker Desktop
- AstrBot 4.29.x：推荐用 **AstrBot Launcher**（社区版，比源码方式省心，它会自己管 Python 环境）
- Ollama：本仓库不包含二进制，按 [ollama.com](https://ollama.com) 或 `ollama-darwin.tgz` 安装

### 2. 启动 SnowLuma（协议端）

```bash
bash scripts/start-snowluma.sh
```

它会用官方镜像起容器，映射 `6081/5099/3000/3001`，并挂载三个数据卷（`qq-gateway-data`、`qq-client-config`、`qq-client-data`）。

首次启动后：

```bash
# noVNC 密码（只打印一次）
docker logs snowluma 2>&1 | grep -E "远程桌面密码|remote desktop password" | tail -1
# WebUI 初始密码
docker logs snowluma 2>&1 | grep "initial credentials" | tail -1
```

打开 <http://localhost:6081>，在 QQ 窗口里**手机扫码登录**。登录成功后 SnowLuma 会自动注入 hook 并启动 OneBot。

### 3. 装 AstrBot 并恢复配置

1. Launcher 里新建实例，安装 **4.29.1**；
2. 停止实例，把 `configs/cmd_config.example.json` 复制成实例的 `core/data/cmd_config.json`；
3. 把 `configs/plugins/*.json` 放到 `core/data/config/`；
4. 把 `plugins/` 下各目录复制到 `core/data/plugins/`；
5. 启动实例。

> 关键：主配置里 `platform` 段的 OneBot **反向 WS 端口必须是 6199**，`ws_reverse_token` 要和你填进 SnowLuma `onebot.json` 的 `accessToken` 一致。

### 4. SnowLuma 对接配置

编辑容器内 `/app/data/config/onebot.json`（参考 `configs/snowluma/onebot.example.json`），`wsClients` 增加一条：

```json
{
  "name": "astrbot",
  "enabled": true,
  "url": "ws://host.docker.internal:6199/ws",
  "role": "Universal",
  "accessToken": "<与 AstrBot 的 ws_reverse_token 一致>",
  "messageFormat": "array",
  "reportSelfMessage": false,
  "reconnectIntervalMs": 5000
}
```

### 5. 装 Ollama 并拉向量模型

```bash
ollama pull bge-m3          # 1.2GB，中文效果好
```

macOS 上建议用 `scripts/com.alex.ollama.plist` 做开机自启（改一下路径里的用户名）。

然后 AstrBot 面板 →「模型提供商」新增：

- 类型：`Ollama Embedding`
- `embedding_api_base`：`http://127.0.0.1:11434`
- `embedding_model`：`bge-m3`
- `embedding_dimensions`：`1024` ← **必填**，否则 LivingMemory 会报「无效的嵌入向量维度: 0」

最后在 LivingMemory 插件配置里把 `embedding_provider_id` 指向这个 provider。

### 6. 需要你自己填的密钥

| 位置 | 说明 |
|---|---|
| `cmd_config.json` → `provider[].key` | DeepSeek API Key（`sk-...`） |
| `cmd_config.json` → `dashboard.password / jwt_secret` | 面板密码（留空则首次启动随机生成） |
| `snowluma/onebot.json` → `accessToken` | 三个地方要一致：AstrBot 反向 WS Token、SnowLuma wsClient Token |
| QQ 登录 | 只能手机扫码，**无法脚本化** |

---

## 四、插件清单

| 插件 | 版本 | 状态 | 说明 |
|---|---|---|---|
| `astrbot_plugin_meme_manager` | 5.2.2 | ✅ 正常 | 表情包管理，2 个包 201 张图（图片不在仓库） |
| `astrbot_plugin_self_learning` | 4.3.1 | ✅ 正常 | 消息捕获 + 风格/黑话学习，自带面板 `:7833` |
| `astrbot_plugin_group_chat_plus` | 1.2.3 | ✅ 正常 | 群聊「读空气」增强 |
| `astrbot_plugin_proactive_chat` | 1.2.6 | ✅ 正常 | 主动消息，面板 `:4100` |
| `astrbot_plugin_livingmemory` | 2.7.1 | ⚠️ 需 embedding | 长期记忆，面板即「记忆/工作台」 |
| `astrbot_plugin_qzone` | 3.1.3 | ⚠️ 未登录 | QQ 空间，需填 Cookie |
| `astrbot_plugin_bilibanshi` | 1.2.1 | ❌ 不可用 | 见「已知问题」 |
| `astrbot_plugin_dynamic_life_state` | 0.3.1 | ⏸ 已停用 | 生活状态注入 |

> `astrbot_plugin_private_companion`（陪伴核心）因**没有 LICENSE 文件**（默认保留所有权利），未包含在公开仓库中。

---

## 五、已知问题

### 1. SnowLuma 的 hook 会「半死」

症状：**出站正常（机器人能发），入站全死（收不到任何消息）**，日志里看不到 `[Event]`，但 QQ 客户端窗口里消息在进。

原因：hook 绑在了一个已经退出的 QQ 进程上，新进程的登录事件没被捕获：

```
[Hook] login detected: PID=946739    ← 旧进程
[Hook] awaiting login: PID=27214     ← 实际在跑的进程，永远等不到
```

修复顺序（从轻到重）：

1. `docker exec snowluma supervisorctl restart snowluma` —— 重启协议进程，**不重启 QQ、不用扫码**；
2. 还不行就 `supervisorctl restart qq` —— 会掉登录，需要重新扫码。

### 2. Mac 睡眠是最大敌人

空闲 1 分钟睡眠会导致上面第 1 条。必须关掉：

```bash
sudo pmset -c sleep 0 displaysleep 15   # 插电时系统不休眠，屏幕照常关
```

### 3. `bilibanshi`（B站搬史）不可用

三个独立阻塞：

| 检查 | 结果 |
|---|---|
| 关键词搜索 | ✅ 正常（WBI 签名可用） |
| 取播放地址 | ❌ `/x/web-interface/view` 返回 **412**（风控） |
| 下载合并 | ❌ 宿主机没有 `ffmpeg` |
| 群推送 | ✅ 白名单已配好 |

已验证的修复方向：把 `view` 换成 **WBI 签名**的 `/x/web-interface/wbi/view`（实测 200，能拿到 `cid`），再接 `playurl`（实测返回 6 个视频流），最后装 ffmpeg 合并 DASH。不登录最高 480p。

### 4. LivingMemory 必须先配 embedding

否则插件初始化 30 秒超时，面板「工作台」显示 **初始化失败**。日志特征：

```
等待 Provider 就绪（未就绪: Embedding Provider）...
等待插件初始化超时（30.0秒）
```

---

## 六、日常运维

```bash
bash scripts/status.sh              # 端口 / 容器 / QQ 在线状态
docker logs -f snowluma             # 协议端日志
docker exec snowluma sh -c "ls -t /app/data/logs/*.log | head -1"   # 当天日志文件
```

> 容器时区是 **UTC+8**，日志文件也按 UTC+8 切分，排查时注意换算（美东 = UTC+8 减 12 小时）。

AstrBot 日志在面板「控制台」里看；面板地址每次启动由 Launcher 随机分配，从 App 里点「打开 WebUI」最稳。

---

## 七、安全提示

- `admins_id` 默认为空：**不要把随便一个 QQ 设为管理员**，管理员策略默认是 `filesystem_scope: host + allow_execution: true`，等于给出整机 shell；
- SnowLuma / OneBot / 面板端口只监听本机，**不要暴露公网**；
- 第三方协议端（SnowLuma）属于非官方实现，有被风控/限制登录的风险，建议只用小号；
- 本仓库已对个人信息脱敏：QQ 号、群号、主人昵称在配置与人格文本中均替换为 `<YOUR_QQ>` / `<GROUP_ID_*>` / `主人` 等占位符；
- 表情包插件、陪伴插件都会往提示词里注入内容，插件装多了会互相抢话、token 翻倍，建议按需启用。

---

## 八、版本快照

| 组件 | 版本 |
|---|---|
| AstrBot | 4.29.1 |
| SnowLuma | latest（2026-10 镜像） |
| LivingMemory | 2.7.1 |
| Ollama | 0.40.9 |
| bge-m3 | latest（1024 维） |
| 打包时间 | 2026-10-24 |
