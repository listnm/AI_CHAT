# AI 中转站管理池

基于 Flask 的 AI 转发服务管理应用，支持多账号管理、自动测速、最快账号自动切换，提供兼容 OpenAI 的转发接口，并内置 new-api 风格的「游乐场」在线测试对话。

## 功能特性

- **多账号管理**：添加多个上游账号，统一管理
- **自动测速**：轻量模式不消耗 Token，严格模式真实对话测速
- **兼容转发**：提供兼容 OpenAI 的接口，可接入任意 AI 工具
- **三种调用模式**：自动选择 / 手动指定 / model=auto
- **游乐场测试对话**：参考 [new-api](https://www.newapi.ai/zh) 游乐场实现，选择账号 + 模型在线流式对话，支持参数面板、消息操作、本地历史
- **安全加密**：访问密钥加密存储
- **浅色 / 深色双主题**：一键切换浅色（紫色调）与深色（青绿科技风）模式，整页适配，偏好自动记忆
- **Sub2API / NewAPI 账号池管理**：记录账号密码、一键查询余额、读取密钥/渠道分组详情，支持批量刷新
- **响应式设计**：电脑端表格 + 手机端卡片，自适应布局
- **默认账号一键切换**：后台选择默认账号；未指定默认账号时自动使用延迟最低的账号

## 快速部署

### 方式一：Render 部署（推荐）

1. Fork 本仓库到你的 GitHub
2. 在 [Render](https://render.com) 创建 Web Service，连接 GitHub 仓库
3. 环境变量配置：

   | 变量名 | 说明 | 默认值 |
   |--------|------|--------|
   | `ADMIN_PASSWORD` | 后台管理密码；生产环境必须设置强随机值 | 无（生产必填） |
   | `PROXY_API_KEY` | API 转发密钥，必须与管理员密码独立 | 无（生产必填） |
   | `DATABASE_URL` | PostgreSQL 连接；Render 生产连接失败不会回退 SQLite | 无（生产必填） |
   | `ENCRYPTION_KEY` | Fernet 凭据加密密钥，必须固定保存 | 无（生产必填） |
   | `FLASK_SECRET_KEY` | Session 签名密钥 | 无（生产必填） |

4. 部署完成后访问 `https://你的域名/admin` 进入后台

### 方式二：本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动
python app.py
# 或
gunicorn app:app --timeout 600
```

访问 `http://localhost:5000`

## 使用说明

### 1. 后台管理

访问 `/admin`，输入管理密码登录（默认 `admin123`，请及时修改）。

#### 添加账号

1. 在「添加账号」区域填写：
   - **名称**：自定义标识，如「OpenAI中转」
   - **API 地址**：中转站的 API 地址，如 `https://api.example.com/v1/chat/completions`
   - **API Key**：中转站的密钥
   - **模型**：点击「刷新」按钮自动获取可用模型列表，或手动输入
2. 点击「添加账号」

#### 账号管理操作

每个已添加的账号支持以下操作：

| 操作 | 说明 |
|------|------|
| ⚡ 测速 | 测试该账号的响应延迟 |
| 💾 保存 | 修改账号信息后保存 |
| ⭐ 设为默认 / 取消默认 | 设置或取消默认中转站（无默认账号时，自动选择延迟最低的账号） |
| 🗑️ 删除 | 删除该账号 |
| 📋 复制地址 | 复制 API 地址到剪贴板 |
| 📋 复制 Key | 复制 API Key 到剪贴板 |
| 🔄 刷新 | 重新获取该账号的可用模型列表 |
| 🚀 全部测速 | 批量测试所有账号延迟 |

测速完成后，延迟最低的账号会自动高亮标记。

#### 测速模式

| 模式 | 说明 |
|------|------|
| **轻量模式**（默认） | 优先调 `/models` 接口验证，不消耗 Token；对不支持的中转站自动回退到同步对话，同样不消耗 Token |
| **严格测速** | 真实发送对话请求，消耗少量 Token，可验证完整链路 |

在「全部测速」按钮旁的开关切换模式。

### 2. 游乐场测试对话（new-api 风格）

访问 `/playground`（首页会自动跳转）即可使用游乐场在线测试对话，界面与交互参考 [new-api](https://www.newapi.ai/zh) 的游乐场（Playground）：

- **转发账号**：前台只显示并直接使用后台选择的默认账号；未设置默认账号时自动选择延迟最低的账号，**不暴露后台其他账号**；输入框左侧展示账号名、模型名称与延迟，账号配置变更后点刷新按钮即可生效
- **参数面板**：点击 ⚙️ 按钮展开参数面板，可分别启用 / 禁用 temperature、top_p、frequency_penalty、presence_penalty、max_tokens、seed，只有启用的参数才会随请求发送，角标显示已启用参数数量
- **流式对话**：SSE 流式输出，支持 Markdown 渲染、代码块、表格；支持思维链（reasoning_content / `<think>` 标签）折叠展示
- **消息操作**：悬停消息可复制、重新生成、编辑（用户消息编辑后自动截断后续消息并重新发送）、删除；错误消息可一键重试
- **本地历史**：会话、配置、参数开关保存在浏览器 localStorage（最多 100 条消息），刷新页面自动恢复；🗑 按钮一键清空（带二次确认）
- **主题切换**：右上角 🌙/☀️ 按钮切换浅色 / 深色模式，偏好自动记忆

### 3. 账号池管理（Sub2API / NewAPI）

访问 `/acc`（需先登录管理员后台），用于管理你的 Sub2API / NewAPI 平台账号池。

**主要功能：**

| 操作 | 说明 |
|------|------|
| ➕ 添加账号 | 选择 Sub2API 或 NewAPI 类型，填写 Base URL、用户名、密码（加密存储） |
| 🔐 登录 | 用账号密码登录远程平台，获取并保存 Access Token |
| 💰 查询余额 | 调用远程接口获取账户剩余额度，自动识别 token 失效并重登 |
| 🗂️ 读取分组 | 查询密钥令牌分组 / 上游渠道分组，按分组字段自动归类 |
| 👁 详情弹窗 | 展开每个分组的所有令牌/渠道，含 Key 预览、配额进度条 |
| 🔄 批量刷新 | 并行刷新全部账号的余额与分组数据 |
| 🔍 搜索过滤 | 按名称、URL、用户名、备注搜索账号 |
| 📊 统计概览 | 顶部 4 张卡片展示：账号总数 / 总余额 / Sub2API 数 / NewAPI 数 |

> 💡 **安全提示**：账号密码与 Access Token 均使用 Fernet 加密后存入数据库。生产环境必须设置固定的 `ENCRYPTION_KEY`；加密初始化失败会阻止服务启动，不会降级为 Base64。

### 4. 兼容转发接口

本应用提供兼容 OpenAI 的转发端点，可将已配置的上游账号统一对外提供。后台不再提供单独的“转发方式”切换，服务始终使用普通账号转发。

#### 端点

```
GET  https://你的域名/v1/models             # OpenAI 兼容模型列表
GET  https://你的域名/models                 # 兼容别名
POST https://你的域名/v1/chat/completions   # OpenAI 兼容 Chat Completions
POST https://你的域名/chat/completions       # 兼容别名
POST https://你的域名/v1/responses          # OpenAI Responses API（Codex CLI 等）
POST https://你的域名/responses              # 兼容别名
```

#### 认证

```
Authorization: Bearer 你的PROXY_API_KEY
```

> `PROXY_API_KEY` 可在后台管理页面查看，通过环境变量 `PROXY_API_KEY` 设置。

#### 三种使用方式

**方式一：自动选择（推荐）**

不传 `account` 时，系统使用后台选择的默认账号；未设置默认账号时自动选择延迟最低的可用账号。客户端传入的 `model` 会按 OpenAI/Sub2API 语义保留并转发，适合后台配置了模型映射或多个上游模型的场景；如果 `model` 缺省或设为 `auto`，才使用该账号后台配置的模型。

```bash
curl https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer sk-proxy-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

**方式二：手动指定账号**

通过 `account` 字段指定使用哪个中转站（名称对应后台添加的名称）。

```bash
curl https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer sk-proxy-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "account": "OpenAI中转",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

**方式三：model=auto（兼容 AI 工具）**

将 `model` 设为 `auto`，系统自动选择最优账号并使用该账号后台配置的模型。适合客户端必须传 model、但希望由后台决定实际模型的场景。

```bash
curl https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer sk-proxy-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

#### 方式四：Responses API（Codex CLI 等）

`/v1/responses` 端点按标准 OpenAI Responses API 直通到后台转发账号对应的上游 `/v1/responses`，请求体、非流式响应和流式 SSE 均不转换为 Chat Completions 格式。客户端可直接使用 Responses API 的 `input`、`instructions`、`tools`、`stream` 等字段。

**Codex CLI 配置示例（`~/.codex/config.toml`）：**

```toml
model_provider = "myproxy"

[model_providers.myproxy]
name = "myproxy"
base_url = "https://你的域名/v1"
env_key = "MYPROXY_API_KEY"
wire_api = "responses"   # 走 /v1/responses；也可设为 "chat" 走 /v1/chat/completions
```

在系统环境变量中设置 `MYPROXY_API_KEY` 为你的 `PROXY_API_KEY`，模型填 `auto` 即自动使用账号在后台配置的模型。

#### 在第三方 AI 工具中使用

以常见的 AI 客户端为例：

1. **API Base URL**：`https://你的域名/v1`
2. **API Key**：你的 `PROXY_API_KEY`
3. **模型**：填写 `auto`，或具体模型名如 `gpt-4o`

> 兼容 OpenAI 的客户端通常会自动请求 `GET /v1/models`。本应用只返回当前后台选择账号的模型，不会暴露其他账号。
>
> **地址填写注意**：客户端的 Base URL 请填写到 `/v1`，不要填写完整的 `/v1/chat/completions`。如果客户端要求填写完整接口地址，则使用 `https://你的域名/v1/chat/completions`；Responses API 客户端使用 `https://你的域名/v1/responses`。
>
> **WorkBuddy / Anthropic 客户端**：使用同一个 Base URL `https://你的域名/v1`，API Key 填 `PROXY_API_KEY`。客户端请求 `/v1/messages` 时，服务端会将 Anthropic Messages 请求转换为后台 OpenAI 兼容上游，并返回 Anthropic 标准响应与 SSE；不要把 Anthropic 请求发到 `/v1/chat/completions`。

支持流式（`stream: true`）和非流式响应，中文编码正常无乱码。

#### 响应格式

非流式响应兼容 OpenAI 格式，额外包含 `_provider` 字段说明实际使用的账号：

```json
{
  "id": "chatcmpl-xxx",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好！"}, "finish_reason": "stop"}],
  "model": "gpt-4o",
  "_provider": {
    "name": "OpenAI中转",
    "latency_ms": 234,
    "effective_model": "gpt-4o"
  }
}
```

### 5. 账号与转发说明

`/admin` 页面用于管理普通账号、测速并选择默认账号。应用保留兼容 OpenAI 的 `/v1/models`、`/v1/chat/completions`、Responses 和 Anthropic Messages 转发接口；废弃的 Grok SSO 账号管理页面与接口已移除。

历史数据库中的 `grok_accounts`、`grok_oauth_sessions`、`grok_device_sessions` 表不会自动删除。如需清理，请先备份数据库后单独执行迁移。

- **后端**：Python / Flask / Gunicorn（超时 600s）
- **数据库**：SQLite（本地）/ PostgreSQL（生产）
- **前端**：原生 HTML / CSS / JavaScript（游乐场页面复刻 new-api Playground 交互），浅色/深色双主题可切换
- **加密**：cryptography (Fernet)
- **部署**：Render / 支持任意 Python 平台

## 项目结构

```
.
├── app.py              # Flask 主应用（路由、API、数据库）
├── templates/
│   ├── playground.html # 游乐场测试对话页（new-api 风格，浅色/深色双主题）
│   ├── admin.html      # 后台管理页面（浅色/深色双主题）
│   └── account-pool.html # Sub2API / NewAPI 账号池管理页（浅色/深色双主题）
├── requirements.txt    # Python 依赖
├── Procfile            # 部署配置（Gunicorn --timeout 600）
└── README.md           # 本文件
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `ADMIN_PASSWORD` | 是（生产） | 后台管理密码，必须使用强随机值 |
| `PROXY_API_KEY` | 是（生产） | 独立的转发访问密钥，不由管理密码派生 |
| `DATABASE_URL` | 是（生产） | PostgreSQL 连接串；本地不设置时使用 SQLite |
| `ENCRYPTION_KEY` | 是（生产） | 固定的 Fernet 凭据加密密钥 |
| `FLASK_SECRET_KEY` | 是（生产） | Session 签名密钥 |

## License

MIT
