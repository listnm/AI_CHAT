# AI Chat - 智能对话中转站

基于 Flask 的 AI 对话代理应用，支持多 API 账号管理、自动测速、最快账号自动切换，并提供 OpenAI 兼容的 API 转发接口。

## 功能特性

- **多账号管理**：添加多个 API 中转站，统一管理
- **自动测速**：一键测试所有账号延迟，自动选择最快的
- **API 转发**：提供 OpenAI 兼容接口，可接入任意 AI 工具
- **三种调用模式**：自动选择 / 手动指定 / model=auto
- **流式对话**：支持 SSE 流式响应
- **对话管理**：保存历史对话，支持多会话
- **图片上传**：支持图片识别（多模态模型）
- **安全加密**：API Key 加密存储
- **响应式后台**：电脑端表格 + 手机端卡片，自适应布局

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
gunicorn app:app
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

### 2. 对话页面

访问首页 `/` 即可使用 AI 对话：

- **选择账号**：在顶部下拉框选择已添加的 API 中转站
- **选择模型**：选择该账号支持的模型
- **开始对话**：输入消息发送即可
- **图片识别**：点击上传按钮发送图片（需多模态模型支持）
- **对话管理**：左侧栏创建、切换、删除对话

### 3. API 转发接口

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

支持流式（`stream: true`）和非流式响应。

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

- **后端**：Python / Flask / Gunicorn
- **数据库**：SQLite（本地）/ PostgreSQL（生产）
- **前端**：原生 HTML / CSS / JavaScript
- **加密**：cryptography (Fernet) / base64 降级
- **部署**：Render / 支持任意 Python 平台

## 项目结构

```
.
├── app.py              # Flask 主应用（路由、API、数据库）
├── templates/
│   ├── index.html      # 对话页面
│   └── admin.html      # 后台管理页面
├── requirements.txt    # Python 依赖
├── Procfile            # 部署配置（Gunicorn）
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
