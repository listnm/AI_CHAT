# AI Chat - 智能对话中转站

基于 Flask 的 AI 对话代理应用，支持多 API 账号管理、自动测速、最快账号自动切换，并提供 OpenAI 兼容的 API 转发接口。

## 功能特性

- **多账号管理**：添加多个 API 中转站，统一管理
- **自动测速**：轻量模式不消耗 Token，严格模式真实对话测速
- **API 转发**：提供 OpenAI 兼容接口，可接入任意 AI 工具
- **三种调用模式**：自动选择 / 手动指定 / model=auto
- **流式对话**：支持 SSE 流式响应，中文字符编码正确
- **对话管理**：多会话隔离，历史对话保存到后端数据库
- **图片上传**：支持图片识别（多模态模型）
- **安全加密**：API Key 加密存储
- **浅色 / 深色双主题**：一键切换浅色（紫色调）与深色（青绿科技风）模式，整页适配，偏好自动记忆
- **Sub2API / NewAPI 账号池管理**：记录账号密码、一键查询余额、读取密钥/渠道分组详情，支持批量刷新
- **响应式设计**：电脑端表格 + 手机端卡片，自适应布局
- **后台对话管理**：查看对话内容预览、搜索、展开详情、单条/批量删除

## 快速部署

### 方式一：Render 部署（推荐）

1. Fork 本仓库到你的 GitHub
2. 在 [Render](https://render.com) 创建 Web Service，连接 GitHub 仓库
3. 环境变量配置：

   | 变量名 | 说明 | 默认值 |
   |--------|------|--------|
   | `ADMIN_PASSWORD` | 后台管理密码 | `admin123` |
   | `PROXY_API_KEY` | API 转发密钥 | 自动生成 |
   | `DATABASE_URL` | PostgreSQL 连接（Render 自动注入） | 本地 SQLite |
   | `ENCRYPTION_KEY` | API Key 加密密钥 | 自动生成 |
   | `FLASK_SECRET_KEY` | Session 密钥 | 默认值 |

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

#### 添加 API 账号

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
| ⭐ 设为默认 | 设为默认中转站（默认账号不可取消，只能切换到其他账号） |
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

#### 对话列表管理

后台底部「对话列表」区域显示所有用户对话：

| 功能 | 说明 |
|------|------|
| 🔍 搜索 | 按对话标题或预览内容过滤 |
| 👁 详情 | 点击展开完整对话消息（气泡视图） |
| 🗑 删除 | 删除单条对话（带二次确认） |
| 🗑 清空全部 | 一键清空所有对话（双重确认防误删） |

对话列表显示：头像 + 标题 + `[我]`/`[AI]` 彩色标签 + 最后消息预览 + 消息数 + 创建/更新时间。

### 2. 对话页面

访问首页 `/` 即可使用 AI 对话：

- **选择账号**：在顶部下拉框选择已添加的 API 中转站
- **选择模型**：选择该账号支持的模型
- **开始对话**：输入消息发送即可，无需先创建对话
- **图片识别**：点击上传按钮发送图片（需多模态模型支持）
- **对话管理**：左侧栏创建、切换、删除对话，各对话内容相互隔离
- **实时同步**：对话自动同步到后端数据库，后台可见
- **Markdown 渲染**：AI 回复支持代码块、表格、列表、链接等格式
- **主题切换**：点击左侧导航栏或顶部的 🌙/☀️ 按钮，在浅色与深色模式间切换，选择会自动记忆，下次打开自动应用

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

> 💡 **安全提示**：账号密码与 Access Token 均通过 Fernet（或降级 base64）加密后存入数据库，加密密钥通过环境变量 `ENCRYPTION_KEY` 或本地 `.encryption_key` 文件管理。

### 4. API 转发接口

本应用提供 OpenAI 兼容的 API 转发端点，可将你的中转站 API 统一对外提供。

#### 端点

```
POST https://你的域名/v1/chat/completions
```

#### 认证

```
Authorization: Bearer 你的PROXY_API_KEY
```

> `PROXY_API_KEY` 可在后台管理页面查看，通过环境变量 `PROXY_API_KEY` 设置。

#### 三种使用方式

**方式一：自动选择（推荐）**

不传额外参数，系统自动选择默认账号或最快的可用账号。

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

将 `model` 设为 `auto`，系统自动选择最优账号和模型。适合在第三方 AI 工具中使用。

```bash
curl https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer sk-proxy-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

#### 在第三方 AI 工具中使用

以常见的 AI 客户端为例：

1. **API Base URL**：`https://你的域名/v1`
2. **API Key**：你的 `PROXY_API_KEY`
3. **模型**：填写 `auto`，或具体模型名如 `gpt-4o`

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

## 技术栈

- **后端**：Python / Flask / Gunicorn（超时 600s）
- **数据库**：SQLite（本地）/ PostgreSQL（生产），对话保留 30 天
- **前端**：原生 HTML / CSS / JavaScript，浅色/深色双主题可切换
- **加密**：cryptography (Fernet) / base64 降级
- **部署**：Render / 支持任意 Python 平台

## 项目结构

```
.
├── app.py              # Flask 主应用（路由、API、数据库）
├── templates/
│   ├── index.html      # 对话页面（浅色/深色双主题）
│   ├── admin.html      # 后台管理页面（浅色/深色双主题）
│   └── account-pool.html # Sub2API / NewAPI 账号池管理页
├── requirements.txt    # Python 依赖
├── Procfile            # 部署配置（Gunicorn --timeout 600）
└── README.md           # 本文件
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `ADMIN_PASSWORD` | 否 | 后台管理密码，默认 `admin123` |
| `PROXY_API_KEY` | 否 | API 转发密钥，默认根据管理密码自动生成 |
| `DATABASE_URL` | 否 | PostgreSQL 连接串，不设则用本地 SQLite |
| `ENCRYPTION_KEY` | 否 | API Key 加密密钥，不设则自动生成 |
| `FLASK_SECRET_KEY` | 否 | Session 密钥 |

## License

MIT
