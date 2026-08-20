# 中转站管理池

AI 中转站管理与转发工具，支持多中转站管理、一键测速、自动 failover、游乐场对话测试。

## 功能

- **中转站管理**：添加 / 编辑 / 删除中转站，加密存储 API Key
- **一键测速**：轻量模式调 `/models` 不消耗 token，不支持时自动回退同步对话
- **OpenAI 兼容转发**：`/v1/chat/completions`，自动选择默认 / 最快中转站，失败自动切换
- **游乐场**：独立对话测试页，流式输出 + Markdown 渲染
- **数据持久化**：Render 使用 PostgreSQL，本地使用 SQLite

## 页面结构

| 路由 | 页面 | 说明 |
|------|------|------|
| `/` | → 重定向到 `/admin` | |
| `/admin` | 管理后台 | 中转站管理、测速、转发密钥显示 |
| `/playground` | 游乐场 | 对话测试，选择中转站和模型聊天 |
| `/login` | 登录页 | 管理密码登录 |
| `/healthz` | 健康检查 | 返回 `{"ok":true,"status":"healthy"}` |

## 快速开始（本地）

```bash
# 安装依赖
pip install -r requirements.txt

# 启动
python app.py

# 浏览器打开 http://127.0.0.1:8765
```

默认管理密码：`admin123`

## 转发接口（OpenAI 兼容）

| 配置项 | 值 |
|--------|-----|
| API 地址 | `http://127.0.0.1:8765/v1` |
| API Key | 环境变量 `PROXY_API_KEY`（本地自动生成） |
| 模型 | `auto`（自动用默认中转站的默认模型），或具体模型名 |

### 请求约定

- 不传 `account`：默认中转站优先，失败自动切换下一个可用中转站（failover）
- 传 `"account": "中转站名称"`：只走该中转站
- `model` 为 `auto` 或留空：使用该中转站的默认模型
- 响应头带 `X-Provider-Name` / `X-Provider-Model`，标明实际使用的中转站

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer 转发密钥" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "你好"}]}'
```

## API 列表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/login` | 管理页登录 |
| GET | `/admin` | 管理后台页面 |
| GET | `/playground` | 游乐场页面 |
| GET | `/api/stations` | 中转站列表（Key 脱敏） |
| POST | `/api/stations` | 添加中转站 |
| PUT | `/api/stations/<id>` | 保存修改 |
| DELETE | `/api/stations/<id>` | 删除 |
| POST | `/api/stations/<id>/reveal` | 查看完整 Key |
| POST | `/api/stations/<id>/default` | 设为默认 |
| DELETE | `/api/stations/<id>/default` | 取消默认 |
| POST | `/api/stations/<id>/test` | 单站测速 |
| POST | `/api/test-all` | 全部并行测速 |
| POST | `/api/chat` | 游乐场对话（SSE 流式） |
| POST | `/v1/chat/completions` | OpenAI 兼容转发 |
| GET | `/v1/models` | 池内模型列表合并 |
| GET | `/healthz` | 健康检查 |

## Render 部署（Web Service + PostgreSQL）

### 1. 创建 Render PostgreSQL

1. Render 控制台点击 **New → PostgreSQL**
2. 创建数据库，选择与 Web Service 相同的 Region
3. 在数据库 **Info/Connections** 中复制 **Internal Database URL**

### 2. 创建 Web Service

| 配置项 | 值 |
|--------|-----|
| Runtime | Python |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 4 --timeout 600` |
| Health Check Path | `/healthz` |

### 3. 必填环境变量

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | Render PostgreSQL 的 Internal Database URL |
| `ADMIN_PASSWORD` | 管理页面密码，不能使用 `admin123` |
| `PROXY_API_KEY` | `/v1/*` 转发接口 Bearer 密钥 |
| `FLASK_SECRET_KEY` | Flask Session 签名密钥 |
| `ENCRYPTION_KEY` | Fernet 密钥，用于加密存储上游 API Key |
| `RENDER` | 设置为 `true` |
| `HOST` | 设置为 `0.0.0.0` |

生成密钥：

```bash
# PROXY_API_KEY 和 FLASK_SECRET_KEY：用 Render 的 Generate Value

# ENCRYPTION_KEY（必须是 Fernet 格式）：
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 4. 部署后检查

1. 打开 `https://你的服务名.onrender.com/healthz` → 应返回 `{"ok":true,"status":"healthy"}`
2. 访问根路径 → 自动跳转到管理后台，用 `ADMIN_PASSWORD` 登录
3. 确认已有站点配置或新增一个站点
4. 使用 `PROXY_API_KEY` 验证 `/v1/models` 和 `/v1/chat/completions`
5. 重启后再次检查，确认 PostgreSQL 持久化生效

### 5. 转发地址

部署到 Render 后，转发地址会自动适配为 `https://你的服务名.onrender.com/v1/chat/completions`，无需手动修改。在管理后台顶部和游乐场顶部均可看到并复制。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8765` | 监听端口 |
| `HOST` | `127.0.0.1` | 监听地址；Render 设为 `0.0.0.0` |
| `ADMIN_PASSWORD` | `admin123` | 管理页密码 |
| `PROXY_API_KEY` | 自动生成 | 转发接口的 Bearer 密钥 |
| `FLASK_SECRET_KEY` | 自动生成 | Flask Session 签名 |
| `ENCRYPTION_KEY` | 自动生成 | Key 加密密钥 |
| `DATABASE_URL` | 空 | Render 设为 PostgreSQL URL，本地用 SQLite |

## 项目结构

```
.
├── app.py                  # Flask 后端（路由 + 转发 + 管理 API）
├── static/
│   └── style.css           # 共享样式
├── templates/
│   ├── login.html          # 登录页
│   ├── admin.html          # 管理后台页
│   └── playground.html     # 游乐场页
├── requirements.txt        # 依赖
├── Procfile                # Render 启动命令
└── README.md
```
