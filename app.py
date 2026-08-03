"""
AI 对话网页应用 - Flask 后端
提供 /chat 流式接口，代理到用户指定的 OpenAI 兼容 API
支持多账号管理、自动测速、最快账号自动切换
"""

import os
import json
import time
import uuid
import base64
import hashlib
import sqlite3
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ai-chat-secret-key-change-in-production")

# Render 环境配置（HTTPS 代理修复 + 安全 cookie）
if os.environ.get("RENDER"):
    # 让 Flask 正确识别 HTTPS 代理
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.config["PREFERRED_URL_SCHEME"] = "https"
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ================================================================
#  数据库（支持 SQLite 本地开发 / PostgreSQL Render 生产）
# ================================================================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
_accounts_lock = threading.Lock()
_USE_PG = bool(DATABASE_URL)


class _DBConnection:
    """统一的数据库连接包装，兼容 SQLite 和 PostgreSQL"""

    def __init__(self):
        self._using_pg = False
        if _USE_PG:
            try:
                import psycopg2
                import psycopg2.extras
                self._conn = psycopg2.connect(DATABASE_URL)
                self._conn.autocommit = False
                self._extras = psycopg2.extras
                self._using_pg = True
            except Exception as e:
                print(f"[WARN] PostgreSQL 连接失败 ({e})，回退到 SQLite")
                self._fallback_to_sqlite()
        else:
            self._fallback_to_sqlite()

    def _fallback_to_sqlite(self):
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=None):
        if self._using_pg:
            sql = sql.replace("?", "%s")
            cur = self._conn.cursor(cursor_factory=self._extras.RealDictCursor)
            if params is not None:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return cur
        else:
            if params is not None:
                return self._conn.execute(sql, params)
            else:
                return self._conn.execute(sql)

    def executescript(self, sql):
        if self._using_pg:
            cur = self._conn.cursor()
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            cur.close()
        else:
            self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _get_db():
    """获取数据库连接"""
    return _DBConnection()


def _init_db():
    """初始化数据库表结构"""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            api_url TEXT NOT NULL,
            api_key_encrypted TEXT NOT NULL,
            model TEXT NOT NULL,
            latency_ms INTEGER,
            last_speed_test TEXT,
            is_default INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '新对话',
            messages TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    # 兼容旧表：添加 is_default 列（如果不存在）
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在
    conn.commit()
    conn.close()


# ================================================================
#  API Key 加密 / 解密
# ================================================================

try:
    from cryptography.fernet import Fernet as _Fernet

    def _get_encryption_key() -> bytes:
        """获取加密密钥：优先从环境变量，否则从文件读取或生成"""
        env_key = os.environ.get("ENCRYPTION_KEY")
        if env_key:
            return env_key.encode()
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".encryption_key")
        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                return f.read()
        key = _Fernet.generate_key()
        with open(key_file, "wb") as f:
            f.write(key)
        return key

    _cipher = _Fernet(_get_encryption_key())

    def _encrypt(text: str) -> str:
        return _cipher.encrypt(text.encode()).decode()

    def _decrypt(text: str) -> str:
        return _cipher.decrypt(text.encode()).decode()

except Exception as _e:
    # 加密初始化失败（如 ENCRYPTION_KEY 格式不对），降级为 base64
    print(f"[WARN] 加密初始化失败 ({_e})，降级为 base64 编码")
    import base64 as _base64

    def _encrypt(text: str) -> str:
        return _base64.b64encode(text.encode()).decode()

    def _decrypt(text: str) -> str:
        return _base64.b64decode(text.encode()).decode()


# ================================================================
#  账号存储（SQLite + 加密）
# ================================================================


def _load_accounts() -> list:
    """从数据库加载所有账号"""
    conn = _get_db()
    try:
        rows = conn.execute("SELECT * FROM accounts ORDER BY is_default DESC, latency_ms ASC").fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "name": row["name"],
                "api_url": row["api_url"],
                "api_key": _decrypt(row["api_key_encrypted"]),
                "model": row["model"],
                "latency_ms": row["latency_ms"],
                "last_speed_test": row["last_speed_test"],
                "is_default": bool(row["is_default"]),
            })
        return result
    finally:
        conn.close()


def _save_accounts(accounts: list):
    """全量保存账号列表到数据库"""
    conn = _get_db()
    try:
        conn.execute("DELETE FROM accounts")
        for acc in accounts:
            conn.execute(
                "INSERT INTO accounts (id, name, api_url, api_key_encrypted, model, latency_ms, last_speed_test, is_default) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (acc["id"], acc["name"], acc["api_url"], _encrypt(acc["api_key"]), acc["model"], acc.get("latency_ms"), acc.get("last_speed_test"), 1 if acc.get("is_default") else 0)
            )
        conn.commit()
    finally:
        conn.close()


def _find_account(account_id: str) -> dict | None:
    """按 ID 查找账号（直接查数据库，更高效）"""
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "api_url": row["api_url"],
            "api_key": _decrypt(row["api_key_encrypted"]),
            "model": row["model"],
            "latency_ms": row["latency_ms"],
            "last_speed_test": row["last_speed_test"],
            "is_default": bool(row["is_default"]),
        }
    finally:
        conn.close()


# ================================================================
#  URL 规范化
# ================================================================

def normalize_api_url(url: str) -> str:
    """
    自动补全 API URL 路径
    如果用户输入的是 base URL（如 https://aihub.top），
    自动追加 /v1/chat/completions
    """
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


# ================================================================
#  页面路由
# ================================================================

@app.route("/")
def index():
    """渲染主页面"""
    return render_template("index.html")


# ================================================================
#  管理后台
# ================================================================

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
# 转发 API 的 Key，用于 OpenAI 兼容端点认证
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "sk-proxy-" + hashlib.md5(ADMIN_PASSWORD.encode()).hexdigest()[:16])


@app.route("/admin", methods=["GET", "POST"])
@app.route("/admin/", methods=["GET", "POST"])
def admin():
    """管理后台：查看数据库中的账号和对话数据"""
    from flask import session as flask_session

    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            flask_session["admin_logged_in"] = True
        else:
            return render_template("admin.html", logged_in=False, error="密码错误")

    if not flask_session.get("admin_logged_in"):
        return render_template("admin.html", logged_in=False, error="")

    # 加载数据
    accounts = _load_accounts()
    safe_accounts = []
    for acc in accounts:
        safe_accounts.append({
            "id": acc["id"],
            "name": acc.get("name", ""),
            "api_url": acc.get("api_url", ""),
            "model": acc.get("model", ""),
            "api_key": acc.get("api_key", ""),
            "api_key_preview": acc["api_key"][:12] + "..." if acc.get("api_key") else "",
            "latency_ms": acc.get("latency_ms"),
            "last_speed_test": acc.get("last_speed_test"),
            "is_default": acc.get("is_default", False),
        })

    convs = _load_conversations()
    conv_summary = []
    for c in convs:
        conv_summary.append({
            "id": c["id"],
            "title": c.get("title", "新对话"),
            "msg_count": len(c.get("messages", [])),
            "created_at": c.get("created_at", ""),
            "updated_at": c.get("updated_at", ""),
        })

    # 找出最快延迟
    fastest_latency = None
    for acc in safe_accounts:
        if acc.get("latency_ms") is not None:
            if fastest_latency is None or acc["latency_ms"] < fastest_latency:
                fastest_latency = acc["latency_ms"]

    base_url = request.host_url.rstrip("/")
    proxy_url = base_url + "/v1/chat/completions"

    return render_template(
        "admin.html",
        logged_in=True,
        accounts=safe_accounts,
        conversations=conv_summary,
        base_url=base_url,
        proxy_url=proxy_url,
        proxy_models_url=proxy_url,
        proxy_api_key=PROXY_API_KEY,
        fastest_latency=fastest_latency,
    )


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    """退出登录"""
    from flask import session as flask_session
    flask_session.clear()
    return render_template("admin.html", logged_in=False, error="")


# ================================================================
#  账号管理 API
# ================================================================

def _require_login():
    """检查用户是否已登录（主页面或后台）"""
    from flask import session as flask_session
    if not flask_session.get("user_logged_in") and not flask_session.get("admin_logged_in"):
        return False
    return True


@app.route("/api/accounts", methods=["GET"])
def api_accounts_list():
    """获取所有账号列表（不返回 API Key 完整内容，仅显示前 8 位）"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    accounts = _load_accounts()
    # 脱敏返回，安全第一
    safe_list = []
    for acc in accounts:
        safe = {
            "id": acc["id"],
            "name": acc.get("name", ""),
            "api_url": acc.get("api_url", ""),
            "model": acc.get("model", ""),
            "latency_ms": acc.get("latency_ms", None),
            "last_speed_test": acc.get("last_speed_test", None),
            "api_key_preview": acc.get("api_key", "")[:8] + "..." if acc.get("api_key") else "",
        }
        safe_list.append(safe)
    return {"ok": True, "accounts": safe_list}


@app.route("/api/accounts", methods=["POST"])
def api_accounts_add():
    """添加一个新账号"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()

    if not name:
        return {"ok": False, "message": "请输入账号名称"}
    if not api_url:
        return {"ok": False, "message": "请输入 API 地址"}
    if not api_key:
        return {"ok": False, "message": "请输入 API Key"}
    if not model:
        return {"ok": False, "message": "请输入模型名称"}

    accounts = _load_accounts()
    new_account = {
        "id": str(uuid.uuid4()),
        "name": name,
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "latency_ms": None,
        "last_speed_test": None,
    }
    accounts.append(new_account)
    _save_accounts(accounts)

    return {"ok": True, "message": f"账号「{name}」添加成功", "account": new_account}


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_accounts_delete(account_id):
    """删除指定账号"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    accounts = _load_accounts()
    new_list = [a for a in accounts if a["id"] != account_id]
    if len(new_list) == len(accounts):
        return {"ok": False, "message": "账号不存在"}
    _save_accounts(new_list)
    return {"ok": True, "message": "账号已删除"}


@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_accounts_update(account_id):
    """修改指定账号（名称、URL、Key、模型）"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    accounts = _load_accounts()
    for acc in accounts:
        if acc["id"] == account_id:
            data = request.get_json(force=True)
            if "name" in data:
                acc["name"] = data["name"].strip()
            if "api_url" in data:
                acc["api_url"] = data["api_url"].strip()
            if "api_key" in data and data["api_key"].strip():
                acc["api_key"] = data["api_key"].strip()
            if "model" in data:
                acc["model"] = data["model"].strip()
            # 仅在修改核心信息（名称/地址/Key）时清除延迟记录
            if "name" in data or "api_url" in data or ("api_key" in data and data.get("api_key","").strip()):
                acc["latency_ms"] = None
                acc["last_speed_test"] = None
            # 允许单独更新延迟数据（来自测速）
            if "latency_ms" in data:
                acc["latency_ms"] = data["latency_ms"]
            if "last_speed_test" in data:
                acc["last_speed_test"] = data["last_speed_test"]
            _save_accounts(accounts)
            return {"ok": True, "message": "账号已更新"}
    return {"ok": False, "message": "账号不存在"}


@app.route("/api/accounts/<account_id>/set-default", methods=["POST"])
def api_accounts_set_default(account_id):
    """将指定账号设为默认中转站"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    accounts = _load_accounts()
    found = False
    for acc in accounts:
        if acc["id"] == account_id:
            acc["is_default"] = True
            found = True
        else:
            acc["is_default"] = False
    if not found:
        return {"ok": False, "message": "账号不存在"}
    _save_accounts(accounts)
    return {"ok": True, "message": "已设为默认中转站"}


@app.route("/api/accounts/speedtest", methods=["POST"])
def api_accounts_speedtest():
    """
    对所有账号进行测速（并行请求），返回按延迟排序的结果
    每个账号发送一个简短的 Chat 请求，记录响应时间
    """
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    accounts = _load_accounts()
    if not accounts:
        return {"ok": False, "accounts": [], "message": "没有可测试的账号，请先添加"}

    results = []
    threads = []

    def test_one(acc: dict):
        """测试单个账号的延迟"""
        url = normalize_api_url(acc["api_url"])
        headers = {
            "Authorization": f"Bearer {acc['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": acc["model"],
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 2,
            "stream": False,
        }
        start = time.time()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            elapsed_ms = int((time.time() - start) * 1000)
            latency = elapsed_ms
            success = True
        except Exception:
            latency = None
            success = False

        with _accounts_lock:
            acc["latency_ms"] = latency
            acc["last_speed_test"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            results.append({
                "id": acc["id"],
                "name": acc.get("name", ""),
                "latency_ms": latency,
                "success": success,
                "api_url": acc.get("api_url", ""),
                "model": acc.get("model", ""),
            })

    # 并行测试所有账号
    for acc in accounts:
        t = threading.Thread(target=test_one, args=(acc,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # 保存更新后的延迟数据
    _save_accounts(accounts)

    # 按延迟排序（成功且快的排前面，失败的排最后）
    results.sort(key=lambda r: (0 if r["success"] else 1, r["latency_ms"] if r["latency_ms"] is not None else 99999))

    return {"ok": True, "accounts": results}


# ================================================================
#  API 连通性测试
# ================================================================

@app.route("/api/check", methods=["POST"])
def api_check():
    """
    API 连通性测试接口
    支持 account_id（使用已保存账号）或直接传入 api_url / api_key / model
    """
    data = request.get_json(force=True)
    account_id = data.get("account_id")

    if account_id:
        account = _find_account(account_id)
        if not account:
            return {"ok": False, "message": "账号不存在"}
        api_url = normalize_api_url(account["api_url"])
        api_key = account["api_key"]
        model = account["model"]
    else:
        api_url = normalize_api_url(data.get("api_url", ""))
        api_key = data.get("api_key", "")
        model = data.get("model", "")

    if not api_url or not api_key or not model:
        return {"ok": False, "message": "请先完整填写 API 地址、Key 和模型名称"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
        "stream": False,
    }

    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "choices" in data and len(data["choices"]) > 0:
            return {"ok": True, "message": "连接成功，API 可用"}
        else:
            return {"ok": False, "message": f"响应格式异常：{json.dumps(data)[:200]}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "message": "连接超时，请检查 API 地址或网络"}
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        if status == 401:
            return {"ok": False, "message": "认证失败（401），请检查 API Key 是否正确"}
        elif status == 404:
            return {"ok": False, "message": "API 地址不存在（404），请检查地址是否正确"}
        else:
            body = e.response.text[:200]
            return {"ok": False, "message": f"HTTP {status}：{body}"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": "无法连接到服务器，请检查 API 地址或网络"}
    except Exception as e:
        return {"ok": False, "message": f"测试失败：{str(e)}"}


# ================================================================
#  获取模型列表
# ================================================================

@app.route("/api/models", methods=["POST"])
def api_models():
    """
    获取可用模型列表
    支持 account_id 或直接传入 api_url / api_key
    """
    data = request.get_json(force=True)
    account_id = data.get("account_id")

    if account_id:
        account = _find_account(account_id)
        if not account:
            return {"ok": False, "models": [], "message": "账号不存在"}
        api_url = account["api_url"]
        api_key = account["api_key"]
    else:
        api_url = data.get("api_url", "")
        api_key = data.get("api_key", "")

    if not api_url or not api_key:
        return {"ok": False, "models": [], "message": "请先填写 API 地址和 Key"}

    models_url = normalize_api_url(api_url).replace("/chat/completions", "/models")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(models_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        model_list = [m["id"] for m in data.get("data", []) if "id" in m]
        return {"ok": True, "models": model_list}
    except Exception as e:
        return {"ok": False, "models": [], "message": f"获取模型列表失败：{str(e)}"}


# ================================================================
#  文件上传
# ================================================================

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp",  # 图片
    "txt", "py", "js", "ts", "jsx", "tsx", "html", "css", "json", "xml", "yaml", "yml", "md", "csv",  # 文本
    "pdf", "doc", "docx",  # 文档
}

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    上传文件接口
    返回文件类型、内容（base64 图片或纯文本），供前端构造消息
    """
    if "file" not in request.files:
        return {"ok": False, "message": "未选择文件"}

    file = request.files["file"]
    if file.filename == "" or not allowed_file(file.filename):
        return {"ok": False, "message": "不支持的文件类型"}

    # 检查文件大小
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        return {"ok": False, "message": "文件超过 20MB 限制"}

    ext = file.filename.rsplit(".", 1)[1].lower()
    file_bytes = file.read()

    # 图片类型：返回 base64
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
        return {
            "ok": True,
            "type": "image",
            "filename": file.filename,
            "mime": mime,
            "data": b64,
        }

    # 文本类型：返回纯文本内容
    try:
        text = file_bytes.decode("utf-8")
        return {
            "ok": True,
            "type": "text",
            "filename": file.filename,
            "data": text[:50000],  # 限制 50000 字符
        }
    except UnicodeDecodeError:
        return {"ok": False, "message": "无法解析文件内容，请使用纯文本文件"}


# ================================================================
#  对话管理（SQLite 存储，每日自动清理旧对话）
# ================================================================


def _load_conversations() -> list:
    """从数据库加载所有对话"""
    conn = _get_db()
    try:
        rows = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "title": row["title"],
                "messages": json.loads(row["messages"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result
    finally:
        conn.close()


def _save_conversations(convs: list):
    """全量保存对话列表到数据库"""
    conn = _get_db()
    try:
        conn.execute("DELETE FROM conversations")
        for c in convs:
            conn.execute(
                "INSERT INTO conversations (id, title, messages, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (c["id"], c.get("title", "新对话"), json.dumps(c.get("messages", [])), c.get("created_at", ""), c.get("updated_at", ""))
            )
        conn.commit()
    finally:
        conn.close()


def _cleanup_old_conversations():
    """删除超过 24 小时的对话"""
    now = datetime.now()
    cutoff = (now - timedelta(hours=24)).isoformat()
    conn = _get_db()
    try:
        conn.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


@app.route("/api/conversations", methods=["GET"])
def api_conversations_list():
    """获取所有对话列表（不含消息内容，仅元信息）"""
    if not _require_login():
        return {"ok": False, "conversations": [], "message": "请先登录"}, 401
    _cleanup_old_conversations()
    convs = _load_conversations()
    # 按更新时间倒序
    convs.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    summary = [{"id": c["id"], "title": c.get("title", "新对话"), "created_at": c.get("created_at"), "updated_at": c.get("updated_at")} for c in convs]
    return {"ok": True, "conversations": summary}


@app.route("/api/conversations", methods=["POST"])
def api_conversations_create():
    """创建新对话"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    data = request.get_json(force=True) or {}
    title = data.get("title", "新对话")
    now = datetime.now().isoformat()
    conv = {
        "id": str(uuid.uuid4()),
        "title": title,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }
    convs = _load_conversations()
    convs.append(conv)
    _save_conversations(convs)
    return {"ok": True, "conversation": conv}


@app.route("/api/conversations/<conv_id>", methods=["GET"])
def api_conversations_get(conv_id):
    """获取单个对话的完整消息"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    convs = _load_conversations()
    for c in convs:
        if c["id"] == conv_id:
            return {"ok": True, "conversation": c}
    return {"ok": False, "message": "对话不存在"}


@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def api_conversations_delete(conv_id):
    """删除指定对话"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    convs = _load_conversations()
    new_list = [c for c in convs if c["id"] != conv_id]
    if len(new_list) == len(convs):
        return {"ok": False, "message": "对话不存在"}
    _save_conversations(new_list)
    return {"ok": True, "message": "对话已删除"}


@app.route("/api/conversations/<conv_id>/messages", methods=["PUT"])
def api_conversations_save_messages(conv_id):
    """保存对话的消息列表"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    data = request.get_json(force=True)
    messages = data.get("messages", [])
    convs = _load_conversations()
    for c in convs:
        if c["id"] == conv_id:
            c["messages"] = messages
            c["updated_at"] = datetime.now().isoformat()
            # 自动生成标题（取第一条用户消息的前 30 个字符）
            for m in messages:
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    c["title"] = m["content"][:30]
                    break
            _save_conversations(convs)
            return {"ok": True, "message": "已保存"}
    return {"ok": False, "message": "对话不存在"}


# ================================================================
#  流式对话接口
# ================================================================

@app.route("/chat", methods=["POST"])
def chat():
    """
    流式对话接口
    支持 account_id（使用已保存账号）或直接传入 api_url / api_key / model
    自动使用最快的可用账号（如果传入了 account_id="auto"）
    """
    data = request.get_json(force=True)

    account_id = data.get("account_id")

    # 处理 auto 模式：自动选择延迟最低的账号
    if account_id == "auto":
        accounts = _load_accounts()
        # 过滤出有延迟记录的可用账号
        valid = [a for a in accounts if a.get("latency_ms") is not None]
        if valid:
            valid.sort(key=lambda a: a["latency_ms"])
            account = valid[0]
        elif accounts:
            account = accounts[0]
        else:
            account = None
    elif account_id:
        account = _find_account(account_id)
    else:
        account = None

    if account:
        api_url = normalize_api_url(account["api_url"])
        api_key = account["api_key"]
        model = account["model"]
    else:
        api_url = normalize_api_url(data.get("api_url", ""))
        api_key = data.get("api_key", "")
        model = data.get("model", "")

    messages = data.get("messages", [])

    if not api_url or not api_key or not model or not messages:
        def error_gen():
            yield f"data: {json.dumps({'error': '缺少必要参数（api_url、api_key、model、messages）'})}\n\n"
            yield "data: [DONE]\n\n"
        return Response(
            stream_with_context(error_gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
    }

    try:
        upstream_response = requests.post(
            api_url, headers=headers, json=payload, stream=True, timeout=60,
        )
        upstream_response.raise_for_status()
        # 强制 UTF-8 编码，防止上游 SSE 未指定 charset 导致中文乱码
        upstream_response.encoding = "utf-8"
    except requests.exceptions.Timeout:
        def timeout_gen():
            yield f"data: {json.dumps({'error': '请求上游 API 超时，请检查网络或 API 地址'})}\n\n"
            yield "data: [DONE]\n\n"
        return Response(
            stream_with_context(timeout_gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except requests.exceptions.RequestException as e:
        err_msg = f"请求失败: {str(e)}"
        def error_gen():
            yield f"data: {json.dumps({'error': err_msg})}\n\n"
            yield "data: [DONE]\n\n"
        return Response(
            stream_with_context(error_gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def generate():
        buffer = ""
        for chunk in upstream_response.iter_content(chunk_size=None, decode_unicode=True):
            if not chunk:
                continue
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data: "):
                    data_content = line[6:]
                    if data_content.strip() == "[DONE]":
                        yield "data: [DONE]\n\n"
                        return
                    yield f"data: {data_content}\n\n"
                elif line.startswith("event: "):
                    yield f"{line}\n"
                elif line.startswith("id: "):
                    yield f"{line}\n"
                elif line.startswith("retry: "):
                    yield f"{line}\n"
        if buffer.strip():
            if buffer.startswith("data: "):
                data_content = buffer[6:]
                if data_content.strip() != "[DONE]":
                    yield f"data: {data_content}\n\n"
                else:
                    yield "data: [DONE]\n\n"
            else:
                yield f"data: {buffer}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


# ================================================================
#  OpenAI 兼容转发 API（可用在 ChatGPT、Cursor 等工具中）
# ================================================================


def _select_account(account_name: str = "") -> dict | None:
    """
    选择账号：
    - 指定 account_name → 按名称匹配
    - 不指定 → 优先使用被设为默认的账号，没有则选最快的
    """
    accounts = _load_accounts()
    if not accounts:
        return None

    if account_name:
        for a in accounts:
            if a.get("name", "").strip() == account_name.strip():
                return a

    # 优先使用默认账号
    for a in accounts:
        if a.get("is_default"):
            return a

    # 按延迟排序选最快的
    valid = [a for a in accounts if a.get("latency_ms") is not None]
    if valid:
        valid.sort(key=lambda a: a["latency_ms"])
        return valid[0]
    return accounts[0]


def _make_provider_info(account: dict) -> dict:
    """生成账号信息描述"""
    return {
        "name": account.get("name", ""),
        "api_url": account.get("api_url", ""),
        "model": account.get("model", ""),
        "latency_ms": account.get("latency_ms"),
    }


@app.route("/v1/chat/completions", methods=["POST"])
def proxy_chat_completions():
    """
    OpenAI 兼容的 API 转发端点
    认证方式：Authorization: Bearer {PROXY_API_KEY}

    使用方式：
    - 不传任何选择参数 → 使用默认中转站（可在后台设置），无默认则选最快的
    - 传 "account": "账号名称" → 手动指定使用哪个中转站
    - 传 "model": "auto" → 自动选择最优中转站（兼容各种 AI 工具）
    - 传 "model": "具体模型名" → 直接传给中转站，不干涉

    响应中会包含 _provider 字段说明实际使用的账号
    """
    # 认证
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + PROXY_API_KEY
    if auth != expected:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}, "ok": False}, 401

    data = request.get_json(force=True) or {}
    account_name = data.get("account", "")
    model = data.get("model", "")
    is_auto = model == "auto"
    messages = data.get("messages", [])
    stream = data.get("stream", False)
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 4096)

    if not messages:
        return {"error": {"message": "messages is required", "type": "invalid_request"}, "ok": False}, 400

    # 选择账号：指定 account 则用该账号，否则用默认/最快的
    account = _select_account(account_name) if not is_auto else _select_account()
    if not account:
        return {"error": {"message": "No available accounts in pool", "type": "server_error"}, "ok": False}, 503

    api_url = normalize_api_url(account["api_url"])
    api_key = account["api_key"]
    # model=auto 时使用账号配置的模型名，否则用请求的 model
    effective_model = account["model"] if is_auto else (model or account["model"])
    provider_info = _make_provider_info(account)
    provider_info["effective_model"] = effective_model

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": effective_model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        upstream = requests.post(
            api_url, headers=headers, json=payload, stream=stream, timeout=120,
        )
        upstream.raise_for_status()
        # 强制 UTF-8 编码，防止上游 SSE 未指定 charset 导致中文乱码
        upstream.encoding = "utf-8"
    except requests.exceptions.Timeout:
        return {"error": {"message": "Upstream API timeout", "type": "timeout"}, "ok": False}, 504
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        try:
            body = e.response.json()
            msg = body.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return {"error": {"message": msg, "type": "upstream_error", "code": status}, "ok": False}, status
    except requests.exceptions.RequestException as e:
        return {"error": {"message": str(e), "type": "connection_error"}, "ok": False}, 502

    if stream:
        # 流式响应
        def generate():
            # 先发送账号信息事件
            yield f"event: provider\ndata: {json.dumps(provider_info, ensure_ascii=False)}\n\n"
            # 再转发上游的 SSE 数据
            buffer = ""
            for chunk in upstream.iter_content(chunk_size=None, decode_unicode=True):
                if not chunk:
                    continue
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_content = line[6:]
                        if data_content.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        yield f"data: {data_content}\n\n"
                    elif line.startswith(":"):
                        continue
            if buffer.strip():
                if buffer.startswith("data: "):
                    dc = buffer[6:]
                    if dc.strip() != "[DONE]":
                        yield f"data: {dc}\n\n"
                    else:
                        yield "data: [DONE]\n\n"
                else:
                    yield f"data: {buffer}\n\n"
            yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )
    else:
        # 非流式响应
        try:
            resp_data = upstream.json()
            # 注入 _provider 信息
            resp_data["_provider"] = provider_info
            # 确保非流式响应也使用 UTF-8
            return app.response_class(
                response=json.dumps(resp_data, ensure_ascii=False),
                mimetype="application/json; charset=utf-8",
            )
        except Exception as e:
            return {"error": {"message": f"Failed to parse upstream response: {str(e)}", "type": "parse_error"}, "ok": False}, 502


def _migrate_old_data():
    """从旧 JSON 文件迁移数据到数据库（兼容升级）"""
    old_accounts_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
    old_conversations_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.json")

    # 迁移账号
    if os.path.exists(old_accounts_file):
        try:
            with open(old_accounts_file, "r", encoding="utf-8") as f:
                old_accounts = json.load(f)
            if isinstance(old_accounts, list) and old_accounts:
                existing = _load_accounts()
                if not existing:
                    _save_accounts(old_accounts)
                    print(f"已迁移 {len(old_accounts)} 个账号从 accounts.json 到数据库")
            os.rename(old_accounts_file, old_accounts_file + ".bak")
        except Exception as e:
            print(f"迁移 accounts.json 失败: {e}")

    # 迁移对话
    if os.path.exists(old_conversations_file):
        try:
            with open(old_conversations_file, "r", encoding="utf-8") as f:
                old_convs = json.load(f)
            if isinstance(old_convs, list) and old_convs:
                existing = _load_conversations()
                if not existing:
                    _save_conversations(old_convs)
                    print(f"已迁移 {len(old_convs)} 个对话从 conversations.json 到数据库")
            os.rename(old_conversations_file, old_conversations_file + ".bak")
        except Exception as e:
            print(f"迁移 conversations.json 失败: {e}")


# 在模块加载时初始化数据库（支持 gunicorn）
try:
    _init_db()
    _migrate_old_data()
except Exception as _e:
    print(f"[ERROR] 数据库初始化失败: {_e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)