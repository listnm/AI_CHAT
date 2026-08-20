# 中转站管理池 · 本地客户端

一个纯本地的 AI 中转站管理工具，桌面客户端形态（pywebview 原生窗口），浅绿色科技风卡片式界面。

功能：**中转站管理 + Key 加密存储 + 一键测速 + 模型选择 + OpenAI 兼容转发 + 游乐场对话**。

- 后端为单文件 Flask（`app.py`），前端为卡片式单页界面（`templates/index.html`）
- 数据存本地 SQLite（`data.db`），API Key 用 Fernet 加密（密钥在 `.encryption_key`）
- 默认只监听 `127.0.0.1`，其他设备无法访问
- 双击 `start.bat` 即弹出客户端窗口；也可用网页模式（见下）

## 快速开始

1. 双击 `start.bat`（首次运行会自动创建 `.venv`、安装依赖并弹出客户端窗口）
2. 在客户端登录页输入管理密码（默认 `admin123`）
3. 点「＋ 添加中转站」，填写名称、Base URL（如 `https://api.example.com/v1`）、API Key
4. 点卡片上的「⚡ 测速」或「🚀 全部测速」——测速会自动拉取模型列表
5. 在卡片「模型」下拉框里选择该站的默认模型（转发和游乐场 `model=auto` 时使用）

**网页模式**（浏览器访问）：命令行运行 `start.bat web`，或直接：

```
.venv\Scripts\python.exe app.py
```

然后浏览器打开 `http://127.0.0.1:8765`。

## 游乐场

客户端顶部「🎮 游乐场」标签，仿 new-api 游乐场的对话测试页：

- 左侧参数面板：选择中转站 → 自动带出该站模型下拉；可填系统提示词、调温度（0~2）、限最大 Token
- 右侧对话区：流式输出（打字机效果 + 闪烁光标）、Markdown 渲染（标题/代码块/列表/引用/链接/行内样式）
- Enter 发送、Shift+Enter 换行；「🗑 清空对话」重置上下文
- 对话直接走所选中转站（管理会话认证，不消耗转发密钥），模型留空则用该站的默认模型

## 转发接口（OpenAI 兼容）

在任意 OpenAI 兼容客户端（ChatBox / NextChat / Cursor / API 工具）中：

| 配置项 | 值 |
|--------|-----|
| API 地址 | `http://127.0.0.1:8765/v1` |
| API Key | 客户端顶部的「转发密钥」（默认 `sk-local-0192023a7bbd7325`） |
| 模型 | `auto`（自动用默认中转站的默认模型），或具体模型名 |

约定：

- 不传 `account`：默认中转站优先，失败自动切换下一个可用中转站（failover）
- 传 `"account": "中转站名称"`：只走该中转站
- `model` 为 `auto` 或留空：使用该中转站在卡片上选择的「默认模型」（未选则用第一个模型；建议先测速，测速会自动拉取模型列表）
- 响应头带 `X-Provider-Name` / `X-Provider-Model`，标明实际使用的中转站

curl 示例：

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer 转发密钥" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "你好"}]}'
```

流式（`"stream": true`）同样支持，SSE 原样转发。

## 测速说明

测速为**轻量模式**：优先调 `GET /v1/models` 验证连通性，**不消耗 token**，并顺带刷新模型列表；某些中转站不实现 `/models`，会自动回退到 `stream=false + max_tokens=1` 的同步对话，同样基本不消耗 token。

## 配置（在 start.bat 顶部修改，或用环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8765` | 监听端口 |
| `HOST` | `127.0.0.1` | 监听地址；改成 `0.0.0.0` 可让局域网其他设备访问（需防火墙放行） |
| `ADMIN_PASSWORD` | `admin123` | 管理页密码 |
| `PROXY_API_KEY` | 自动生成 | 转发接口的 Bearer 密钥 |
| `ENCRYPTION_KEY` | 自动生成 | Key 加密密钥（对应 `.encryption_key` 文件） |

> ⚠️ **编码提醒**：`start.bat` 是 **GBK 编码**（内含中文提示，Windows 批处理在中文系统下按 GBK 解析）。
> 如果要用编辑器修改它，请保持 GBK/ANSI 编码保存（记事本选「ANSI」，VS Code 右下角把编码改为 GBK），
> 不要存成 UTF-8，否则再次运行时中文会变乱码。也可全部改用英文提示、另存为纯 ASCII。

> 💡 **桌面窗口依赖**：客户端窗口基于 pywebview + Edge WebView2（Win10/11 自带），首次启动依赖
> 缺失时会自动安装；如窗口无法打开，可改用网页模式，或手动执行 `.venv\Scripts\pip install pywebview`。

## EXE 分发版

已生成可直接分发的 Windows 单文件客户端：

```text
dist\中转站管理池.exe
```

把这个 EXE 复制给别人即可使用，不需要安装 Python 或项目源码。首次运行会弹出桌面客户端窗口，并在当前 Windows 用户目录创建：

```text
%LOCALAPPDATA%\中转站管理池\data.db
%LOCALAPPDATA%\中转站管理池\.encryption_key
```

这两个文件保存每台电脑自己的中转站配置和加密密钥，不会写入 EXE 所在目录。请不要把自己的 `data.db`、`.encryption_key` 或 `.venv` 一起发给别人，否则会泄露你保存的中转站配置和 API Key。

### 构建 EXE

在开发机双击：

```text
build_exe.bat
```

构建前需要当前项目的 `.venv` 和依赖。构建产物会覆盖 `dist\中转站管理池.exe`。

### 运行要求

- Windows 10/11，建议 x64
- 安装 Microsoft Edge WebView2 Runtime（大多数 Windows 10/11 已自带）
- 首次启动可能需要几秒钟解压和初始化
- 默认管理密码仍为 `admin123`，分发前建议通过环境变量或源码配置改掉默认凭据


|------|------|------|
| GET/POST | `/login` | 管理页登录 |
| GET | `/api/stations` | 中转站列表（Key 脱敏） |
| POST | `/api/stations` | 添加中转站 |
| PUT | `/api/stations/<id>` | 保存修改（Key 留空则不修改） |
| DELETE | `/api/stations/<id>` | 删除 |
| POST | `/api/stations/<id>/reveal` | 查看完整 Key（本地管理用） |
| POST | `/api/stations/<id>/default` | 设为默认 |
| DELETE | `/api/stations/<id>/default` | 取消默认 |
| POST | `/api/stations/<id>/test` | 单站测速 |
| POST | `/api/test-all` | 全部并行测速 |
| POST | `/v1/chat/completions` | OpenAI 兼容转发（Bearer 转发密钥） |
| GET | `/v1/models` | 池内模型列表合并（Bearer 转发密钥） |

## Render 部署（公网 Web Service）

本项目可以部署到 Render，但 Render 不能直接读取你电脑上的本地目录。请先把本目录（不含 `data.db`、`.encryption_key` 和 `.venv`）推送到 GitHub 或 GitLab，再在 Render 连接仓库。

项目已提供 `render.yaml`，可在 Render 选择 **New → Blueprint** 自动创建服务。若手动创建 Web Service，使用以下配置：

| 配置项 | 值 |
|--------|-----|
| Runtime | Python |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 4 --timeout 0 app:app` |
| Health Check Path | `/healthz` |
| Persistent Disk | 挂载到 `/var/data`，至少 1 GB |

### 必填环境变量

在 Render 的 Environment 中创建以下 Secret，所有密钥都应使用随机值并长期保持不变：

| 变量 | 说明 |
|------|------|
| `ADMIN_PASSWORD` | 管理页面密码，不能使用 `admin123` |
| `PROXY_API_KEY` | `/v1/*` 转发接口的 Bearer 密钥，不要由管理员密码派生 |
| `FLASK_SECRET_KEY` | Flask Session 签名密钥；更换后已有登录会话失效 |
| `ENCRYPTION_KEY` | Fernet 密钥；更换后已保存的上游 API Key 无法解密 |
| `RENDER` | 设置为 `true`（Blueprint 已配置） |
| `HOST` | 设置为 `0.0.0.0`（Blueprint 已配置） |
| `DATA_DIR` | 设置为 `/var/data`（Blueprint 已配置） |

可以使用 Python 生成 Fernet 密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Render Persistent Disk 与 SQLite 方案要求保持 **单实例、单 worker**；当前启动命令使用线程 worker 来支持流式响应。如果以后需要多实例、自动扩容或更高并发，应迁移到 PostgreSQL，而不是让多个实例共享 SQLite 文件。

### 部署后检查

1. 打开 `https://你的服务名.onrender.com/healthz`，应返回 `{"ok":true,"status":"healthy"}`。
2. 访问根路径，用 `ADMIN_PASSWORD` 登录管理页。
3. 添加一个中转站并执行测速，确认 `/var/data/data.db` 持久化。
4. 使用 `PROXY_API_KEY` 验证 `GET /v1/models` 和 `POST /v1/chat/completions`。
5. 确认服务重启后中转站配置仍在；不要把本地 `data.db` 或 `.encryption_key` 提交到仓库。


```
.
├── app.py              # Flask 单文件后端（含桌面模式 --desktop）
├── templates/index.html# 卡片式单页界面
├── requirements.txt    # 依赖（flask / requests / cryptography / pywebview）
├── start.bat           # Windows 一键启动（默认客户端，web 参数走网页）
└── README.md
```
