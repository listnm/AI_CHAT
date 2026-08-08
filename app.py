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
# 禁用模板缓存，开发阶段修改 templates 立刻生效
app.config['TEMPLATES_AUTO_RELOAD'] = True
try:
    app.jinja_env.auto_reload = True
except Exception:
    pass
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


# 账号池建表 SQL（独立抽取，便于运行时兜底建表）
_POOL_ACCOUNTS_DDL = """
    CREATE TABLE IF NOT EXISTS pool_accounts (
        id TEXT PRIMARY KEY,
        pool_type TEXT NOT NULL,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        username TEXT NOT NULL,
        password_encrypted TEXT NOT NULL,
        access_token_encrypted TEXT,
        balance REAL,
        balance_updated_at TEXT,
        groups_json TEXT DEFAULT '[]',
        groups_updated_at TEXT,
        remark TEXT,
        status TEXT DEFAULT 'active',
        selected_group TEXT,
        selected_token_id TEXT,
        selected_token_name TEXT,
        selected_token_key_encrypted TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""

_accounts_dll = """
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
"""

_conversations_dll = """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '新对话',
        messages TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""


_POOL_NEW_COLUMNS_DDL = [
    "ALTER TABLE pool_accounts ADD COLUMN selected_group TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN selected_token_id TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN selected_token_name TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN selected_token_key_encrypted TEXT",
]


def _ensure_pool_table(conn):
    """确保 pool_accounts 表存在 + 所有新列齐全（运行时兜底，防止 PG 多 worker 环境遗漏迁移）"""
    migrated = False
    try:
        conn.execute(_POOL_ACCOUNTS_DDL)
        migrated = True
    except Exception:
        # 表已存在类错误（PG 的 "relation already exists" 等），忽略，继续补列
        pass
    # 兼容旧 pool_accounts：补充新增字段（无论表是否新建都走一遍，已存在则 except 跳过）
    for col_sql in _POOL_NEW_COLUMNS_DDL:
        try:
            conn.execute(col_sql)
            migrated = True
        except Exception:
            pass  # 列已存在
    if migrated:
        try:
            conn.commit()
        except Exception:
            pass


def _init_db():
    """初始化数据库表结构"""
    conn = _get_db()
    try:
        try:
            conn.executescript(_accounts_dll + _conversations_dll)
        except Exception:
            pass
        _ensure_pool_table(conn)
        # 兼容旧 accounts 表：添加 is_default 列（如果不存在）
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # 列已存在
    finally:
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
    from flask import session as flask_session
    # 前端用户自动标记为已登录（仅限对话 API，不能访问 /admin）
    flask_session["user_logged_in"] = True
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
        msgs = c.get("messages", [])
        preview = ""
        if msgs:
            # 取最后一条消息作为预览（最多 100 字符）
            last = msgs[-1]
            content = last.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        content = part.get("text", "")
                        break
                else:
                    content = ""
            content = str(content).replace("\r", " ").replace("\n", " ").strip()
            preview = ("[AI] " if last.get("role") == "assistant" else "[我] ") + content[:100]
        conv_summary.append({
            "id": c["id"],
            "title": c.get("title", "新对话"),
            "msg_count": len(msgs),
            "preview": preview,
            "messages_json": json.dumps(msgs, ensure_ascii=False),
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
    ), 200, {"Cache-Control": "no-cache, no-store, must-revalidate"}


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
    mode: "light" (默认，调 /models 不消耗 token) 或 "strict" (真实对话，消耗少量 token)
    """
    data = request.get_json(force=True)
    account_id = data.get("account_id")
    mode = data.get("mode", "light")  # light | strict

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

    if not api_url or not api_key:
        return {"ok": False, "message": "请先填写 API 地址和 Key"}
    if mode == "strict" and not model:
        return {"ok": False, "message": "严格测速需要模型名称"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ===== 轻量模式：优先调 /models，失败或非预期格式则回退到同步对话 =====
    # 说明：某些中转站（如 sub2api）不实现 /v1/models，会把它 fallback 成流式
    # 对话，反而消耗 Token。回退用 stream=False 的同步对话，sub2api 等会识别为
    # "同步"模式不消耗 Token，且 max_tokens=1 几乎不产生实际计费。
    if mode != "strict":
        models_url = api_url.replace("/chat/completions", "/models")
        fallback_reason = None
        try:
            resp = requests.get(models_url, headers=headers, timeout=10)
            # 404/405 等说明上游不支持 /models，直接走回退
            if resp.status_code in (404, 405):
                fallback_reason = f"上游不支持 /models（HTTP {resp.status_code}）"
            else:
                resp.raise_for_status()
                data = resp.json()
                # 正常模型列表格式：{"object": "list", "data": [{"id": ...}]}
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    model_ids = [m.get("id", "") for m in data["data"] if "id" in m]
                    model_ok = True
                    if model and model_ids:
                        model_ok = model in model_ids
                    if model_ok:
                        msg = "连接成功（轻量模式 · /models，不消耗 Token）"
                        if model and model_ids and model not in model_ids:
                            msg += f"，但模型 {model} 不在可用列表中"
                        return {"ok": True, "message": msg, "mode": "light"}
                    else:
                        return {"ok": False, "message": f"模型 {model} 不在可用列表中（共 {len(model_ids)} 个模型）"}
                else:
                    # 返回的不是模型列表格式（可能是 sub2api 把 /models 当成对话处理了）
                    fallback_reason = "上游 /models 返回非模型列表格式"
        except requests.exceptions.Timeout:
            return {"ok": False, "message": "连接超时，请检查 API 地址或网络"}
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                return {"ok": False, "message": "认证失败（401），请检查 API Key"}
            elif status not in (404, 405):
                # 其他 HTTP 错误直接返回，不回退（如 401/500 等）
                return {"ok": False, "message": f"HTTP {status}：{e.response.text[:200]}"}
            fallback_reason = f"HTTP {status}"
        except requests.exceptions.ConnectionError:
            return {"ok": False, "message": "无法连接到服务器，请检查 API 地址或网络"}
        except Exception as e:
            fallback_reason = f"/models 异常：{str(e)[:80]}"

        # ===== 回退：同步对话验证（stream=False, max_tokens=1） =====
        # 对 sub2api 等不实现 /models 的中转站，这是最安全的验证方式：
        # - stream=False 被识别为"同步"模式，不消耗 Token
        # - max_tokens=1 即使计费也极少
        if not model:
            return {"ok": False, "message": "上游不支持 /models 且未配置模型，无法回退验证"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                msg = "连接成功（轻量模式 · 同步回退"
                if fallback_reason:
                    msg += f"：{fallback_reason}"
                msg += "，不消耗 Token）"
                return {"ok": True, "message": msg, "mode": "light_fallback"}
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
                return {"ok": False, "message": f"HTTP {status}：{e.response.text[:200]}"}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "message": "无法连接到服务器，请检查 API 地址或网络"}
        except Exception as e:
            return {"ok": False, "message": f"测试失败：{str(e)}"}

    # ===== 严格模式：真实对话，消耗少量 token =====
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
            return {"ok": True, "message": "连接成功（严格模式，已消耗少量 Token）", "mode": "strict"}
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
    """删除超过 30 天的对话"""
    now = datetime.now()
    cutoff = (now - timedelta(days=30)).isoformat()
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


@app.route("/api/conversations", methods=["DELETE"])
def api_conversations_delete_all():
    """清空全部对话"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    convs = _load_conversations()
    count = len(convs)
    _save_conversations([])
    return {"ok": True, "message": "所有对话已清空", "deleted_count": count}


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
        buffer = b""
        for chunk in upstream_response.iter_content(chunk_size=None):
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith(b":"):
                    continue
                if line.startswith(b"data: "):
                    data_content = line[6:]
                    if data_content.strip() == b"[DONE]":
                        yield "data: [DONE]\n\n"
                        return
                    yield b"data: " + data_content + b"\n\n"
                elif line.startswith(b"event: "):
                    yield line + b"\n"
                elif line.startswith(b"id: "):
                    yield line + b"\n"
                elif line.startswith(b"retry: "):
                    yield line + b"\n"
        if buffer.strip():
            if buffer.startswith(b"data: "):
                data_content = buffer[6:]
                if data_content.strip() != b"[DONE]":
                    yield b"data: " + data_content + b"\n\n"
                else:
                    yield "data: [DONE]\n\n"
            else:
                yield b"data: " + buffer + b"\n\n"
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
    # 透传客户端请求的参数（top_p/tools/response_format/frequency_penalty 等），
    # 但强制丢弃 max_tokens。原因：Trae/Cursor 等客户端默认会传 max_tokens=4096
    # 之类的小值，透传到上游后回复被截断在约 2000 中文字。丢弃后由上游模型按
    # 自身上限输出（如 GPT-4o 16K、Claude 8K）。
    _reserved = {"account", "model", "messages", "stream", "max_tokens"}
    payload = {k: v for k, v in data.items() if k not in _reserved}
    payload["model"] = effective_model
    payload["messages"] = messages
    payload["stream"] = stream

    try:
        # timeout 用元组 (connect_timeout, read_timeout)：
        # - 连接建立 30s 足够
        # - 单次读 chunk 600s（流式时相邻 chunk 之间的间隔通常很小，但长输出
        #   时上游可能间隔较久才推下一块）
        upstream = requests.post(
            api_url, headers=headers, json=payload, stream=stream, timeout=(30, 600),
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
        # 流式响应（严格 OpenAI 兼容：只输出 data: 行，不注入任何自定义事件，
        # 否则 Trae/Cursor 等客户端会把注入的内容误判为模型输出，导致工具调用
        # 解析失败、上下文被污染、模型显得"弱智"）
        def generate():
            # 直接透传原始字节，避免 decode_unicode 截断多字节 UTF-8 字符导致中文乱码
            buffer = b""
            chunk_count = 0
            byte_count = 0
            for chunk in upstream.iter_content(chunk_size=None):
                if not chunk:
                    continue
                chunk_count += 1
                byte_count += len(chunk)
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(b"data: "):
                        data_content = line[6:]
                        if data_content.strip() == b"[DONE]":
                            print(f"[proxy-stream] done: chunks={chunk_count} bytes={byte_count}", flush=True)
                            yield "data: [DONE]\n\n"
                            return
                        yield b"data: " + data_content + b"\n\n"
                    elif line.startswith(b":"):
                        continue
            # 循环正常结束（上游关闭连接但没发 [DONE]）
            print(f"[proxy-stream] upstream closed without [DONE]: chunks={chunk_count} bytes={byte_count} buf_tail={buffer[:80]!r}", flush=True)
            if buffer.strip():
                if buffer.startswith(b"data: "):
                    dc = buffer[6:]
                    if dc.strip() != b"[DONE]":
                        yield b"data: " + dc + b"\n\n"
                    else:
                        yield "data: [DONE]\n\n"
                else:
                    yield b"data: " + buffer + b"\n\n"
            yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                # 账号信息通过响应头透出，不污染 SSE 流
                "X-Provider-Name": provider_info.get("name", ""),
                "X-Provider-Model": provider_info.get("effective_model", ""),
            }
        )
    else:
        # 非流式响应（严格 OpenAI 兼容：不注入 _provider 等非标准字段，
        # 否则部分 SDK 严格校验会报错；信息改用响应头透出）
        try:
            resp_data = upstream.json()
            return app.response_class(
                response=json.dumps(resp_data, ensure_ascii=False),
                mimetype="application/json; charset=utf-8",
                headers={
                    "X-Provider-Name": provider_info.get("name", ""),
                    "X-Provider-Model": provider_info.get("effective_model", ""),
                }
            )
        except Exception as e:
            return {"error": {"message": f"Failed to parse upstream response: {str(e)}", "type": "parse_error"}, "ok": False}, 502


# ================================================================
#  Sub2API / NewAPI 账号池（SQLite 存储，密码加密）
# ================================================================


def _load_pool_accounts() -> list:
    """从数据库加载所有账号池账号"""
    conn = _get_db()
    try:
        _ensure_pool_table(conn)
        rows = conn.execute("SELECT * FROM pool_accounts ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            sel_key_enc = None
            try:
                sel_key_enc = row["selected_token_key_encrypted"]
            except (KeyError, IndexError):
                sel_key_enc = None
            result.append({
                "id": row["id"],
                "pool_type": row["pool_type"],
                "name": row["name"],
                "base_url": row["base_url"],
                "username": row["username"],
                "password": _decrypt(row["password_encrypted"]),
                "access_token": _decrypt(row["access_token_encrypted"]) if row["access_token_encrypted"] else None,
                "balance": row["balance"],
                "balance_updated_at": row["balance_updated_at"],
                "groups": json.loads(row["groups_json"]) if row["groups_json"] else [],
                "groups_updated_at": row["groups_updated_at"],
                "remark": row["remark"],
                "status": row["status"],
                "selected_group": row["selected_group"] if "selected_group" in row.keys() else None,
                "selected_token_id": row["selected_token_id"] if "selected_token_id" in row.keys() else None,
                "selected_token_name": row["selected_token_name"] if "selected_token_name" in row.keys() else None,
                "selected_token_key": _decrypt(sel_key_enc) if sel_key_enc else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result
    finally:
        conn.close()


def _find_pool_account(pool_id: str) -> dict | None:
    """按 ID 查找账号池账号"""
    conn = _get_db()
    try:
        _ensure_pool_table(conn)
        row = conn.execute("SELECT * FROM pool_accounts WHERE id = ?", (pool_id,)).fetchone()
        if row is None:
            return None
        sel_key_enc = None
        try:
            sel_key_enc = row["selected_token_key_encrypted"]
        except (KeyError, IndexError):
            sel_key_enc = None
        cols = set(row.keys())
        return {
            "id": row["id"],
            "pool_type": row["pool_type"],
            "name": row["name"],
            "base_url": row["base_url"],
            "username": row["username"],
            "password": _decrypt(row["password_encrypted"]),
            "access_token": _decrypt(row["access_token_encrypted"]) if row["access_token_encrypted"] else None,
            "balance": row["balance"],
            "balance_updated_at": row["balance_updated_at"],
            "groups": json.loads(row["groups_json"]) if row["groups_json"] else [],
            "groups_updated_at": row["groups_updated_at"],
            "remark": row["remark"],
            "status": row["status"],
            "selected_group": row["selected_group"] if "selected_group" in cols else None,
            "selected_token_id": row["selected_token_id"] if "selected_token_id" in cols else None,
            "selected_token_name": row["selected_token_name"] if "selected_token_name" in cols else None,
            "selected_token_key": _decrypt(sel_key_enc) if sel_key_enc else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()


def _upsert_pool_account(acc: dict):
    """新增或更新账号池账号"""
    conn = _get_db()
    now = datetime.now().isoformat()
    try:
        _ensure_pool_table(conn)
        existing = conn.execute("SELECT id FROM pool_accounts WHERE id = ?", (acc["id"],)).fetchone()
        sel_key_enc = _encrypt(acc["selected_token_key"]) if acc.get("selected_token_key") else None
        if existing:
            conn.execute(
                """UPDATE pool_accounts SET pool_type=?, name=?, base_url=?, username=?,
                   password_encrypted=?, access_token_encrypted=?, balance=?, balance_updated_at=?,
                   groups_json=?, groups_updated_at=?, remark=?, status=?,
                   selected_group=?, selected_token_id=?, selected_token_name=?, selected_token_key_encrypted=?,
                   updated_at=? WHERE id=?""",
                (
                    acc["pool_type"], acc["name"], acc["base_url"], acc["username"],
                    _encrypt(acc["password"]),
                    _encrypt(acc["access_token"]) if acc.get("access_token") else None,
                    acc.get("balance"), acc.get("balance_updated_at"),
                    json.dumps(acc.get("groups", []), ensure_ascii=False),
                    acc.get("groups_updated_at"),
                    acc.get("remark"), acc.get("status", "active"),
                    acc.get("selected_group"),
                    acc.get("selected_token_id"),
                    acc.get("selected_token_name"),
                    sel_key_enc,
                    now, acc["id"]
                )
            )
        else:
            conn.execute(
                """INSERT INTO pool_accounts (id, pool_type, name, base_url, username,
                   password_encrypted, access_token_encrypted, balance, balance_updated_at,
                   groups_json, groups_updated_at, remark, status,
                   selected_group, selected_token_id, selected_token_name, selected_token_key_encrypted,
                   created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    acc["id"], acc["pool_type"], acc["name"], acc["base_url"], acc["username"],
                    _encrypt(acc["password"]),
                    _encrypt(acc["access_token"]) if acc.get("access_token") else None,
                    acc.get("balance"), acc.get("balance_updated_at"),
                    json.dumps(acc.get("groups", []), ensure_ascii=False),
                    acc.get("groups_updated_at"),
                    acc.get("remark"), acc.get("status", "active"),
                    acc.get("selected_group"),
                    acc.get("selected_token_id"),
                    acc.get("selected_token_name"),
                    sel_key_enc,
                    now, now
                )
            )
        conn.commit()
    finally:
        conn.close()


def _delete_pool_account(pool_id: str) -> bool:
    """删除账号池账号"""
    conn = _get_db()
    try:
        _ensure_pool_table(conn)
        cur = conn.execute("DELETE FROM pool_accounts WHERE id = ?", (pool_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _update_pool_field(pool_id: str, **fields):
    """部分更新账号池字段"""
    conn = _get_db()
    now = datetime.now().isoformat()
    try:
        _ensure_pool_table(conn)
        sets = []
        params = []
        for k, v in fields.items():
            if k == "password":
                sets.append("password_encrypted = ?")
                params.append(_encrypt(v))
            elif k == "access_token":
                sets.append("access_token_encrypted = ?")
                params.append(_encrypt(v) if v else None)
            elif k == "groups":
                sets.append("groups_json = ?")
                params.append(json.dumps(v, ensure_ascii=False))
            elif k == "selected_token_key":
                sets.append("selected_token_key_encrypted = ?")
                params.append(_encrypt(v) if v else None)
            else:
                sets.append(f"{k} = ?")
                params.append(v)
        sets.append("updated_at = ?")
        params.append(now)
        params.append(pool_id)
        sql = f"UPDATE pool_accounts SET {', '.join(sets)} WHERE id = ?"
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ================================================================
#  Sub2API / NewAPI 接口调用（登录、余额、密钥分组）
# ================================================================


def _normalize_base_url(url: str) -> str:
    """规范化 base URL，去掉末尾斜杠"""
    return url.rstrip("/")


def _pool_login(pool_type: str, base_url: str, username: str, password: str) -> tuple[str | None, str]:
    """
    登录 sub2api / newapi 获取 access_token
    返回 (token, error_msg)
    """
    base = _normalize_base_url(base_url)
    # 尝试多种常见的登录端点
    endpoints = [
        f"{base}/api/v1/auth/login",
        f"{base}/api/user/login",
        f"{base}/api/auth/login",
        f"{base}/api/login",
        f"{base}/user/login",
    ]
    # payload 候选：不同平台对字段名、大小写、username/email 要求不同，都试一遍
    payloads = [
        {"username": username, "password": password},
        {"email": username, "password": password},
        {"Username": username, "Password": password},
        {"Email": username, "Password": password},
        {"email": username, "password": password, "username": username},
    ]
    last_error = ""
    for url in endpoints:
        for payload in payloads:
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        token = None
                        if isinstance(data, dict):
                            d = data.get("data", data)
                            if isinstance(d, dict):
                                token = d.get("token") or d.get("access_token") or d.get("Authorization") or d.get("Token") or d.get("AccessToken")
                            if not token:
                                token = data.get("token") or data.get("access_token") or data.get("Authorization") or data.get("Token") or data.get("AccessToken")
                        if token and isinstance(token, str):
                            if token.lower().startswith("bearer "):
                                token = token[7:]
                            return token, ""
                        last_error = f"登录成功但未解析到 token：响应={json.dumps(data)[:200]}"
                    except Exception as e:
                        last_error = f"登录响应解析失败：{e}"
                else:
                    body = resp.text[:150]
                    try:
                        jr = resp.json()
                        if isinstance(jr, dict):
                            m = jr.get("message") or jr.get("msg") or jr.get("error")
                            if m:
                                body = str(m)
                    except Exception:
                        pass
                    last_error = f"HTTP {resp.status_code}：{body}"
            except requests.exceptions.ConnectionError:
                last_error = "无法连接到服务器，请检查 API 地址或网络"
            except requests.exceptions.Timeout:
                last_error = "连接超时"
            except Exception as e:
                last_error = f"异常：{e}"
    return None, last_error or "所有登录端点均未返回有效 token"


def _pool_get_balance(pool_type: str, base_url: str, token: str) -> tuple[float | None, str]:
    """
    查询余额（sub2api / newapi）
    返回 (balance_value, error_msg)；余额单位通常为 USD 或 CNY，按上游原样返回
    """
    base = _normalize_base_url(base_url)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    endpoints = [
        f"{base}/api/v1/auth/me",
        f"{base}/api/v1/user/profile",
        f"{base}/api/user/self",
        f"{base}/api/user/info",
        f"{base}/api/me",
        f"{base}/user/self",
    ]
    last_error = ""
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        d = data.get("data", data)
                        if isinstance(d, dict):
                            # 常见字段：balance / quota / credits / available / remaining
                            for key in ("balance", "quota", "credits", "available", "remaining", "quota_remaining"):
                                v = d.get(key)
                                if isinstance(v, (int, float)):
                                    return float(v), ""
                                if isinstance(v, str) and v.replace(".", "", 1).isdigit():
                                    return float(v), ""
                            # 有些放在更深处
                            for key in ("user", "info"):
                                sub = d.get(key)
                                if isinstance(sub, dict):
                                    for k2 in ("balance", "quota", "credits", "available", "remaining"):
                                        v = sub.get(k2)
                                        if isinstance(v, (int, float)):
                                            return float(v), ""
                    last_error = f"未解析到余额字段：响应={json.dumps(data)[:300]}"
                except Exception as e:
                    last_error = f"响应解析失败：{e}"
            else:
                last_error = f"HTTP {resp.status_code}：{resp.text[:150]}"
        except Exception as e:
            last_error = f"异常：{e}"
    return None, last_error or "未能从任何端点获取余额信息"


def _pool_get_groups(pool_type: str, base_url: str, token: str) -> tuple[list | None, str]:
    """
    读取密钥/令牌分组（sub2api 的渠道分组、newapi 的令牌分组）
    返回 (groups_list, error_msg)
    """
    base = _normalize_base_url(base_url)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    groups = []

    # ==== 【关键】先拉「可用分组列表」（包含空分组！这才是 /keys 页面下拉的来源）====
    # 优先使用独立分组接口；后续合并到 all_group_options，确保下拉能列出所有分组（哪怕组里没 key）
    group_list_endpoints = [
        f"{base}/api/v1/groups/available",
        f"{base}/api/groups/available",
        f"{base}/api/v1/group/available",
        f"{base}/api/v1/user/groups",
        f"{base}/api/user/groups",
        f"{base}/api/v1/keys/groups/options",
    ]
    server_group_options = []  # [{id,name}]
    for url in group_list_endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                arr = None
                if isinstance(data, dict):
                    d = data.get("data")
                    if isinstance(d, list):
                        arr = d
                    elif isinstance(d, dict):
                        arr = d.get("items") or d.get("list") or d.get("groups")
                    if not arr:
                        arr = data.get("items") or data.get("list") or data.get("groups")
                elif isinstance(data, list):
                    arr = data
                if isinstance(arr, list) and arr:
                    out = []
                    for g in arr:
                        if not isinstance(g, dict):
                            continue
                        gid = g.get("id")
                        if gid is None or gid == "":
                            continue
                        gname = g.get("name") or g.get("title") or (f"分组 #{gid}" if isinstance(gid, (int, float)) else str(gid))
                        item = {"id": gid, "name": gname}
                        # 倍率字段：有的话一起带，供前端选项里显示
                        for fld, key in (("rate_multiplier", "rate_multiplier"),
                                         ("peak_rate_multiplier", "peak_rate_multiplier"),
                                         ("peak_rate_enabled", "peak_rate_enabled"),
                                         ("platform", "platform")):
                            if fld in g:
                                item[key] = g[fld]
                        out.append(item)
                    if out:
                        server_group_options = out
                        break
        except Exception:
            pass

    # ==== 再取令牌列表（sub2api/newapi 通用 /api/token 等） ====
    token_endpoints = [
        f"{base}/api/v1/keys",
        f"{base}/api/token",
        f"{base}/api/tokens",
        f"{base}/api/keys",
        f"{base}/api/user/tokens",
    ]
    token_list = []
    last_token_err = ""
    for url in token_endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=6, params={"p": 1, "page_size": 500})
            if resp.status_code == 200:
                data = resp.json()
                arr = None
                if isinstance(data, dict):
                    # aliuapi/sub2api 格式: {"code":0,"data":{"items":[...]}}
                    d = data.get("data")
                    if isinstance(d, dict):
                        arr = d.get("items") or d.get("list") or d.get("tokens") or d.get("keys")
                    elif isinstance(d, list):
                        arr = d
                    if not arr:
                        arr = data.get("items") or data.get("list") or data.get("tokens") or data.get("keys")
                elif isinstance(data, list):
                    arr = data
                if isinstance(arr, list):
                    token_list = arr
                    break
                last_token_err = f"令牌列表格式异常：{json.dumps(data)[:200]}"
            elif resp.status_code in (401, 403):
                last_token_err = f"HTTP {resp.status_code}（Token 可能失效）"
                # 401/403 就不用再试后面的 endpoint 了，一样会 401
                break
            else:
                last_token_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_token_err = f"异常：{e}"

    # 从令牌列表中提取分组信息
    if token_list:
        # 按 group / group_name / group_id / channel / tag 字段分组
        group_map: dict = {}
        # 收集全部分组选项（用于切换分组下拉）：group_id(原始) -> group_name
        group_opt_map: dict = {}
        for t in token_list:
            if not isinstance(t, dict):
                continue
            group_key = None
            # 提取原始 group_id（用于 PUT /api/v1/keys/{id} 切分组），优先级：group.id > group_id > group(字符串则无ID)
            group_val = t.get("group")
            raw_group_id = None
            raw_group_name = None
            if isinstance(group_val, dict):
                raw_group_id = group_val.get("id")
                raw_group_name = group_val.get("name") or group_val.get("title")
                group_key = raw_group_name or str(raw_group_id or "")
            elif group_val is not None and group_val != "":
                group_key = str(group_val)
            # 再检查其他分组字段
            if not group_key:
                for k in ("group_name", "group_id", "channel", "tag", "category", "type"):
                    v = t.get(k)
                    if v is not None and v != "":
                        if k == "group_id":
                            if raw_group_id is None:
                                raw_group_id = v
                            if isinstance(v, (int, float)):
                                group_key = f"分组 #{v}"
                            else:
                                group_key = str(v)
                        else:
                            group_key = str(v)
                        if k == "group_name" and raw_group_name is None:
                            raw_group_name = str(v)
                        break
            if not group_key:
                group_key = "默认分组"
            # 注册分组选项（用原始 group_id 做 key，缺省用 group_key）
            opt_key = raw_group_id if raw_group_id is not None and raw_group_id != "" else group_key
            if opt_key not in group_opt_map:
                group_opt_map[opt_key] = {
                    "id": raw_group_id if raw_group_id is not None else group_key,
                    "name": raw_group_name or group_key,
                }
            if group_key not in group_map:
                group_map[group_key] = {
                    "name": group_key,
                    "count": 0,
                    "tokens": [],
                }
            entry = {
                "id": t.get("id"),
                "name": t.get("name") or t.get("remark") or t.get("title") or "",
                "key_preview": (str(t.get("key") or t.get("token") or "")[:16] + "...") if (t.get("key") or t.get("token")) else "",
                "key": t.get("key") or t.get("token") or "",  # 完整密钥，供前端复制
                "status": t.get("status") or t.get("enabled") or "active",
                "created_at": t.get("created_at") or t.get("createdTime") or "",
                "expired_at": t.get("expired_at") or t.get("expireTime") or t.get("expires_at") or "",
                "used_quota": t.get("used_quota") or t.get("used") or t.get("consume") or t.get("quota_used") or 0,
                "remaining_quota": t.get("remaining_quota") or t.get("remaining") or t.get("quota") or 0,
                # 保存原始 group_id，供切换分组 API 传参
                "_group_id": raw_group_id if raw_group_id is not None else group_key,
                "_group_name": raw_group_name or group_key,
            }
            group_map[group_key]["tokens"].append(entry)
            group_map[group_key]["count"] += 1
        groups = list(group_map.values())

        # 把每个分组块自身挂上倍率/平台信息（给分组标题旁显示）
        # 优先用 server_group_options（独立接口拉的，一定有完整 rate_multiplier），其次用单条 key 里 group dict 的 rate_multiplier
        def _merge_rate_to_group(g_block, src_dict):
            if not isinstance(src_dict, dict):
                return
            for fld in ("rate_multiplier", "peak_rate_multiplier", "peak_rate_enabled",
                        "peak_start", "peak_end", "platform", "status", "description"):
                if fld in src_dict and fld not in g_block:
                    g_block[fld] = src_dict[fld]
        # 先建 id→server_group 的索引
        srv_by_id: dict = {}
        for o in server_group_options:
            iid = o.get("id")
            if iid is not None and iid != "":
                srv_by_id[str(iid)] = o
        # 给每个 keys 分组块补元信息
        for g in groups:
            if g.get("is_channel_group"):
                continue
            # 按分组名匹配（老系统无group_id场景兜底）
            matched = None
            # 取该分组第一个 token 的 _group_id 来匹配
            first_tk = (g.get("tokens") or [None])[0]
            gid_candidates = []
            if isinstance(first_tk, dict) and first_tk.get("_group_id") is not None:
                gid_candidates.append(str(first_tk["_group_id"]))
            # 也按名字找一次（大小写/空白敏感，命中就算）
            name2srvs = {s.get("name", ""): s for s in server_group_options}
            if g.get("name") in name2srvs:
                matched = name2srvs[g["name"]]
            # 再按 id 找
            for cid in gid_candidates:
                if cid in srv_by_id:
                    matched = srv_by_id[cid]
                    break
            if matched:
                _merge_rate_to_group(g, matched)
            # 再用第一个 token 的原始 group dict（从 keys 里带出来的）兜底
            if isinstance(first_tk, dict) and isinstance(first_tk.get("_raw_group"), dict):
                _merge_rate_to_group(g, first_tk["_raw_group"])

        # 合并分组选项：
        #   1) 优先 server_group_options（来自独立分组接口 /groups/available，包含空分组，和网站下拉一致）
        #   2) 补充 group_opt_map（从 tokens 的 group 字段反推，避免部分老系统没有独立分组接口时列表为空）
        merged_opts: dict = {}
        def _add_opt(o):
            if not isinstance(o, dict):
                return
            o_id = o.get("id")
            if o_id is None or o_id == "":
                return
            o_name = o.get("name")
            # 统一用字符串做主键（保留原值在 .id 里）
            k = str(o_id)
            if k in merged_opts:
                # 已有：缺字段时补（尤其是倍率）
                exist = merged_opts[k]
                if not exist.get("name") and o_name:
                    exist["name"] = o_name
                for fld in ("rate_multiplier", "peak_rate_multiplier", "peak_rate_enabled", "peak_start", "peak_end", "platform"):
                    if fld in o and fld not in exist:
                        exist[fld] = o[fld]
                return
            entry = {"id": o_id, "name": o_name or f"分组 #{o_id}"}
            for fld in ("rate_multiplier", "peak_rate_multiplier", "peak_rate_enabled", "peak_start", "peak_end", "platform"):
                if fld in o:
                    entry[fld] = o[fld]
            merged_opts[k] = entry
        for o in server_group_options:
            _add_opt(o)
        # 从 keys 反推的 group_opt_map：里面其实只有 id/name，但如果原始 group dict 里带倍率也已经进过 server_group_options 了
        for o in group_opt_map.values():
            _add_opt(o)
        if merged_opts:
            opts = list(merged_opts.values())
            for g in groups:
                if not g.get("is_channel_group"):
                    g["all_group_options"] = opts
        elif group_opt_map:
            # 兜底（老系统，理论不会走到）
            opts = list(group_opt_map.values())
            for g in groups:
                if not g.get("is_channel_group"):
                    g["all_group_options"] = opts

    # ==== 再尝试取上游/渠道分组（sub2api 特有的渠道/供应商分组） ====
    channel_endpoints = [
        f"{base}/api/v1/admin/channels",
        f"{base}/api/v1/channel",
        f"{base}/api/channel",
        f"{base}/api/channels",
        f"{base}/api/provider",
        f"{base}/api/providers",
        f"{base}/api/upstream",
    ]
    channel_list = []
    for url in channel_endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=3, params={"p": 1, "page_size": 500})
            if resp.status_code == 200:
                data = resp.json()
                arr = None
                if isinstance(data, dict):
                    arr = data.get("data") or data.get("items") or data.get("list")
                    if not isinstance(arr, list) and isinstance(data.get("data"), dict):
                        arr = data["data"].get("items") or data["data"].get("list")
                if isinstance(arr, list):
                    channel_list = arr
                    break
        except Exception:
            pass

    if channel_list:
        channel_group_map: dict = {}
        for c in channel_list:
            if not isinstance(c, dict):
                continue
            group_key = None
            for k in ("type", "group", "category", "tag"):
                v = c.get(k)
                if v:
                    group_key = str(v)
                    break
            if not group_key:
                group_key = "默认渠道"
            if group_key not in channel_group_map:
                channel_group_map[group_key] = {
                    "name": group_key,
                    "count": 0,
                    "channels": [],
                }
            entry = {
                "id": c.get("id"),
                "name": c.get("name") or c.get("remark") or c.get("title") or "",
                "type": c.get("type") or c.get("category") or "",
                "model": c.get("model") or c.get("models") or "",
                "base_url": c.get("base_url") or c.get("api_base") or "",
                "status": c.get("status") or c.get("enabled") or "active",
                "priority": c.get("priority") or c.get("weight") or 0,
            }
            channel_group_map[group_key]["channels"].append(entry)
            channel_group_map[group_key]["count"] += 1

        # 把渠道分组合并到总 groups（如果存在渠道，则 keys 为"密钥分组"，channels 为"渠道分组"）
        if groups:
            # 已有令牌分组 → 渠道分组作为单独一类
            groups.append({
                "name": "上游渠道分组",
                "count": len(channel_list),
                "sub_groups": list(channel_group_map.values()),
                "is_channel_group": True,
            })
        else:
            # 没取到令牌分组 → 直接用渠道分组展示
            groups = list(channel_group_map.values())
            for g in groups:
                g["is_channel_group"] = True

    if not groups and not token_list and not channel_list:
        return None, last_token_err or "未能获取到任何分组信息"

    return groups, ""


def _pool_update_key_group(pool_type: str, base_url: str, token: str, key_id, target_group_id) -> tuple[bool, str]:
    """
    修改某个密钥的所属分组（调用远程 PUT /api/v1/keys/{id}）
    返回 (ok, msg)
    """
    base = _normalize_base_url(base_url)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 不同系统支持的字段名可能不同：group_id / group
    payload_candidates = [
        {"group_id": target_group_id},
        {"group": target_group_id},
        {"group_id": target_group_id, "group": target_group_id},
    ]
    endpoints = [
        f"{base}/api/v1/keys/{key_id}",
        f"{base}/api/keys/{key_id}",
        f"{base}/api/v1/token/{key_id}",
        f"{base}/api/token/{key_id}",
    ]

    last_err = ""
    for url in endpoints:
        for payload in payload_candidates:
            try:
                resp = requests.put(url, headers=headers, json=payload, timeout=15)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            code = data.get("code")
                            if code is None or code == 0 or str(code) == "200" or code is True:
                                return True, "修改成功"
                            msg = data.get("message") or data.get("msg") or f"code={code}"
                            last_err = f"返回异常：{msg}"
                            continue
                    except Exception:
                        # 200 但非 JSON 也算成功
                        return True, "修改成功（HTTP 200）"
                else:
                    # 也尝试 PATCH
                    try:
                        resp2 = requests.patch(url, headers=headers, json=payload, timeout=15)
                        if resp2.status_code == 200:
                            return True, "修改成功（PATCH）"
                        last_err = f"HTTP {resp.status_code} / PATCH {resp2.status_code}"
                    except Exception as e2:
                        last_err = f"HTTP {resp.status_code} / PATCH异常 {e2}"
            except Exception as e:
                last_err = f"异常：{e}"
    return False, last_err or "未能调用修改分组接口"


# ================================================================
#  账号池页面路由
# ================================================================


@app.route("/acc", methods=["GET"])
@app.route("/acc/", methods=["GET"])
def account_pool_page():
    """账号池管理页面（需要管理员登录）——入口 /acc，好记"""
    from flask import session as flask_session
    if not flask_session.get("admin_logged_in"):
        # 未登录管理员 → 跳转到 admin 登录页
        return render_template("admin.html", logged_in=False, error="请先登录管理员账号以访问账号池管理")
    return render_template("account-pool.html")


@app.route("/account-pool", methods=["GET"])
@app.route("/account-pool/", methods=["GET"])
def account_pool_redirect():
    """旧入口兼容：永久跳转到新地址 /acc"""
    from flask import redirect
    return redirect("/acc", code=301)


# ================================================================
#  账号池 REST API
# ================================================================


def _require_admin():
    """要求管理员权限"""
    from flask import session as flask_session
    return bool(flask_session.get("admin_logged_in"))


@app.route("/api/pool/accounts", methods=["GET"])
def api_pool_list():
    """获取账号池列表（密码脱敏）"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        accounts = _load_pool_accounts()
        safe = []
        for a in accounts:
            safe.append({
                "id": a["id"],
                "pool_type": a["pool_type"],
                "name": a["name"],
                "base_url": a["base_url"],
                "username": a["username"],
                "password_preview": a["password"][:4] + "****" if a.get("password") else "",
                "balance": a.get("balance"),
                "balance_updated_at": a.get("balance_updated_at"),
                "groups_summary": [
                    {"name": g.get("name"), "count": g.get("count", 0)}
                    for g in (a.get("groups") or [])[:10]
                ],
                "groups_count": len(a.get("groups") or []),
                "groups_updated_at": a.get("groups_updated_at"),
                "remark": a.get("remark"),
                "status": a.get("status"),
                "created_at": a.get("created_at"),
                "updated_at": a.get("updated_at"),
            })
        return {"ok": True, "accounts": safe}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts", methods=["POST"])
def api_pool_add():
    """新增账号池账号"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        data = request.get_json(force=True) or {}
        pool_type = (data.get("pool_type") or "").strip()
        name = (data.get("name") or "").strip()
        base_url = (data.get("base_url") or "").strip()
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        remark = (data.get("remark") or "").strip()

        if pool_type not in ("sub2api", "newapi"):
            return {"ok": False, "message": "账号类型必须是 sub2api 或 newapi"}
        if not name:
            return {"ok": False, "message": "请填写名称"}
        if not base_url:
            return {"ok": False, "message": "请填写 API 地址"}
        if not username:
            return {"ok": False, "message": "请填写用户名"}
        if not password:
            return {"ok": False, "message": "请填写密码"}

        acc = {
            "id": str(uuid.uuid4()),
            "pool_type": pool_type,
            "name": name,
            "base_url": _normalize_base_url(base_url),
            "username": username,
            "password": password,
            "access_token": None,
            "balance": None,
            "balance_updated_at": None,
            "groups": [],
            "groups_updated_at": None,
            "remark": remark,
            "status": "active",
        }
        _upsert_pool_account(acc)
        return {"ok": True, "message": f"账号「{name}」添加成功", "id": acc["id"]}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>", methods=["PUT"])
def api_pool_update(pool_id):
    """修改账号池账号"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}
        data = request.get_json(force=True) or {}
        fields = {}
        for k in ("pool_type", "name", "base_url", "username", "password", "remark", "status"):
            if k in data:
                v = data[k]
                if isinstance(v, str):
                    v = v.strip()
                if k == "base_url" and isinstance(v, str):
                    v = _normalize_base_url(v)
                if v is not None and v != "":
                    fields[k] = v
        if not fields:
            return {"ok": False, "message": "没有提供可更新的字段"}
        _update_pool_field(pool_id, **fields)
        return {"ok": True, "message": "账号已更新"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>", methods=["DELETE"])
def api_pool_delete(pool_id):
    """删除账号池账号"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        ok = _delete_pool_account(pool_id)
        if not ok:
            return {"ok": False, "message": "账号不存在"}
        return {"ok": True, "message": "账号已删除"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/login", methods=["POST"])
def api_pool_login(pool_id):
    """对指定账号执行登录，并保存 access_token"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}
        token, err = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
        if not token:
            return {"ok": False, "message": f"登录失败：{err}"}
        _update_pool_field(pool_id, access_token=token)
        return {"ok": True, "message": "登录成功，已保存 Token", "token_preview": token[:16] + "..."}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/balance", methods=["POST"])
def api_pool_balance(pool_id):
    """查询并保存余额（如未登录或 Token 失效会自动重新登录）"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}

        token = acc.get("access_token")
        need_relogin = False
        if token:
            balance, err = _pool_get_balance(acc["pool_type"], acc["base_url"], token)
            if balance is None:
                need_relogin = True
        else:
            need_relogin = True

        if need_relogin:
            token, err = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
            if not token:
                return {"ok": False, "message": f"登录失败，无法查询余额：{err}"}
            _update_pool_field(pool_id, access_token=token)
            balance, err = _pool_get_balance(acc["pool_type"], acc["base_url"], token)
            if balance is None:
                return {"ok": False, "message": f"登录成功但余额查询失败：{err}"}

        now = datetime.now().isoformat()
        _update_pool_field(pool_id, balance=balance, balance_updated_at=now)
        return {"ok": True, "message": "余额查询成功", "balance": balance, "updated_at": now}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/groups", methods=["POST"])
def api_pool_groups(pool_id):
    """查询并保存密钥/渠道分组（如未登录会自动登录）"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}

        token = acc.get("access_token")
        need_relogin = False
        groups = None
        err = ""
        if token:
            groups, err = _pool_get_groups(acc["pool_type"], acc["base_url"], token)
            if groups is None:
                need_relogin = True
        else:
            need_relogin = True

        if need_relogin:
            token, err_login = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
            if not token:
                return {"ok": False, "message": f"登录失败，无法查询分组：{err_login}"}
            _update_pool_field(pool_id, access_token=token)
            groups, err = _pool_get_groups(acc["pool_type"], acc["base_url"], token)
            if groups is None:
                return {"ok": False, "message": f"登录成功但分组查询失败：{err}"}

        now = datetime.now().isoformat()
        _update_pool_field(pool_id, groups=groups, groups_updated_at=now)
        all_options = []
        for g in groups:
            opts = g.get("all_group_options") if isinstance(g, dict) else None
            if isinstance(opts, list) and opts:
                all_options = opts
                break
        return {"ok": True, "message": f"分组查询成功，共 {len(groups)} 个分组", "groups": groups, "group_options": all_options, "updated_at": now}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/groups", methods=["GET"])
def api_pool_groups_get(pool_id):
    """获取分组详情：如果本地无缓存或缓存超过 5 分钟，自动登录并拉取远程分组"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}

        groups = acc.get("groups") or []
        updated_at = acc.get("groups_updated_at")
        needs_refresh = False

        # 判断是否需要重新拉取：无缓存 / 缓存超过 5 分钟 / 显式 force=1
        force = request.args.get("force", "").strip() in ("1", "true", "yes")
        if force or not groups:
            needs_refresh = True
        elif updated_at:
            try:
                from datetime import datetime as _dt
                last = _dt.fromisoformat(updated_at)
                if (_dt.now() - last).total_seconds() > 300:
                    needs_refresh = True
            except Exception:
                needs_refresh = True

        if needs_refresh:
            # 自动登录获取 token
            token = acc.get("access_token")
            relogged_in = False
            if not token:
                token, err = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
                if not token:
                    return {"ok": False, "message": f"登录失败：{err}"}
                _update_pool_field(pool_id, access_token=token)
                relogged_in = True

            # 拉取分组
            groups, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token)
            # 若拉取失败且本次没有重登（说明 token 可能过期）→ 强制再登录一次再拉
            if groups is None and not relogged_in:
                token2, err2 = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
                if token2:
                    _update_pool_field(pool_id, access_token=token2)
                    groups, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token2)
            if groups is None:
                return {"ok": False, "message": f"分组查询失败：{g_err}"}
            now = datetime.now().isoformat()
            _update_pool_field(pool_id, groups=groups, groups_updated_at=now)
            updated_at = now

        # 重新读取最新数据（包含 selected 等字段）
        acc = _find_pool_account(pool_id)
        # 把「全部分组选项」抽出来放顶层，避免前端在 groups 里循环找（渠道分组不会挂 all_group_options）
        all_options = []
        for g in groups:
            opts = g.get("all_group_options") if isinstance(g, dict) else None
            if isinstance(opts, list) and opts:
                all_options = opts
                break
        return {
            "ok": True,
            "groups": groups,
            "group_options": all_options,
            "groups_updated_at": updated_at,
            "selected_group": acc.get("selected_group") if acc else None,
            "selected_token_id": acc.get("selected_token_id") if acc else None,
            "selected_token_name": acc.get("selected_token_name") if acc else None,
            "selected_token_key": acc.get("selected_token_key") if acc else None,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/select-token", methods=["POST"])
def api_pool_select_token(pool_id):
    """选择某个分组下的某个密钥作为当前账号的主 Key"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        data = request.get_json(silent=True) or {}
        group_name = data.get("group_name") or ""
        token_id = data.get("token_id") or ""
        token_name = data.get("token_name") or ""
        token_key = data.get("token_key") or ""

        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}

        # 校验 token_key 非空
        if not token_key:
            return {"ok": False, "message": "密钥不能为空"}

        _update_pool_field(
            pool_id,
            selected_group=group_name or None,
            selected_token_id=token_id or None,
            selected_token_name=token_name or None,
            selected_token_key=token_key,
        )
        return {
            "ok": True,
            "message": f"已选用分组「{group_name}」→ 密钥「{token_name}」",
            "selected_group": group_name,
            "selected_token_name": token_name,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/keys/<key_id>/move-group", methods=["POST"])
def api_pool_key_move_group(pool_id, key_id):
    """
    修改某个密钥的所属分组：转发 PUT /api/v1/keys/{id} {group_id}
    body: {"target_group_id": ..., "target_group_name": 可选}
    """
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        data = request.get_json(silent=True) or {}
        target_group_id = data.get("target_group_id")
        target_group_name = data.get("target_group_name") or ""
        if target_group_id is None or target_group_id == "":
            return {"ok": False, "message": "缺少目标分组ID（target_group_id）"}

        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}

        # 如未登录先登录一次
        token = acc.get("access_token")
        need_relogin = False
        msg = ""
        if token:
            ok, err = _pool_update_key_group(acc["pool_type"], acc["base_url"], token, key_id, target_group_id)
            if not ok:
                # Token 可能失效 → 尝试重新登录
                if "401" in err or "HTTP 4" in err:
                    need_relogin = True
                else:
                    msg = err
        else:
            need_relogin = True

        if need_relogin:
            token2, err_login = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
            if not token2:
                return {"ok": False, "message": f"登录失败，无法修改分组：{err_login}"}
            _update_pool_field(pool_id, access_token=token2)
            ok2, err2 = _pool_update_key_group(acc["pool_type"], acc["base_url"], token2, key_id, target_group_id)
            if not ok2:
                return {"ok": False, "message": f"远程修改分组失败：{err2}"}
        elif msg:
            return {"ok": False, "message": f"远程修改分组失败：{msg}"}

        # 成功后立即刷新分组缓存，保证前端下次看到的是新分组
        groups_new, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token or token2)
        now = datetime.now().isoformat()
        if groups_new is not None:
            _update_pool_field(pool_id, groups=groups_new, groups_updated_at=now)
        else:
            # 刷新失败没关系，标记一下下次要强制刷新
            _update_pool_field(pool_id, groups_updated_at="")

        name_hint = target_group_name or str(target_group_id)
        return {
            "ok": True,
            "message": f"已将密钥 #{key_id} 移动到分组「{name_hint}」",
            "target_group_id": target_group_id,
            "target_group_name": target_group_name,
            "refreshed_groups": groups_new is not None,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/clear-selection", methods=["POST"])
def api_pool_clear_selection(pool_id):
    """清除当前账号的选用密钥"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        _update_pool_field(
            pool_id,
            selected_group=None,
            selected_token_id=None,
            selected_token_name=None,
            selected_token_key=None,
        )
        return {"ok": True, "message": "已清除选用密钥"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/refresh-all", methods=["POST"])
def api_pool_refresh_all():
    """批量刷新所有账号的余额和分组"""
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        accounts = _load_pool_accounts()
        if not accounts:
            return {"ok": False, "message": "暂无账号"}

        results = []

        def do_one(acc: dict):
            r = {"id": acc["id"], "name": acc["name"], "pool_type": acc["pool_type"]}
            # 登录
            token, err = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
            if not token:
                r["login_ok"] = False
                r["login_err"] = err
                results.append(r)
                return
            r["login_ok"] = True
            # 余额
            balance, b_err = _pool_get_balance(acc["pool_type"], acc["base_url"], token)
            if balance is not None:
                r["balance"] = balance
                r["balance_ok"] = True
            else:
                r["balance_ok"] = False
                r["balance_err"] = b_err
            # 分组
            groups, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token)
            if groups is not None:
                r["groups_count"] = len(groups)
                r["groups_ok"] = True
            else:
                r["groups_ok"] = False
                r["groups_err"] = g_err
            # 保存
            now = datetime.now().isoformat()
            fields = {"access_token": token}
            if balance is not None:
                fields["balance"] = balance
                fields["balance_updated_at"] = now
            if groups is not None:
                fields["groups"] = groups
                fields["groups_updated_at"] = now
            try:
                _update_pool_field(acc["id"], **fields)
                r["saved"] = True
            except Exception as e:
                r["saved"] = False
                r["save_err"] = str(e)
            results.append(r)

        threads = []
        for acc in accounts:
            t = threading.Thread(target=do_one, args=(acc,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        success = sum(1 for r in results if r.get("saved"))
        return {
            "ok": True,
            "message": f"批量刷新完成：成功 {success}/{len(results)}",
            "results": results,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


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