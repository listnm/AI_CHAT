"""
AI 对话网页应用 - Flask 后端
提供 /chat 流式接口，代理到用户指定的 OpenAI 兼容 API
支持多账号管理、自动测速、最快账号自动切换
"""

import os
import json
import time
import uuid
import hashlib
import secrets
import base64
import sqlite3
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import quote
from datetime import datetime, timedelta
from flask import Flask, render_template, request, Response, stream_with_context, redirect
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

# 上游连接池：每个工作线程复用 TCP/TLS 连接，避免每次 API 调用重新握手。
# 不配置自动重试，避免 POST 请求被重复提交；失败直接返回给客户端。
_upstream_local = threading.local()


def _get_upstream_session():
    session = getattr(_upstream_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=0, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _upstream_local.session = session
    return session


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
        access_token_input_encrypted TEXT,
        user_id TEXT,
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

_PROXY_SETTINGS_DDL = """
    CREATE TABLE IF NOT EXISTS proxy_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""


_GROK_OAUTH_SESSIONS_DDL = """
    CREATE TABLE IF NOT EXISTS grok_oauth_sessions (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL UNIQUE,
        nonce TEXT NOT NULL,
        code_verifier TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0
    );
"""


_GROK_ACCOUNTS_DDL = """
    CREATE TABLE IF NOT EXISTS grok_accounts (
        id TEXT PRIMARY KEY,
        stable_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        email TEXT,
        platform TEXT NOT NULL DEFAULT 'grok',
        base_url TEXT NOT NULL,
        access_token_encrypted TEXT NOT NULL,
        refresh_token_encrypted TEXT,
        client_id_encrypted TEXT,
        team_id TEXT,
        subject_id TEXT,
        expires_at TEXT,
        token_version TEXT,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        last_error TEXT,
        last_latency_ms INTEGER,
        last_test_at TEXT,
        last_used_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT ''
    );
"""


_POOL_NEW_COLUMNS_DDL = [
    "ALTER TABLE pool_accounts ADD COLUMN selected_group TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN selected_token_id TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN selected_token_name TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN selected_token_key_encrypted TEXT",
    "ALTER TABLE pool_accounts ADD COLUMN access_token_input_encrypted TEXT",
        "ALTER TABLE pool_accounts ADD COLUMN user_id TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN refresh_token_encrypted TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN client_id_encrypted TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN team_id TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN subject_id TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN expires_at TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN token_version TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN notes TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE grok_accounts ADD COLUMN last_error TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN last_latency_ms INTEGER",
    "ALTER TABLE grok_accounts ADD COLUMN last_test_at TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN last_used_at TEXT",
    "ALTER TABLE grok_accounts ADD COLUMN model TEXT NOT NULL DEFAULT ''",
]


def _ensure_pool_table(conn):
    """确保 pool_accounts 表存在 + 所有新列齐全（运行时兜底，防止 PG 多 worker 环境遗漏迁移）"""
    migrated = False
    # PG 下 DDL 必须在独立事务里执行，否则 rollback 会丢失
    # 先提交当前事务，避免 CREATE TABLE/ALTER TABLE 因前面的 INSERT 失败被回滚
    try:
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute(_POOL_ACCOUNTS_DDL)
        migrated = True
        conn.commit()
    except Exception as e:
        # 表已存在类错误（PG 的 "relation already exists" 等），忽略，继续补列
        try:
            conn.commit()
        except Exception:
            pass
    # 兼容旧 pool_accounts/grok_accounts：补充新增字段（已存在则跳过）
    for col_sql in _POOL_NEW_COLUMNS_DDL:
        try:
            conn.execute(col_sql)
            migrated = True
            conn.commit()
        except Exception as e:
            # 列已存在或其它 PG 错误（如多 worker 并发），记录但不中断
            try:
                conn.commit()
            except Exception:
                pass
    # 最终确认所有列都已就绪（PG 下用显式 EXISTS 查询兜底）
    try:
        cols_sql = """SELECT column_name FROM information_schema.columns
                       WHERE table_name = 'pool_accounts' AND column_name = 'access_token_input_encrypted'"""
        rows = conn.execute(cols_sql).fetchall()
        if not rows:
            # 兜底：如果列还没加上（可能上面的 ALTER TABLE 因事务丢失），重试一次
            conn.execute("ALTER TABLE pool_accounts ADD COLUMN access_token_input_encrypted TEXT")
            conn.commit()
    except Exception:
        try:
            conn.commit()
        except Exception:
            pass
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
            conn.executescript(_accounts_dll)
            conn.executescript(_GROK_ACCOUNTS_DDL)
            conn.executescript(_GROK_OAUTH_SESSIONS_DDL)
            conn.executescript(_PROXY_SETTINGS_DDL)
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

def _upstream_models_url(chat_url: str) -> str:
    """从聊天接口地址推导同一上游的 models 地址。"""
    url = chat_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url[:-len("/chat/completions")] + "/models"
    return url + "/models"


def _resolve_effective_model(account: dict, requested_model: str = "") -> str:
    """解析最终上游模型：请求模型 > 后台账号模型 > 上游 models 第一个。"""
    requested_model = (requested_model or "").strip()
    if requested_model and requested_model != "auto":
        return requested_model

    configured = (account.get("model") or "").strip()
    if configured:
        return configured.split(",")[0].strip()

    api_url = normalize_api_url(account.get("api_url", ""))
    api_key = (account.get("api_key") or "").strip()
    if not api_url or not api_key:
        return ""
    try:
        resp = _get_upstream_session().get(
            _upstream_models_url(api_url),
            headers=_grok_cli_headers(api_url, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}),
            timeout=(8, 15),
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data", []) if isinstance(body, dict) else []
        for item in items:
            if isinstance(item, dict) and str(item.get("id", "")).strip():
                return str(item["id"]).strip()
    except Exception as exc:
        print(f"[model-resolve] upstream models lookup failed: {exc}", flush=True)
    return ""


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
    # 某些 sub2api 站点已经携带 /v1/chat/completions 或包含其他固定路径，
    # 这里仅在“纯域名”或 “.../v1” 结尾时补全；不再盲目追加。
    if url.endswith("/v1/chat/completions") or url.endswith("/v1/chat/completions/"):
        return url
    if url.endswith("/chat/completions") or url.endswith("/chat/completions/"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


# ================================================================
#  页面路由
# ================================================================

@app.route("/")
def index():
    """首页：原对话页已移除，跳转到游乐场"""
    return redirect("/playground")


@app.route("/playground")
@app.route("/playground/")
def playground():
    """new-api 风格游乐场：选账号/模型 + 参数面板 + 流式对话"""
    from flask import session as flask_session
    # 前端用户自动标记为已登录（仅限对话 API，不能访问 /admin）
    flask_session["user_logged_in"] = True
    return render_template("playground.html")


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

    # 找出最快延迟
    fastest_latency = None
    for acc in safe_accounts:
        if acc.get("latency_ms") is not None:
            if fastest_latency is None or acc["latency_ms"] < fastest_latency:
                fastest_latency = acc["latency_ms"]

    default_account_id = next((a["id"] for a in safe_accounts if a.get("is_default")), "")
    proxy_provider = _proxy_provider_setting()
    settings_conn = _get_db()
    try:
        grok_available = settings_conn.execute("SELECT COUNT(*) AS n FROM grok_accounts WHERE status='active' AND access_token_encrypted IS NOT NULL AND access_token_encrypted != ''").fetchone()["n"]
    finally:
        settings_conn.close()

    base_url = request.host_url.rstrip("/")
    proxy_url = base_url + "/v1/chat/completions"

    return render_template(
        "admin.html",
        logged_in=True,
        accounts=safe_accounts,
        default_account_id=default_account_id,
        base_url=base_url,
        proxy_url=proxy_url,
        proxy_models_url=proxy_url,
        proxy_api_key=PROXY_API_KEY,
        fastest_latency=fastest_latency,
        proxy_provider=proxy_provider,
        grok_available=grok_available,
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
            "is_default": acc.get("is_default", False),
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

    new_account = {
        "id": str(uuid.uuid4()),
        "name": name,
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "latency_ms": None,
        "last_speed_test": None,
    }
    # 直接插入单条记录，避免读取/重写整个账号表时因旧账号解密失败而阻塞新增。
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO accounts (id, name, api_url, api_key_encrypted, model, latency_ms, last_speed_test, is_default) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (new_account["id"], name, api_url, _encrypt(api_key), model, None, None),
        )
        conn.commit()
    finally:
        conn.close()

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


@app.route("/api/accounts/<account_id>/unset-default", methods=["POST"])
def api_accounts_unset_default(account_id):
    """取消默认中转站（无默认账号后，转发时自动回退到最快的账号）"""
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    accounts = _load_accounts()
    found = False
    for acc in accounts:
        if acc["id"] == account_id:
            acc["is_default"] = False
            found = True
    if not found:
        return {"ok": False, "message": "账号不存在"}
    _save_accounts(accounts)
    return {"ok": True, "message": "已取消默认中转站"}


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
        api_key = (account["api_key"] or "").strip()
        model = account["model"]
    else:
        api_url = normalize_api_url(data.get("api_url", ""))
        api_key = (data.get("api_key") or "").strip()
        model = data.get("model", "")

    if not api_url or not api_key:
        return {"ok": False, "message": "请先填写 API 地址和 Key"}
    if mode == "strict" and not model:
        return {"ok": False, "message": "严格测速需要模型名称"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers = _grok_cli_headers(api_url, headers)

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
        api_key = (account["api_key"] or "").strip()
    else:
        api_url = data.get("api_url", "")
        api_key = (data.get("api_key") or "").strip()

    if not api_url or not api_key:
        return {"ok": False, "models": [], "message": "请先填写 API 地址和 Key"}

    models_url = normalize_api_url(api_url).replace("/chat/completions", "/models")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers = _grok_cli_headers(api_url, headers)

    try:
        resp = requests.get(models_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        model_list = [m["id"] for m in data.get("data", []) if "id" in m]
        return {"ok": True, "models": model_list}
    except Exception as e:
        return {"ok": False, "models": [], "message": f"获取模型列表失败：{str(e)}"}


# ================================================================
#  流式对话接口（游乐场 / 测试用）
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
        api_key = (account["api_key"] or "").strip()
        # 允许前端指定模型（游乐场可选列表中的任意模型），缺省用账号配置的模型
        model = (data.get("model") or "").strip() or account["model"]
    else:
        api_url = normalize_api_url(data.get("api_url", ""))
        api_key = (data.get("api_key") or "").strip()
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
    headers = _grok_cli_headers(api_url, headers)
    # 游乐场参数面板：只透传前端显式给出的参数（new-api 语义：未启用的参数不发送）
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    for key in ("temperature", "top_p", "max_tokens", "frequency_penalty", "presence_penalty", "seed"):
        if key in data and data[key] is not None:
            payload[key] = data[key]
    if "temperature" not in payload:
        payload["temperature"] = 0.7

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


@app.route("/api/playground/account", methods=["GET"])
def api_playground_account():
    """
    游乐场用：只返回后台「转发账号」（默认账号）的信息，不暴露全部账号列表。
    与 /v1/chat/completions 的默认账号选择逻辑一致：默认账号优先，否则选最快的。
    """
    if not _require_login():
        return {"ok": False, "message": "请先登录"}, 401
    account = _select_account("")
    if not account:
        return {"ok": True, "account": None}
    return {"ok": True, "account": {
        "id": account["id"],
        "name": account.get("name", ""),
        "model": account.get("model", ""),
        "latency_ms": account.get("latency_ms"),
    }}


def _make_provider_info(account: dict) -> dict:
    """生成账号信息描述"""
    return {
        "name": account.get("name", ""),
        "api_url": account.get("api_url", ""),
        "model": account.get("model", ""),
        "latency_ms": account.get("latency_ms"),
    }


def _ascii_header(value: str) -> str:
    """HTTP 响应头只允许 ASCII（latin-1）。账号名/模型名含中文等非 ASCII 字符时，
    直接写入会触发 werkzeug 的 UnicodeEncodeError，响应发送失败，客户端表现为 502。
    这里将不可编码部分做百分号编码（RFC 3986），保证响应头始终能正常发送。"""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return quote(value, safe="")


def _anthropic_auth_ok():
    """兼容 Anthropic x-api-key 与 OpenAI Bearer 认证。"""
    key = (request.headers.get("x-api-key") or "").strip()
    bearer = request.headers.get("Authorization", "")
    return key == PROXY_API_KEY or bearer == "Bearer " + PROXY_API_KEY


def _anthropic_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "image" and block.get("source", {}).get("type") == "base64":
                parts.append("[image]")
        return "".join(parts)
    return "" if value is None else str(value)


def _anthropic_content_to_openai(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _anthropic_text(content)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif typ == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64" and source.get("media_type") and source.get("data"):
                parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (source["media_type"], source["data"])}})
            elif source.get("type") == "url" and source.get("url"):
                parts.append({"type": "image_url", "image_url": {"url": source["url"]}})
        elif typ == "tool_result":
            # tool_result 由消息转换器单独处理
            continue
    return parts or ""


def _anthropic_messages_to_openai(data):
    out = []
    system = data.get("system")
    if system:
        out.append({"role": "system", "content": _anthropic_text(system)})
    for message in data.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")
        blocks = content if isinstance(content, list) else []
        tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        tool_results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        text_content = _anthropic_content_to_openai(content)
        if role == "assistant" and tool_uses:
            calls = []
            for b in tool_uses:
                calls.append({"id": b.get("id", "call_" + uuid.uuid4().hex[:16]), "type": "function", "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            msg = {"role": "assistant", "content": text_content if text_content else None, "tool_calls": calls}
            out.append(msg)
        elif tool_results:
            for b in tool_results:
                out.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": _anthropic_text(b.get("content", ""))})
        else:
            out.append({"role": role, "content": text_content})
    return out


def _anthropic_tools_to_openai(tools):
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append({"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("input_schema") or {"type": "object", "properties": {}}}})
    return out or None


def _anthropic_tool_choice_to_openai(choice):
    if not isinstance(choice, dict):
        return None
    typ = choice.get("type")
    if typ == "auto":
        return "auto"
    if typ == "any":
        return "required"
    if typ == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def _anthropic_message_response(chat, model):
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": str(msg["content"])})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": call.get("id", "call_" + uuid.uuid4().hex[:16]), "name": fn.get("name", ""), "input": inp})
    finish = choice.get("finish_reason")
    stop_reason = "tool_use" if msg.get("tool_calls") else ("max_tokens" if finish == "length" else "end_turn")
    usage = chat.get("usage") or {}
    return {"id": chat.get("id", "msg_" + uuid.uuid4().hex[:24]), "type": "message", "role": "assistant", "model": model, "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}}


def _anthropic_error(message, status=400, error_type="invalid_request_error"):
    return {"type": "error", "error": {"type": error_type, "message": message}}, status


@app.route("/v1/messages/count_tokens", methods=["POST"])
@app.route("/messages/count_tokens", methods=["POST"])
def anthropic_count_tokens():
    if not _anthropic_auth_ok():
        return _anthropic_error("Invalid API Key", 401, "authentication_error")
    data = request.get_json(force=True) or {}
    text = _anthropic_text(data.get("system", "")) + " " + " ".join(_anthropic_text(m.get("content", "")) for m in data.get("messages") or [] if isinstance(m, dict))
    text += " " + " ".join(str(t.get("name", "")) + " " + str(t.get("description", "")) for t in data.get("tools") or [] if isinstance(t, dict))
    return {"input_tokens": max(1, (len(text) + 3) // 4)}


@app.route("/v1/messages", methods=["POST"])
@app.route("/messages", methods=["POST"])
def anthropic_messages():
    if not _anthropic_auth_ok():
        return _anthropic_error("Invalid API Key", 401, "authentication_error")
    data = request.get_json(force=True) or {}
    provider = _proxy_provider("")
    if not provider:
        return _anthropic_error("No available accounts in pool", 503, "api_error")
    account = provider.get("account") or {"id":provider["id"],"name":provider["name"],"api_url":provider["api_url"],"api_key":provider["api_key"],"model":provider.get("model","")}
    model = _resolve_effective_model(account, data.get("model", ""))
    if not model:
        return _anthropic_error("model is required", 400)
    messages = _anthropic_messages_to_openai(data)
    if not messages:
        return _anthropic_error("messages is required", 400)
    payload = {"model": model, "messages": messages, "stream": bool(data.get("stream", False)), "max_tokens": data.get("max_tokens", 4096)}
    for key in ("temperature", "top_p", "stop_sequences"):
        if key in data:
            payload["stop" if key == "stop_sequences" else key] = data[key]
    tools = _anthropic_tools_to_openai(data.get("tools"))
    if tools:
        payload["tools"] = tools
    choice = _anthropic_tool_choice_to_openai(data.get("tool_choice"))
    if choice is not None:
        payload["tool_choice"] = choice
    api_url = normalize_api_url(provider["api_url"])
    headers = {"Authorization": "Bearer " + (provider["api_key"] or "").strip(), "Content-Type": "application/json"}
    headers = _grok_cli_headers(api_url, headers)
    try:
        upstream = _get_upstream_session().post(api_url, headers=headers, json=payload, stream=payload["stream"], timeout=(8, 600))
        upstream.raise_for_status()
        upstream.encoding = "utf-8"
    except requests.exceptions.HTTPError as e:
        try:
            return _anthropic_error(e.response.json().get("error", {}).get("message", str(e)), e.response.status_code, "api_error")
        except Exception:
            return _anthropic_error(str(e), e.response.status_code, "api_error")
    except requests.exceptions.RequestException as e:
        return _anthropic_error(str(e), 502, "api_error")
    if not payload["stream"]:
        try:
            return _anthropic_message_response(upstream.json(), model)
        finally:
            upstream.close()
    return Response(stream_with_context(_anthropic_stream(upstream, model)), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _anthropic_stream(upstream, model):
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    yield "event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}, ensure_ascii=False) + "\n\n"
    yield "event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, ensure_ascii=False) + "\n\n"
    buf = b""
    for chunk in upstream.iter_content(chunk_size=1024):
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            raw = line[6:]
            if raw.strip() == b"[DONE]":
                yield "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
                yield "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\",\"stop_sequence\":null},\"usage\":{\"output_tokens\":0}}\n\n"
                yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
                upstream.close()
                return
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content"):
                yield "event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta["content"]}}, ensure_ascii=False) + "\n\n"
    yield "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
    yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
    upstream.close()


@app.route("/v1/models", methods=["GET"])
@app.route("/models", methods=["GET"])
def proxy_models():
    """OpenAI 兼容模型列表：只公开当前默认转发账号配置的模型。"""
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + PROXY_API_KEY
    if auth != expected:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}}, 401

    provider = _proxy_provider("")
    if not provider:
        return {"object": "list", "data": []}
    account = provider.get("account") or {"id":provider["id"],"name":provider["name"],"api_url":provider["api_url"],"api_key":provider["api_key"],"model":provider.get("model","")}
    model = _resolve_effective_model(account, provider.get("model", ""))
    models = []
    if model:
        models.append({
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "proxy",
        })
    return {"object": "list", "data": models}



def _grok_provider():
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM grok_accounts WHERE status='active' AND access_token_encrypted IS NOT NULL AND access_token_encrypted != '' ORDER BY last_latency_ms ASC, updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        expires = str(row["expires_at"] or "")
        if expires:
            exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            now = datetime.now(exp.tzinfo) if exp.tzinfo else datetime.now()
            if (exp - now).total_seconds() < 300 and row["refresh_token_encrypted"]:
                result, error = _grok_refresh_token(row)
                if error:
                    return None
                access, refresh, new_exp = result
                conn = _get_db()
                try:
                    conn.execute("UPDATE grok_accounts SET access_token_encrypted=?,refresh_token_encrypted=?,expires_at=?,status='active',last_error=NULL,updated_at=? WHERE id=?", (_encrypt(access), _encrypt(refresh), new_exp, datetime.now().isoformat(), row["id"]))
                    conn.commit()
                finally:
                    conn.close()
                row = dict(row)
                row["access_token_encrypted"] = _encrypt(access)
    except Exception:
        pass
    return {"kind":"grok", "id":row["id"], "name":row["name"], "model":(row["model"] if "model" in row.keys() else ""), "api_url":row["base_url"], "api_key":_decrypt(row["access_token_encrypted"]), "row":row}


def _proxy_provider(account_name=""):
    if _proxy_provider_setting() == "grok" and not account_name:
        return _grok_provider()
    acc = _select_account(account_name)
    if not acc:
        return None
    return {"kind":"accounts", "id":acc["id"], "name":acc["name"], "model":acc.get("model",""), "api_url":acc["api_url"], "api_key":acc["api_key"], "account":acc}


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
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

    provider = _proxy_provider(account_name)
    if not provider:
        return {"error": {"message": "No available accounts in pool", "type": "server_error"}, "ok": False}, 503
    account = provider.get("account") or {"id":provider["id"],"name":provider["name"],"api_url":provider["api_url"],"api_key":provider["api_key"],"model":provider.get("model","")}
    api_url = normalize_api_url(provider["api_url"])
    api_key = (provider["api_key"] or "").strip()
    # OpenAI/Sub2API 语义：显式模型名原样转发，供上游渠道或模型映射处理。
    # 只有 model=auto 或请求缺少 model 时，才回退到账号后台配置模型。
    effective_model = _resolve_effective_model(account, model)
    if not effective_model:
        return {"error": {"message": "Model name not specified, model name cannot be empty", "type": "invalid_request_error"}}, 400
    provider_info = _make_provider_info(account)
    provider_info["effective_model"] = effective_model

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers = _grok_cli_headers(api_url, headers)
    # 透传客户端请求的参数（top_p/tools/response_format/frequency_penalty 等）。
    # 同时兼容 max_completion_tokens：统一转换为上游常见的 max_tokens 字段。
    _reserved = {"account", "model", "messages", "stream", "max_tokens", "max_completion_tokens"}
    payload = {k: v for k, v in data.items() if k not in _reserved}
    # 新版 OpenAI 客户端可能发送 max_completion_tokens；多数兼容上游仍使用 max_tokens。
    payload["model"] = effective_model
    payload["messages"] = messages
    payload["stream"] = stream

    try:
        # timeout 用元组 (connect_timeout, read_timeout)：
        # - 连接建立 30s 足够
        # - 单次读 chunk 600s（流式时相邻 chunk 之间的间隔通常很小，但长输出
        #   时上游可能间隔较久才推下一块）
        upstream = _get_upstream_session().post(
            api_url, headers=headers, json=payload, stream=stream, timeout=(8, 600),
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
            err_obj = body.get("error") if isinstance(body, dict) else None
            if isinstance(err_obj, dict):
                msg = err_obj.get("message", str(e))
            elif isinstance(err_obj, str):
                msg = err_obj
            else:
                msg = body.get("message", str(e)) if isinstance(body, dict) else str(e)
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
            for chunk in upstream.iter_content(chunk_size=1024):
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
                        # 上游在流中返回错误（如限流/暂时不可用）：OpenAI SSE 没有标准错误
                        # 事件，透传原始 error 行会导致客户端解析失败（如 Codex CLI 报
                        # "Turn execution failed"）。这里记录日志并以 [DONE] 干净收尾。
                        try:
                            _err = json.loads(data_content)
                            if isinstance(_err, dict) and _err.get("error"):
                                print(f"[proxy-stream] upstream error mid-stream: {json.dumps(_err, ensure_ascii=False)[:300]}", flush=True)
                                yield "data: [DONE]\n\n"
                                return
                        except Exception:
                            pass
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
                "X-Provider-Name": _ascii_header(provider_info.get("name", "")),
                "X-Provider-Model": _ascii_header(provider_info.get("effective_model", "")),
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
                    "X-Provider-Name": _ascii_header(provider_info.get("name", "")),
                    "X-Provider-Model": _ascii_header(provider_info.get("effective_model", "")),
                }
            )
        except Exception as e:
            return {"error": {"message": f"Failed to parse upstream response: {str(e)}", "type": "parse_error"}, "ok": False}, 502


# ================================================================
#  OpenAI Responses API 兼容转发端点（/v1/responses）
#  供 Codex CLI 等默认走 Responses API 的客户端使用：
#  请求转换为 chat/completions 转发到上游中转站，再把响应（含流式）
#  转回 Responses API 格式。认证与账号选择逻辑与 /v1/chat/completions 一致。
# ================================================================

def _responses_input_to_messages(instructions, input_items):
    """Responses API 的 input 列表 → chat completions messages"""
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    for item in input_items or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "message" or "role" in item:
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type")
                    if pt in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": p.get("text", "")})
                    elif pt == "input_image":
                        parts.append({"type": "image_url",
                                      "image_url": {"url": p.get("image_url") or p.get("data") or ""}})
                    elif pt == "refusal":
                        parts.append({"type": "text", "text": p.get("refusal", "")})
                content = parts if parts else ""
            messages.append({"role": role, "content": content})
        elif typ == "function_call":
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex[:16]),
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "")},
                }],
            })
        elif typ == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": "" if item.get("output") is None else str(item.get("output")),
            })
        # reasoning 等其他类型直接忽略
    return messages


def _responses_tools_to_chat(tools):
    """Responses API 的 tools → chat completions tools（仅保留 function 工具）"""
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }
        if t.get("strict") is not None:
            fn["strict"] = t["strict"]
        out.append({"type": "function", "function": fn})
    return out or None


def _responses_tool_choice_to_chat(tc):
    """Responses API 的 tool_choice → chat completions 格式"""
    if not tc or tc in ("auto", "none", "required"):
        return tc
    if isinstance(tc, dict) and tc.get("type") == "function":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def _chat_usage_to_responses(usage):
    """chat completions usage → Responses API usage"""
    usage = usage or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": details.get("reasoning_tokens", 0)},
    }


def _chat_message_to_responses_items(message):
    """chat completions 的单条 message → Responses API output items"""
    items = []
    text_parts = []
    content = message.get("content")
    if content:
        text_parts.append({"type": "output_text", "text": str(content), "annotations": []})
    if text_parts:
        items.append({
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex[:24],
            "status": "completed",
            "role": "assistant",
            "content": text_parts,
        })
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        items.append({
            "type": "function_call",
            "id": "fc_" + uuid.uuid4().hex[:24],
            "status": "completed",
            "call_id": tc.get("id") or ("call_" + uuid.uuid4().hex[:16]),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
        })
    return items


def _chat_response_to_responses(chat_json, model):
    """非流式 chat/completions 响应 → Responses API 响应体"""
    choice = (chat_json.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    items = _chat_message_to_responses_items(message)
    finish = choice.get("finish_reason")
    output_text = ""
    for it in items:
        if it.get("type") == "message":
            output_text = "".join(p.get("text", "") for p in it.get("content", []))
    return {
        "id": "resp_" + uuid.uuid4().hex[:24],
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": items,
        "output_text": output_text,
        "usage": _chat_usage_to_responses(chat_json.get("usage")),
    }


def _responses_sse(event, obj):
    """Responses API 流式事件行"""
    return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _responses_stream_generator(upstream, resp_id, created_at, effective_model):
    """把上游 chat/completions 的 SSE 流转成 Responses API 的 SSE 事件流"""
    skeleton = {
        "id": resp_id,
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": effective_model,
        "output": [],
    }
    yield _responses_sse("response.created", {"type": "response.created", "response": skeleton})
    yield _responses_sse("response.in_progress", {"type": "response.in_progress", "response": skeleton})

    msg_item = None      # {item_id, text}
    funcs = {}           # tool index -> {index, item_id, call_id, name, args}
    finish = None
    usage = None
    buffer = b""

    for chunk in upstream.iter_content(chunk_size=1024):
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data: "):
                continue
            content = line[6:].strip()
            if content == b"[DONE]":
                buffer = b""
                break
            try:
                evt = json.loads(content)
            except Exception:
                continue
            # 上游在流中返回错误：以 response.failed 干净收尾，避免客户端
            # （Codex CLI 等）解析到非标准 data 行而报 "Turn execution failed"
            if isinstance(evt, dict) and evt.get("error"):
                err_obj = evt["error"]
                err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                print(f"[responses-stream] upstream error mid-stream: {err_msg}", flush=True)
                yield _responses_sse("response.failed", {
                    "type": "response.failed",
                    "response": {
                        "id": resp_id,
                        "object": "response",
                        "created_at": created_at,
                        "status": "failed",
                        "model": effective_model,
                        "error": {"code": "upstream_error", "message": err_msg},
                    },
                })
                return
            choices = evt.get("choices") or []
            if not choices:
                if evt.get("usage"):
                    usage = evt["usage"]
                continue
            ch = choices[0]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            delta = ch.get("delta") or {}

            text = delta.get("content")
            if text:
                if msg_item is None:
                    msg_item = {"item_id": "msg_" + uuid.uuid4().hex[:24], "text": ""}
                    yield _responses_sse("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "id": msg_item["item_id"],
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    })
                    yield _responses_sse("response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": msg_item["item_id"],
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                msg_item["text"] += text
                yield _responses_sse("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": msg_item["item_id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                })

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if idx not in funcs:
                    funcs[idx] = {
                        "index": len(funcs),
                        "item_id": "fc_" + uuid.uuid4().hex[:24],
                        "call_id": tc.get("id") or ("call_" + uuid.uuid4().hex[:16]),
                        "name": fn.get("name") or "",
                        "args": "",
                    }
                    yield _responses_sse("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": funcs[idx]["index"],
                        "item": {
                            "id": funcs[idx]["item_id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": funcs[idx]["call_id"],
                            "name": funcs[idx]["name"],
                            "arguments": "",
                        },
                    })
                args = fn.get("arguments")
                if args:
                    funcs[idx]["args"] += args
                    yield _responses_sse("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": funcs[idx]["item_id"],
                        "output_index": funcs[idx]["index"],
                        "delta": args,
                    })

    # 流结束：补齐各 item 的 done 事件与最终 response
    output_items = []
    if msg_item is not None:
        output_items.append({
            "type": "message",
            "id": msg_item["item_id"],
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": msg_item["text"], "annotations": []}],
        })
        yield _responses_sse("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_item["item_id"],
            "output_index": 0,
            "content_index": 0,
            "text": msg_item["text"],
        })
        yield _responses_sse("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_item["item_id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": msg_item["text"], "annotations": []},
        })
        yield _responses_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": output_items[-1],
        })
    for idx in sorted(funcs):
        f = funcs[idx]
        output_items.append({
            "type": "function_call",
            "id": f["item_id"],
            "status": "completed",
            "call_id": f["call_id"],
            "name": f["name"],
            "arguments": f["args"],
        })
        yield _responses_sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "item_id": f["item_id"],
            "output_index": f["index"],
            "arguments": f["args"],
        })
        yield _responses_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": f["index"],
            "item": output_items[-1],
        })

    final_response = {
        "id": resp_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed" if finish != "length" else "incomplete",
        "model": effective_model,
        "output": output_items,
        "output_text": msg_item["text"] if msg_item else "",
        "usage": _chat_usage_to_responses(usage),
    }
    yield _responses_sse("response.completed", {"type": "response.completed", "response": final_response})


@app.route("/v1/responses", methods=["POST"])
@app.route("/responses", methods=["POST"])
def proxy_responses():
    """标准 OpenAI Responses API 直通转发，不转换为 Chat Completions。"""
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + PROXY_API_KEY
    if auth != expected:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}}, 401

    data = request.get_json(force=True) or {}
    account_name = data.get("account", "")
    model = data.get("model", "")
    provider = _proxy_provider(account_name)
    if not provider:
        return {"error": {"message": "No available accounts in pool", "type": "server_error"}}, 503
    account = provider.get("account") or {"id":provider["id"],"name":provider["name"],"api_url":provider["api_url"],"api_key":provider["api_key"],"model":provider.get("model","")}
    effective_model = _resolve_effective_model(account, model)
    if not effective_model:
        return {"error": {"message": "Model name not specified, model name cannot be empty", "type": "invalid_request_error"}}, 400

    chat_url = normalize_api_url(account["api_url"])
    responses_url = chat_url
    if chat_url.endswith("/chat/completions"):
        responses_url = chat_url[:-len("/chat/completions")] + "/responses"

    # 只有内部 account 字段不转发；Responses 请求体其余字段保持 OpenAI 原格式。
    payload = dict(data)
    payload.pop("account", None)
    payload["model"] = effective_model

    headers = {
        "Authorization": "Bearer " + (provider["api_key"] or "").strip(),
        "Content-Type": request.headers.get("Content-Type", "application/json"),
    }
    headers = _grok_cli_headers(provider["api_url"], headers)
    stream = bool(payload.get("stream", False))
    try:
        upstream = _get_upstream_session().post(
            responses_url, headers=headers, json=payload, stream=stream, timeout=(8, 600),
        )
        upstream.raise_for_status()
        upstream.encoding = "utf-8"
    except requests.exceptions.Timeout:
        return {"error": {"message": "Upstream API timeout", "type": "timeout"}}, 504
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        try:
            body = e.response.json()
            return body, status
        except Exception:
            return {"error": {"message": e.response.text[:1000], "type": "upstream_error"}}, status
    except requests.exceptions.RequestException as e:
        return {"error": {"message": str(e), "type": "connection_error"}}, 502

    provider_headers = {
        "X-Provider-Name": _ascii_header(account.get("name", "")),
        "X-Provider-Model": _ascii_header(payload.get("model", "")),
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if stream:
        content_type = upstream.headers.get("Content-Type", "text/event-stream; charset=utf-8")
        def generate_responses():
            try:
                for chunk in upstream.iter_content(chunk_size=1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
        return Response(
            stream_with_context(generate_responses()),
            status=upstream.status_code,
            content_type=content_type,
            headers={**provider_headers, "Connection": "keep-alive"},
        )

    try:
        body = upstream.content
        content_type = upstream.headers.get("Content-Type", "application/json; charset=utf-8")
        return Response(body, status=upstream.status_code, content_type=content_type, headers=provider_headers)
    finally:
        upstream.close()


def proxy_responses_legacy():
    """
    OpenAI Responses API 兼容端点（Codex CLI 等客户端默认走这里）。
    请求与响应的协议转换：
      - input/instructions/tools/reasoning  → chat/completions 消息
      - chat/completions 流式响应           → Responses API SSE 事件
    账号选择与 /v1/chat/completions 完全一致（model=auto 用账号配置的模型）。
    """
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + PROXY_API_KEY
    if auth != expected:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}, "ok": False}, 401

    data = request.get_json(force=True) or {}
    account_name = data.get("account", "")
    model = data.get("model", "")
    is_auto = model == "auto"
    stream = data.get("stream", False)

    provider = _proxy_provider(account_name)
    if not provider:
        return {"error": {"message": "No available accounts in pool", "type": "server_error"}, "ok": False}, 503
    account = provider.get("account") or {"id":provider["id"],"name":provider["name"],"api_url":provider["api_url"],"api_key":provider["api_key"],"model":provider.get("model","")}
    api_url = normalize_api_url(provider["api_url"])
    api_key = (provider["api_key"] or "").strip()
    effective_model = _resolve_effective_model(account, model)

    messages = _responses_input_to_messages(data.get("instructions"), data.get("input"))
    if not messages:
        return {"error": {"message": "input is required", "type": "invalid_request"}, "ok": False}, 400

    provider_info = _make_provider_info(account)
    provider_info["effective_model"] = effective_model

    # 丢弃 Responses API 专属字段与 max_output_tokens（与 chat 端点一致，
    # 避免客户端传的小 max 截断上游输出）
    _reserved = {
        "account", "model", "stream", "instructions", "input", "store", "include",
        "metadata", "prompt_cache_key", "previous_response_id", "truncation", "user",
        "max_output_tokens",
    }
    payload = {k: v for k, v in data.items() if k not in _reserved}
    payload["model"] = effective_model
    payload["messages"] = messages
    payload["stream"] = stream

    if isinstance(data.get("reasoning"), dict):
        effort = data["reasoning"].get("effort")
        if effort:
            payload["reasoning_effort"] = effort
        payload.pop("reasoning", None)
    if isinstance(data.get("text"), dict):
        fmt = data["text"].get("format") or {}
        if fmt.get("type") == "json_schema" and fmt.get("schema"):
            payload["response_format"] = {"type": "json_schema", "json_schema": fmt["schema"]}
        elif fmt.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}
        payload.pop("text", None)
    tools = _responses_tools_to_chat(data.get("tools"))
    if tools:
        payload["tools"] = tools
    else:
        payload.pop("tools", None)
    tool_choice = _responses_tool_choice_to_chat(data.get("tool_choice"))
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    else:
        payload.pop("tool_choice", None)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers = _grok_cli_headers(api_url, headers)

    try:
        upstream = _get_upstream_session().post(
            api_url, headers=headers, json=payload, stream=stream, timeout=(8, 600),
        )
        upstream.raise_for_status()
        upstream.encoding = "utf-8"
    except requests.exceptions.Timeout:
        return {"error": {"message": "Upstream API timeout", "type": "timeout"}, "ok": False}, 504
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        try:
            body = e.response.json()
            err_obj = body.get("error") if isinstance(body, dict) else None
            if isinstance(err_obj, dict):
                msg = err_obj.get("message", str(e))
            elif isinstance(err_obj, str):
                msg = err_obj
            else:
                msg = body.get("message", str(e)) if isinstance(body, dict) else str(e)
        except Exception:
            msg = str(e)
        return {"error": {"message": msg, "type": "upstream_error", "code": status}, "ok": False}, status
    except requests.exceptions.RequestException as e:
        return {"error": {"message": str(e), "type": "connection_error"}, "ok": False}, 502

    base_headers = {
        "X-Provider-Name": _ascii_header(provider_info.get("name", "")),
        "X-Provider-Model": _ascii_header(provider_info.get("effective_model", "")),
    }

    if stream:
        resp_id = "resp_" + uuid.uuid4().hex[:24]
        created_at = int(time.time())
        return Response(
            stream_with_context(_responses_stream_generator(upstream, resp_id, created_at, effective_model)),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                **base_headers,
            }
        )
    else:
        try:
            resp_data = upstream.json()
            converted = _chat_response_to_responses(resp_data, effective_model)
            return app.response_class(
                response=json.dumps(converted, ensure_ascii=False),
                mimetype="application/json; charset=utf-8",
                headers=base_headers,
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
                "access_token_input": (lambda enc: _decrypt(enc) if enc else None)(row["access_token_input_encrypted"]) if "access_token_input_encrypted" in row.keys() else None,
                "user_id": row["user_id"] if "user_id" in row.keys() else None,
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
            "access_token_input": (lambda enc: _decrypt(enc) if enc else None)(row["access_token_input_encrypted"]) if "access_token_input_encrypted" in cols else None,
            "user_id": row["user_id"] if "user_id" in cols else None,
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
        tok_input_enc = _encrypt(acc["access_token_input"]) if acc.get("access_token_input") else None
        if existing:
            conn.execute(
                """UPDATE pool_accounts SET pool_type=?, name=?, base_url=?, username=?,
                   password_encrypted=?, access_token_encrypted=?, access_token_input_encrypted=?, user_id=?, balance=?, balance_updated_at=?,
                   groups_json=?, groups_updated_at=?, remark=?, status=?,
                   selected_group=?, selected_token_id=?, selected_token_name=?, selected_token_key_encrypted=?,
                   updated_at=? WHERE id=?""",
                (
                    acc["pool_type"], acc["name"], acc["base_url"], acc["username"],
                    _encrypt(acc["password"]),
                    _encrypt(acc["access_token"]) if acc.get("access_token") else None,
                    tok_input_enc,
                    acc.get("user_id"),
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
                   password_encrypted, access_token_encrypted, access_token_input_encrypted, user_id, balance, balance_updated_at,
                   groups_json, groups_updated_at, remark, status,
                   selected_group, selected_token_id, selected_token_name, selected_token_key_encrypted,
                   created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    acc["id"], acc["pool_type"], acc["name"], acc["base_url"], acc["username"],
                    _encrypt(acc["password"]),
                    _encrypt(acc["access_token"]) if acc.get("access_token") else None,
                    tok_input_enc,
                    acc.get("user_id"),
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
            elif k == "access_token_input":
                sets.append("access_token_input_encrypted = ?")
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


def _pool_login(pool_type: str, base_url: str, username: str, password: str) -> tuple[str | None, str | None, str]:
    """
    登录 sub2api / newapi 获取 access_token
    返回 (token, user_id, error_msg)
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
                        user_id = None
                        if isinstance(data, dict):
                            d = data.get("data", data)
                            if isinstance(d, dict):
                                token = d.get("token") or d.get("access_token") or d.get("Authorization") or d.get("Token") or d.get("AccessToken")
                                user = d.get("user") or {}
                                if isinstance(user, dict):
                                    user_id = user.get("id")
                            if not token:
                                token = data.get("token") or data.get("access_token") or data.get("Authorization") or data.get("Token") or data.get("AccessToken")
                        if token and isinstance(token, str):
                            if token.lower().startswith("bearer "):
                                token = token[7:]
                            return token, str(user_id) if user_id is not None else None, ""
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
    return None, None, last_error or "所有登录端点均未返回有效 token"


def _pool_headers(pool_type: str, token: str, user_id=None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if pool_type == "newapi" and user_id is not None and str(user_id).strip():
        headers["New-Api-User"] = str(user_id)
    return headers


def _pool_check_token(pool_type: str, base_url: str, token: str, user_id=None) -> tuple[bool, str]:
    """宽松校验 access_token：优先用账号池真实依赖的接口验证。"""
    balance, balance_err = _pool_get_balance(pool_type, base_url, token, user_id)
    if balance is not None:
        return True, ""
    groups, groups_err = _pool_get_groups(pool_type, base_url, token, user_id)
    if groups is not None:
        return True, ""
    return False, groups_err or balance_err or "token 校验失败"


def _pool_relogin_with_fallback(acc: dict) -> tuple[str | None, str | None, str]:
    """
    重新登录：优先复用用户手动绑定的 access_token_input（GitHub/OAuth 用户），
    失败再 fallback 用户名密码登录。返回 (token, error_msg)
    """
    manual = (acc.get("access_token_input") or "").strip()
    if manual:
        ok, err = _pool_check_token(acc["pool_type"], acc["base_url"], manual, acc.get("user_id"))
        if ok:
            return manual, acc.get("user_id"), ""
        if not (acc.get("username") and acc.get("password")):
            return None, None, f"手动 access_token 不可用或已过期：{err}。请先用用户名/密码登录一次以自动保存 NewAPI User ID，或在编辑账号里补充 User ID"
    if acc.get("username") and acc.get("password"):
        return _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
    return None, None, "未配置用户名/密码，也未提供有效手动 access_token"


def _pool_get_balance(pool_type: str, base_url: str, token: str, user_id=None) -> tuple[float | None, str]:
    """
    查询余额（sub2api / newapi）。
    NewAPI 的 /wallet 页面通常使用 /api/user/self 返回的 quota，
    quota 是内部额度单位，钱包金额需要除以 500000。
    """
    base = _normalize_base_url(base_url)
    headers = _pool_headers(pool_type, token, user_id)
    endpoints = [
        f"{base}/api/user/self",
        f"{base}/api/v1/auth/me",
        f"{base}/api/v1/user/profile",
        f"{base}/api/user/info",
        f"{base}/api/me",
        f"{base}/user/self",
    ]

    def to_number(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            try:
                return float(text)
            except ValueError:
                return None
        return None

    def find_amount(obj):
        if not isinstance(obj, dict):
            return None
        # balance/credits 等通常已经是页面展示单位，不能再次换算。
        for key in ("balance", "credits", "available", "remaining", "quota_remaining"):
            amount = to_number(obj.get(key))
            if amount is not None:
                return amount
        # NewAPI quota 是内部额度单位，钱包页面显示 quota / 500000。
        quota = to_number(obj.get("quota"))
        if quota is not None:
            return quota / 500000 if pool_type == "newapi" else quota
        for key in ("user", "info", "account"):
            amount = find_amount(obj.get(key))
            if amount is not None:
                return amount
        return None

    last_error = ""
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    candidates = [data]
                    if isinstance(data, dict):
                        candidates.append(data.get("data"))
                    amount = None
                    for candidate in candidates:
                        amount = find_amount(candidate)
                        if amount is not None:
                            break
                    if amount is not None:
                        return amount, ""
                    last_error = f"未解析到余额字段：响应={json.dumps(data, ensure_ascii=False)[:300]}"
                except Exception as e:
                    last_error = f"响应解析失败：{e}"
            else:
                last_error = f"HTTP {resp.status_code}：{resp.text[:150]}"
        except Exception as e:
            last_error = f"异常：{e}"
    return None, last_error or "未能从任何端点获取余额信息"


def _pool_get_groups(pool_type: str, base_url: str, token: str, user_id=None) -> tuple[list | None, str]:
    """
    读取密钥/令牌分组（sub2api 的渠道分组、newapi 的令牌分组）
    返回 (groups_list, error_msg)
    """
    base = _normalize_base_url(base_url)
    headers = _pool_headers(pool_type, token, user_id)

    groups = []

    # ==== 【关键】先拉「可用分组列表」（包含空分组！这才是 /keys 页面下拉的来源）====
    # 优先使用独立分组接口；后续合并到 all_group_options，确保下拉能列出所有分组（哪怕组里没 key）
    group_list_endpoints = ([f"{base}/api/user/self/groups"] if pool_type == "newapi" else []) + [
        f"{base}/api/v1/groups/available",
        f"{base}/api/groups/available",
        f"{base}/api/v1/group/available",
        f"{base}/api/v1/user/groups",
        f"{base}/api/user/groups",
        f"{base}/api/v1/keys/groups/options",
    ]
    server_group_options = []  # [{id,name}]
    embedded_token_list = []
    for url in group_list_endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                arr = None
                if isinstance(data, dict):
                    d = data.get("data")
                    if isinstance(d, dict):
                        embedded_token_list.extend(d.get("tokens") or d.get("keys") or d.get("items") or [])
                        arr = d.get("groups") or d.get("list")
                        # NewAPI 格式：data = {"ccmax": {"desc": ..., "ratio": 0.9}}
                        # 将分组名提升为 name，保留 ratio 供前端显示倍率。
                        if not arr and pool_type == "newapi":
                            arr = []
                            for group_name, group_info in d.items():
                                if not isinstance(group_info, dict):
                                    continue
                                item = dict(group_info)
                                item.setdefault("id", group_info.get("id") or group_name)
                                item.setdefault("name", group_info.get("name") or group_name)
                                arr.append(item)
                    elif isinstance(d, list):
                        arr = d
                    if not arr:
                        embedded_token_list.extend(data.get("tokens") or data.get("keys") or [])
                        arr = data.get("groups") or data.get("items") or data.get("list")
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
                        # NewAPI 不同版本可能使用 ratio / rate / multiplier 表示倍率。
                        rate_value = None
                        for rate_key in ("rate_multiplier", "ratio", "rate", "multiplier", "倍率"):
                            if g.get(rate_key) is not None:
                                rate_value = g[rate_key]
                                break
                        if rate_value is not None:
                            item["rate_multiplier"] = rate_value
                        for fld, key in (("peak_rate_multiplier", "peak_rate_multiplier"),
                                         ("peak_rate_enabled", "peak_rate_enabled"),
                                         ("platform", "platform"),
                                         ("description", "description")):
                            if fld in g:
                                item[key] = g[fld]
                        out.append(item)
                    if out:
                        server_group_options = out
                        break
        except Exception:
            pass

    # ==== 再取令牌列表（sub2api/newapi 通用 /api/token 等） ====
    token_endpoints = ([f"{base}/api/token/"] if pool_type == "newapi" else []) + [
        f"{base}/api/v1/keys",
        f"{base}/api/token",
        f"{base}/api/tokens",
        f"{base}/api/keys",
        f"{base}/api/user/tokens",
    ]
    token_list = list(embedded_token_list)
    last_token_err = ""
    for url in token_endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=6, params={
                "page": 1, "p": 1, "size": 500, "page_size": 500,
                "sort_by": "created_at", "sort_order": "desc",
                "timezone": "Asia/Shanghai",
            })
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
                "group": t.get("group") if isinstance(t.get("group"), str) else (t.get("group", {}) or {}).get("name") if isinstance(t.get("group"), dict) else None,
                "group_id": t.get("group_id") or (t.get("group", {}) or {}).get("id") if isinstance(t.get("group"), dict) else t.get("group_id"),
                "key_preview": (str(t.get("key") or t.get("token") or "")[:16] + "...") if (t.get("key") or t.get("token")) else "",
                "key": t.get("key") or t.get("token") or "",  # 完整密钥，供前端复制
                "status": t.get("status") or t.get("enabled") or "active",
                "_group_id": raw_group_id,
                "_group_name": raw_group_name or group_key,
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
        elif group_opt_map:
            # 兜底（老系统，理论不会走到）
            opts = list(group_opt_map.values())
        else:
            opts = []

        # 分组选择是“账号级”的，不是“密钥级”的。
        # 因此所有分组块（包括 Sub2API 渠道分组、NewAPI 空分组）都携带同一份可选分组列表。
        if opts:
            for g in groups:
                if isinstance(g, dict):
                    g["all_group_options"] = opts

        # NewAPI /api/user/self/groups 会返回“用户可用的全部分组”，
        # 即使某个分组当前没有 Token，也应该在账号池里显示出来并允许选择。
        if pool_type == "newapi" and groups and server_group_options:
            existing_names = {str(g.get("name") or "") for g in groups if isinstance(g, dict)}
            for option in server_group_options:
                name = str(option.get("name") or option.get("id") or "")
                if not name or name in existing_names:
                    continue
                groups.append({
                    "id": option.get("id"),
                    "name": name,
                    "count": 0,
                    "tokens": [],
                    "rate_multiplier": option.get("rate_multiplier"),
                    "peak_rate_multiplier": option.get("peak_rate_multiplier"),
                    "peak_rate_enabled": option.get("peak_rate_enabled"),
                    "platform": option.get("platform"),
                    "description": option.get("description"),
                    "all_group_options": opts or server_group_options,
                })
                existing_names.add(name)

    # NewAPI 的 /api/user/self/groups 可能只返回分组倍率，不返回密钥列表。
    # 这类分组仍需要展示，供前端显示倍率和作为分组选择来源。
    if not groups and server_group_options:
        groups = []
        for option in server_group_options:
            groups.append({
                "name": option.get("name") or str(option.get("id")),
                "count": 0,
                "tokens": [],
                "rate_multiplier": option.get("rate_multiplier"),
                "peak_rate_multiplier": option.get("peak_rate_multiplier"),
                "peak_rate_enabled": option.get("peak_rate_enabled"),
                "platform": option.get("platform"),
                "description": option.get("description"),
                "all_group_options": server_group_options,
            })

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


def _pool_update_key_group(pool_type: str, base_url: str, token: str, key_id, target_group_id, user_id=None) -> tuple[bool, str]:
    """
    修改某个密钥的所属分组（调用远程 PUT /api/v1/keys/{id}）
    返回 (ok, msg)
    """
    base = _normalize_base_url(base_url)
    headers = _pool_headers(pool_type, token, user_id)

    # NewAPI 的分组键可能是 ccmax 这类字符串；Sub2API 仍按整数 group_id 处理。
    if pool_type == "newapi":
        # NewAPI 官方 Token 管理接口是 PUT /api/token/，
        # body 必须携带 id + group；旧代码尝试 /api/token/{id}，新版会直接 404。
        # 注意 id 必须是 JSON 数字：数据库里 selected_token_id 可能存成字符串，
        # new-api 的 Token.Id 是 int 字段，字符串 "123" 会被反序列化直接 400。
        try:
            key_id_int = int(key_id)
        except (ValueError, TypeError):
            return False, f"密钥ID不是有效整数：{key_id}"
        group_value = str(target_group_id)

        # new-api 的 UpdateToken 会把请求体里的字段（含零值）整批写回数据库：
        # 只传 id+group 会清空令牌的名称/过期时间/额度等字段，甚至可能因缺少
        # name 被校验拒绝（400）。因此先 GET 当前令牌信息，回填完整 payload。
        base_payload = {"id": key_id_int, "group": group_value}
        try:
            get_resp = requests.get(f"{base}/api/token/{key_id_int}", headers=headers, timeout=10)
            if get_resp.status_code == 200:
                gd = get_resp.json()
                cur = gd.get("data") if isinstance(gd, dict) else None
                if isinstance(cur, dict):
                    base_payload.update({
                        "name": cur.get("name") or "",
                        "status": cur.get("status", 1),
                        "expired_time": cur.get("expired_time", -1),
                        "remain_quota": cur.get("remain_quota", 0),
                        "unlimited_quota": cur.get("unlimited_quota", False),
                        "model_limits_enabled": cur.get("model_limits_enabled", False),
                        "model_limits": cur.get("model_limits") or "",
                        "allow_ips": cur.get("allow_ips"),
                        "cross_group_retry": cur.get("cross_group_retry", False),
                    })
        except Exception:
            pass  # GET 失败则降级：仅携带 id + group（尽力而为）
        payload_candidates = [base_payload]
        endpoints = [
            f"{base}/api/token/",
            f"{base}/api/token",
        ]
        methods = [requests.put]
    else:
        try:
            gid_int = int(target_group_id)
        except (ValueError, TypeError):
            return False, f"目标分组ID不是有效整数：{target_group_id}"
        payload_candidates = [
            {"group_id": gid_int},
            {"group_id": gid_int, "group": gid_int},
        ]
        endpoints = [
            f"{base}/api/v1/keys/{key_id}",
            f"{base}/api/keys/{key_id}",
        ]
        methods = [requests.put, requests.patch]
    for url in endpoints:
        for payload in payload_candidates:
            for method in methods:
                try:
                    resp = method(url, headers=headers, json=payload, timeout=15)
                    if resp.status_code in (200, 201, 204):
                        try:
                            data = resp.json()
                        except ValueError:
                            data = None
                        if isinstance(data, dict):
                            ok_flag = data.get("success")
                            code = data.get("code")
                            if ok_flag is False or (code is not None and code not in (0, 200, "0", "200", True)):
                                last_err = data.get("message") or data.get("msg") or f"返回异常：code={code}"
                                continue
                        return True, "修改请求已提交"
                    last_err = f"{method.__name__.upper()} {url}：HTTP {resp.status_code}：{resp.text[:160]}"
                except Exception as e:
                    last_err = f"{method.__name__.upper()} {url}：异常：{e}"
    return False, last_err or "未能调用修改分组接口"


def _pool_get_full_token_key(pool_type: str, base_url: str, token: str, key_id, user_id=None) -> tuple[str | None, str]:
    """
    获取某个令牌的完整 API Key。

    new-api 的 GET /api/token/ 与 GET /api/token/{id} 返回的 key 均为脱敏值
    （sk-****xxxx），完整 key 只能通过 POST /api/token/{id}/key 获取。
    返回 (full_key, error_msg)；非 newapi 站点返回 (None, 提示)。
    """
    if pool_type != "newapi":
        return None, "非 newapi 站点，跳过完整密钥获取"
    base = _normalize_base_url(base_url)
    headers = _pool_headers(pool_type, token, user_id)
    last_err = ""
    for url in (
        f"{base}/api/token/{key_id}/key",
        f"{base}/api/token/{key_id}/key/",
    ):
        for method in (requests.post, requests.get):  # new-api 用 POST；部分 one-api 系变体用 GET
            try:
                resp = method(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    key = None
                    if isinstance(data, dict):
                        d = data.get("data")
                        if isinstance(d, dict):
                            key = d.get("key") or d.get("token")
                        elif isinstance(d, str):
                            key = d
                    if key:
                        return key, ""
                    last_err = f"响应中未找到 key：{str(data)[:160]}"
            except Exception as e:
                last_err = f"异常：{e}"
    return None, last_err or "未能获取完整密钥"


def _grok_cli_headers(api_url, headers):
    """按 Sub2API 的 Grok Build/CLI 兼容要求添加客户端身份头。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(api_url).hostname or "").lower()
    except Exception:
        host = ""
    if host == "cli-chat-proxy.grok.com":
        version = os.environ.get("XAI_GROK_CLI_VERSION", "0.2.114").strip() or "0.2.114"
        headers["X-XAI-Token-Auth"] = "xai-grok-cli"
        headers["x-grok-client-version"] = version
        headers["x-grok-client-identifier"] = "grok-shell"
        headers["User-Agent"] = "xai-grok-workspace/" + version
    return headers


def _proxy_provider_setting():
    conn = _get_db()
    try:
        row = conn.execute("SELECT value FROM proxy_settings WHERE key='proxy_provider'").fetchone()
        return row["value"] if row and row["value"] in ("accounts", "grok") else "accounts"
    finally:
        conn.close()


@app.route("/api/proxy-settings", methods=["GET"])
def api_proxy_settings_get():
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    conn = _get_db()
    try:
        row = conn.execute("SELECT value FROM proxy_settings WHERE key='proxy_provider'").fetchone()
        provider = row["value"] if row and row["value"] in ("accounts", "grok") else "accounts"
        count = conn.execute("SELECT COUNT(*) AS n FROM grok_accounts WHERE status='active' AND access_token_encrypted IS NOT NULL AND access_token_encrypted != ''").fetchone()["n"]
        return {"ok": True, "provider": provider, "grok_available": count}
    finally:
        conn.close()


@app.route("/api/proxy-settings", methods=["PUT"])
def api_proxy_settings_put():
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    provider = str((request.get_json(force=True) or {}).get("provider") or "accounts").strip().lower()
    if provider not in ("accounts", "grok"):
        return {"ok": False, "message": "provider 只能是 accounts 或 grok"}, 400
    conn = _get_db()
    try:
        if provider == "grok":
            row = conn.execute("SELECT id FROM grok_accounts WHERE status='active' AND access_token_encrypted IS NOT NULL AND access_token_encrypted != '' LIMIT 1").fetchone()
            if not row:
                return {"ok": False, "message": "没有可用的 Grok OAuth 账号，请先授权"}, 400
        conn.execute("INSERT INTO proxy_settings(key,value,updated_at) VALUES('proxy_provider',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (provider, datetime.now().isoformat()))
        conn.commit()
        return {"ok": True, "provider": provider}
    finally:
        conn.close()


XAI_AUTHORIZE_URL = os.environ.get("XAI_OAUTH_AUTHORIZE_URL", "https://auth.x.ai/oauth2/authorize")
XAI_TOKEN_URL = os.environ.get("XAI_OAUTH_TOKEN_URL", "https://auth.x.ai/oauth2/token")
XAI_CLIENT_ID = os.environ.get("XAI_OAUTH_CLIENT_ID", "b1a00492-073a-47ea-816f-4c329264a828")
XAI_SCOPE = os.environ.get("XAI_OAUTH_SCOPE", "openid profile email offline_access grok-cli:access api:access")
XAI_REDIRECT_URI = os.environ.get("XAI_OAUTH_REDIRECT_URI", "http://127.0.0.1:56121/callback")


def _xai_redirect_uri():
    return XAI_REDIRECT_URI


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@app.route("/api/grok/oauth/start", methods=["GET"])
def api_grok_oauth_start():
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    verifier, challenge = _pkce_pair()
    state = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    redirect_uri = _xai_redirect_uri()
    now = datetime.now()
    conn = _get_db()
    try:
        conn.execute("INSERT INTO grok_oauth_sessions (id,state,nonce,code_verifier,redirect_uri,created_at,expires_at) VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), state, nonce, verifier, redirect_uri, now.isoformat(), (now + timedelta(minutes=30)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    from urllib.parse import urlencode
    query = urlencode({"response_type": "code", "client_id": XAI_CLIENT_ID, "redirect_uri": redirect_uri, "scope": XAI_SCOPE, "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256", "plan": "grok"})
    return {"ok": True, "authorization_url": XAI_AUTHORIZE_URL + "?" + query}


@app.route("/api/grok/oauth/callback", methods=["GET"])
@app.route("/callback", methods=["GET"])
def api_grok_oauth_callback():
    error = (request.args.get("error") or "").strip()
    if error:
        return "授权失败：" + error, 400
    state = (request.args.get("state") or "").strip()
    code = (request.args.get("code") or "").strip()
    if not state or not code:
        return "授权失败：缺少 state 或 code", 400
    conn = _get_db()
    row = conn.execute("SELECT * FROM grok_oauth_sessions WHERE state=? AND consumed=0", (state,)).fetchone()
    if not row or row["expires_at"] < datetime.now().isoformat():
        conn.close(); return "授权失败：OAuth 会话无效或已过期", 400
    try:
        resp = _get_upstream_session().post(XAI_TOKEN_URL, data={"grant_type":"authorization_code","client_id":XAI_CLIENT_ID,"code":code,"redirect_uri":row["redirect_uri"],"code_verifier":row["code_verifier"]}, headers={"User-Agent":"sub2api-grok-oauth/1.0"}, timeout=(8,20))
        if resp.status_code < 200 or resp.status_code >= 300:
            conn.close(); return "授权失败：token 交换被拒绝", 502
        token = resp.json()
        access = str(token.get("access_token") or "").strip()
        refresh = str(token.get("refresh_token") or "").strip()
        if not access:
            conn.close(); return "授权失败：响应中没有 access_token", 502
        claims = {}
        try:
            parts = access.split(".")
            if len(parts) >= 2:
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode())
        except Exception:
            pass
        subject = str(claims.get("sub") or uuid.uuid4())
        email = str(claims.get("email") or "")
        expires = str(token.get("expires_at") or "")
        now = datetime.now().isoformat()
        stable = "oauth:" + subject
        existing = conn.execute("SELECT id FROM grok_accounts WHERE stable_key=?", (stable,)).fetchone()
        values = ("Grok OAuth " + (email or subject[:12]), email, "grok", "https://cli-chat-proxy.grok.com/v1", _encrypt(access), _encrypt(refresh) if refresh else None, _encrypt(XAI_CLIENT_ID), str(claims.get("team_id") or ""), subject, expires, "", "OAuth 授权导入", now)
        if existing:
            conn.execute("UPDATE grok_accounts SET name=?,email=?,platform=?,base_url=?,access_token_encrypted=?,refresh_token_encrypted=?,client_id_encrypted=?,team_id=?,subject_id=?,expires_at=?,token_version=?,notes=?,status='active',last_error=NULL,updated_at=? WHERE stable_key=?", values + (stable,))
        else:
            conn.execute("INSERT INTO grok_accounts (id,stable_key,name,email,platform,base_url,access_token_encrypted,refresh_token_encrypted,client_id_encrypted,team_id,subject_id,expires_at,token_version,notes,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?)", (str(uuid.uuid4()), stable) + values + (now,))
        conn.execute("UPDATE grok_oauth_sessions SET consumed=1 WHERE state=?", (state,)); conn.commit()
        return redirect("/api?oauth=success")
    except Exception:
        conn.rollback(); return "授权失败：服务端处理异常", 502
    finally:
        conn.close()


def _grok_refresh_token(row):
    if not row["refresh_token_encrypted"]:
        return False, "没有 refresh_token"
    refresh = _decrypt(row["refresh_token_encrypted"])
    client_id = _decrypt(row["client_id_encrypted"]) if row["client_id_encrypted"] else XAI_CLIENT_ID
    resp = _get_upstream_session().post(XAI_TOKEN_URL, data={"grant_type":"refresh_token","client_id":client_id,"refresh_token":refresh}, headers={"User-Agent":"sub2api-grok-oauth/1.0"}, timeout=(8,20))
    if resp.status_code < 200 or resp.status_code >= 300:
        return False, "OAuth refresh 被拒绝（HTTP %s）" % resp.status_code
    token = resp.json(); access = str(token.get("access_token") or "").strip()
    if not access: return False, "OAuth refresh 响应缺少 access_token"
    new_refresh = str(token.get("refresh_token") or refresh).strip()
    expires = str(token.get("expires_at") or "")
    return (access, new_refresh, expires), ""


def _grok_mask_email(email):
    email = str(email or "")
    if "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        local_mask = local[:1] + "***"
    else:
        local_mask = local[:1] + "***" + local[-1:]
    return local_mask + "@" + domain


def _grok_safe(row):
    expires = row["expires_at"] if isinstance(row, dict) else row["expires_at"]
    return {
        "model": row["model"] if "model" in row.keys() else "", "email": _grok_mask_email(row["email"]),
        "platform": row["platform"], "base_url": row["base_url"],
        "has_access_token": bool(row["access_token_encrypted"]),
        "has_refresh_token": bool(row["refresh_token_encrypted"]),
        "expires_at": expires, "status": row["status"],
        "last_error": row["last_error"], "last_latency_ms": row["last_latency_ms"],
        "last_test_at": row["last_test_at"], "last_used_at": row["last_used_at"],
        "notes": row["notes"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _grok_list_rows():
    conn = _get_db()
    try:
        return conn.execute("SELECT * FROM grok_accounts ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()


@app.route("/api", methods=["GET"])
@app.route("/api/", methods=["GET"])
def api_page():
    from flask import session as flask_session
    if not flask_session.get("admin_logged_in"):
        return render_template("admin.html", logged_in=False, error="请先登录管理员账号以访问 API 管理")
    return render_template("api.html")


@app.route("/api/grok/accounts", methods=["GET"])
def api_grok_accounts():
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    return {"ok": True, "accounts": [_grok_safe(r) for r in _grok_list_rows()]}


@app.route("/api/grok/import", methods=["POST"])
def api_grok_import():
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        if request.files.get("file"):
            raw = request.files["file"].read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                return {"ok": False, "message": "JSON 文件不能超过 5MB"}, 400
            data = json.loads(raw.decode("utf-8"))
        else:
            data = request.get_json(force=True) or {}
        items = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return {"ok": False, "message": "JSON 顶层必须包含 accounts 数组"}, 400
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "message": "不是有效的 UTF-8 JSON"}, 400
    except Exception:
        return {"ok": False, "message": "无法读取 JSON"}, 400

    imported = updated = skipped = 0
    errors = []
    now = datetime.now().isoformat()
    conn = _get_db()
    try:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                skipped += 1; errors.append({"index": index, "message": "账号条目不是对象"}); continue
            if str(item.get("platform", "")).lower() != "grok":
                skipped += 1; errors.append({"index": index, "message": "仅支持 platform=grok"}); continue
            cred = item.get("credentials") or {}
            access = str(cred.get("access_token") or "").strip()
            base_url = str(cred.get("base_url") or "").strip().rstrip("/")
            if not access or not base_url or not (base_url.startswith("https://") or base_url.startswith("http://")):
                skipped += 1; errors.append({"index": index, "message": "缺少有效 credentials.access_token 或 credentials.base_url"}); continue
            email = str(cred.get("email") or item.get("extra", {}).get("email") or "").strip()
            subject = str(cred.get("sub") or item.get("extra", {}).get("local_account_id") or "").strip()
            stable = subject or (email + "|" + base_url) or (str(item.get("name", "")) + "|" + base_url)
            if not stable.strip("|"):
                skipped += 1; errors.append({"index": index, "message": "缺少可去重的账号标识"}); continue
            name = str(item.get("name") or email or ("grok-" + subject[:12]) or "Grok OAuth").strip()[:200]
            encrypted = (_encrypt(access), _encrypt(str(cred.get("refresh_token") or "")) if cred.get("refresh_token") else None, _encrypt(str(cred.get("client_id") or "")) if cred.get("client_id") else None)
            row = conn.execute("SELECT id FROM grok_accounts WHERE stable_key = ?", (stable,)).fetchone()
            values = (name, email, "grok", base_url, encrypted[0], encrypted[1], encrypted[2], str(cred.get("team_id") or ""), subject, str(cred.get("expires_at") or item.get("expires_at") or ""), str(cred.get("_token_version") or ""), str(item.get("notes") or ""), now)
            if row:
                conn.execute("""UPDATE grok_accounts SET name=?,email=?,platform=?,base_url=?,access_token_encrypted=?,refresh_token_encrypted=?,client_id_encrypted=?,team_id=?,subject_id=?,expires_at=?,token_version=?,notes=?,status='active',last_error=NULL,updated_at=? WHERE stable_key=?""", values + (stable,))
                updated += 1
            else:
                conn.execute("""INSERT INTO grok_accounts (id,stable_key,name,email,platform,base_url,access_token_encrypted,refresh_token_encrypted,client_id_encrypted,team_id,subject_id,expires_at,token_version,notes,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?)""", (str(uuid.uuid4()), stable) + values + (now,))
                imported += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "imported": imported, "updated": updated, "skipped": skipped, "errors": errors[:20]}


def _grok_model_access_token(row):
    """获取模型查询用 access token，临近过期时按 Sub2API 流程刷新。"""
    token = _decrypt(row["access_token_encrypted"])
    expires = str(row["expires_at"] or "")
    if expires and row["refresh_token_encrypted"]:
        try:
            exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            now = datetime.now(exp.tzinfo) if exp.tzinfo else datetime.now()
            if (exp - now).total_seconds() < 300:
                result, error = _grok_refresh_token(row)
                if error:
                    return None, error
                access, refresh, new_exp = result
                conn = _get_db()
                try:
                    conn.execute("UPDATE grok_accounts SET access_token_encrypted=?,refresh_token_encrypted=?,expires_at=?,status='active',last_error=NULL,updated_at=? WHERE id=?", (_encrypt(access), _encrypt(refresh), new_exp, datetime.now().isoformat(), row["id"]))
                    conn.commit()
                finally:
                    conn.close()
                token = access
        except ValueError:
            pass
    return token, ""


@app.route("/api/grok/accounts/<account_id>/models", methods=["GET", "PUT"])
def api_grok_models(account_id):
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    conn = _get_db()
    row = conn.execute("SELECT * FROM grok_accounts WHERE id=?", (account_id,)).fetchone()
    conn.close()
    if request.method == "PUT":
        selected = str((request.get_json(force=True) or {}).get("model") or "").strip()
        if not selected:
            return {"ok": False, "message": "模型不能为空"}, 400
        try:
            token, token_error = _grok_model_access_token(row)
            if not token:
                return {"ok": False, "message": token_error or "OAuth token 已过期，请重新授权"}, 502
            base = row["base_url"].rstrip("/")
            url = base if base.endswith("/models") else base + "/models"
            resp = _get_upstream_session().get(url, headers=_grok_cli_headers(row["base_url"], {"Authorization": "Bearer " + token, "Accept": "application/json"}), timeout=(8, 15))
            body = resp.json() if 200 <= resp.status_code < 300 else {}
            available = [str(x.get("id")) for x in (body.get("data", []) if isinstance(body, dict) else []) if isinstance(x, dict) and x.get("id")]
            if selected not in available:
                return {"ok": False, "message": "模型不在上游可用列表中", "models": available}, 400
        except Exception:
            return {"ok": False, "message": "无法校验上游模型"}, 502
        conn = _get_db()
        try:
            conn.execute("UPDATE grok_accounts SET model=?,updated_at=? WHERE id=?", (selected, datetime.now().isoformat(), account_id)); conn.commit()
        finally:
            conn.close()
        return {"ok": True, "model": selected}
    try:
        token, token_error = _grok_model_access_token(row)
        if not token:
            return {"ok": False, "message": token_error or "OAuth token 已过期，请重新授权"}, 502
        base = row["base_url"].rstrip("/")
        url = base if base.endswith("/models") else base + "/models"
        resp = _get_upstream_session().get(url, headers=_grok_cli_headers(row["base_url"], {"Authorization": "Bearer " + token, "Accept": "application/json"}), timeout=(8, 15))
        if resp.status_code < 200 or resp.status_code >= 300:
            return {"ok": False, "message": "上游模型接口返回 HTTP %s" % resp.status_code}, 502
        body = resp.json()
        models = []
        for item in (body.get("data", []) if isinstance(body, dict) else []):
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
        return {"ok": True, "models": models}
    except Exception:
        return {"ok": False, "message": "获取模型失败"}, 502


@app.route("/api/grok/accounts/<account_id>/test", methods=["POST"])
def api_grok_test(account_id):
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    conn = _get_db()
    row = conn.execute("SELECT * FROM grok_accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        conn.close(); return {"ok": False, "message": "账号不存在"}, 404
    started = time.time()
    try:
        token = _decrypt(row["access_token_encrypted"])
        url = row["base_url"].rstrip("/") + "/models" if not row["base_url"].endswith("/models") else row["base_url"]
        resp = _get_upstream_session().get(url, headers=_grok_cli_headers(row["base_url"], {"Authorization": "Bearer " + token, "Accept": "application/json"}), timeout=(8, 15))
        latency = int((time.time() - started) * 1000)
        ok = 200 <= resp.status_code < 300
        msg = "连接成功" if ok else "上游返回 HTTP %s" % resp.status_code
        conn.execute("UPDATE grok_accounts SET status=?,last_error=?,last_latency_ms=?,last_test_at=?,updated_at=? WHERE id=?", ("active" if ok else "error", None if ok else msg, latency, datetime.now().isoformat(), account_id)); conn.commit()
        return {"ok": ok, "message": msg, "latency_ms": latency}, (200 if ok else 502)
    except Exception:
        conn.execute("UPDATE grok_accounts SET status='error',last_error=?,last_test_at=?,updated_at=? WHERE id=?", ("连接测试失败", datetime.now().isoformat(), datetime.now().isoformat(), account_id)); conn.commit()
        return {"ok": False, "message": "连接测试失败"}, 502
    finally:
        conn.close()


@app.route("/api/grok/accounts/<account_id>/refresh", methods=["POST"])
def api_grok_refresh(account_id):
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    conn = _get_db()
    row = conn.execute("SELECT * FROM grok_accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        conn.close(); return {"ok": False, "message": "账号不存在"}, 404
    try:
        result, error = _grok_refresh_token(row)
        if error:
            return {"ok": False, "message": error}, 502
        access, refresh, expires = result
        conn.execute("UPDATE grok_accounts SET access_token_encrypted=?,refresh_token_encrypted=?,expires_at=?,status='active',last_error=NULL,updated_at=? WHERE id=?", (_encrypt(access), _encrypt(refresh), expires, datetime.now().isoformat(), account_id)); conn.commit()
        return {"ok": True, "message": "OAuth token 已刷新"}
    except Exception:
        return {"ok": False, "message": "OAuth 刷新失败"}, 502
    finally:
        conn.close()


@app.route("/api/grok/accounts/<account_id>", methods=["DELETE"])
def api_grok_delete(account_id):
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    conn = _get_db()
    try:
        cur = conn.execute("DELETE FROM grok_accounts WHERE id = ?", (account_id,)); conn.commit()
        if not cur.rowcount:
            return {"ok": False, "message": "账号不存在"}, 404
        return {"ok": True}
    finally:
        conn.close()


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
                "user_id": a.get("user_id"),
                "password_preview": a["password"][:4] + "****" if a.get("password") else "",
                "balance": a.get("balance"),
                "balance_updated_at": a.get("balance_updated_at"),
                "groups_summary": [
                    {"name": g.get("name"), "count": g.get("count", 0)}
                    for g in (a.get("groups") or [])[:10]
                ],
                # 前端账号卡片需要完整分组树才能直接选择分组/Key；这里不再只返回 summary。
                "groups": a.get("groups") or [],
                "selected_group": a.get("selected_group"),
                "selected_token_id": a.get("selected_token_id"),
                "selected_token_name": a.get("selected_token_name"),
                "selected_token_key": a.get("selected_token_key"),
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
        access_token_input = (data.get("access_token") or "").strip()

        if pool_type not in ("sub2api", "newapi"):
            return {"ok": False, "message": "账号类型必须是 sub2api 或 newapi"}
        if not name:
            return {"ok": False, "message": "请填写名称"}
        if not base_url:
            return {"ok": False, "message": "请填写 API 地址"}
        # 两种登录方式任选其一：用户名/密码 或 手动粘贴 access_token
        has_up = bool(username and password)
        has_tok = bool(access_token_input)
        if not has_up and not has_tok:
            return {"ok": False, "message": "请填写用户名+密码，或手动粘贴 access_token（GitHub/OAuth 登录用户可用）"}

        acc = {
            "id": str(uuid.uuid4()),
            "pool_type": pool_type,
            "name": name,
            "base_url": _normalize_base_url(base_url),
            "username": username,
            "password": password,
            "access_token_input": access_token_input or None,
            "access_token": access_token_input if has_tok else None,
            "user_id": None,
            "balance": None,
            "balance_updated_at": None,
            "groups": [],
            "groups_updated_at": None,
            "remark": remark,
            "status": "active",
        }
        _upsert_pool_account(acc)
        extras = f"（已手动绑定 access_token，无需再次登录即可查询余额/分组）" if has_tok else ""
        return {"ok": True, "message": f"账号「{name}」添加成功{extras}", "id": acc["id"]}
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
        for k in ("pool_type", "name", "base_url", "username", "password", "remark", "status", "access_token", "user_id"):
            if k in data:
                v = data[k]
                if isinstance(v, str):
                    v = v.strip()
                if k == "base_url" and isinstance(v, str):
                    v = _normalize_base_url(v)
                # 对 access_token 单独处理：有值就同时写 access_token_input + access_token；空值就清掉 input 但保留当前 access_token
                if k == "access_token":
                    if v:
                        fields["access_token_input"] = v
                        fields["access_token"] = v
                    else:
                        fields["access_token_input"] = None
                    continue
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
        # 情况1：用户手动提供了 access_token（如 GitHub OAuth 登录用户），直接用，不走用户名密码登录
        manual_tok = (acc.get("access_token_input") or "").strip()
        if manual_tok:
            ok, err = _pool_check_token(acc["pool_type"], acc["base_url"], manual_tok, acc.get("user_id"))
            if not ok:
                return {"ok": False, "message": f"手动 access_token 不可用或已过期：{err}。请重新从浏览器 localStorage 复制最新的 access_token 再粘贴"}
            _update_pool_field(pool_id, access_token=manual_tok, user_id=acc.get("user_id"))
            return {"ok": True, "message": "已启用手动 access_token，余额/分组接口校验通过（GitHub/OAuth 模式）", "token_preview": manual_tok[:16] + "..."}
        # 情况2：用户名密码登录（传统模式）
        token, user_id, err = _pool_login(acc["pool_type"], acc["base_url"], acc["username"], acc["password"])
        if not token:
            return {"ok": False, "message": f"登录失败：{err}"}
        _update_pool_field(pool_id, access_token=token, user_id=user_id)
        return {"ok": True, "message": f"登录成功，已保存 Token{f' / User ID: {user_id}' if user_id else ''}", "token_preview": token[:16] + "..."}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/oauth/github/start", methods=["POST"])
def api_pool_oauth_github_start():
    """
    发起 GitHub OAuth 登录流程（适用于 NewAPI 中转站，如 seekai.cc）。

    流程说明（已逆向 NewAPI 的 OAuth 实现）：
      1. GET  {base_url}/api/status          → 取 github_oauth / github_client_id
      2. POST {base_url}/api/oauth/state      → 取 flow_token（服务端 CSRF state，无 cookie 绑定）
      3. 拼装 GitHub 授权 URL（client_id + redirect_uri + state=flow_token + scope）

    注意：GitHub 的 redirect_uri 已在中转站后台固定为 {base_url}/api/oauth/github，
    我们无法改写它，因此用户授权后浏览器会跳回中转站回调并返回 access_token（JSON）。
    用户需从回调页的 JSON 响应或中转站 localStorage 里复制 access_token 后回填到表单。
    """
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        data = request.get_json(silent=True) or {}
        base_url = _normalize_base_url((data.get("base_url") or "").strip())
        if not base_url:
            return {"ok": False, "message": "请先填写 Base URL"}

        s = requests.Session()
        s.headers.update({"User-Agent": "acc-pool-oauth/1.0", "Accept": "application/json"})

        # 1. 拉站点状态，确认 GitHub OAuth 已开启并取 client_id
        try:
            r = s.get(f"{base_url}/api/status", timeout=12)
            r.raise_for_status()
            st = (r.json() or {}).get("data", {})
        except Exception as e:
            return {"ok": False, "message": f"访问 {base_url}/api/status 失败：{e}"}
        if not st.get("github_oauth"):
            return {"ok": False, "message": "该站点未启用 GitHub OAuth 登录，请改用用户名/密码"}
        client_id = st.get("github_client_id")
        if not client_id:
            return {"ok": False, "message": "站点配置异常：未返回 github_client_id"}

        # 2. 申请 flow_token（NewAPI 的 CSRF state，服务端保存，约 10 分钟有效期）
        try:
            r = s.post(
                f"{base_url}/api/oauth/state",
                json={"provider": "github", "intent": "login"},
                timeout=12,
            )
            r.raise_for_status()
            payload = r.json() or {}
        except Exception as e:
            return {"ok": False, "message": f"申请 flow_token 失败：{e}"}
        if not payload.get("success"):
            return {"ok": False, "message": f"申请 flow_token 失败：{payload.get('message') or payload}"}
        flow_token = (payload.get("data") or {}).get("flow_token")
        if not flow_token:
            return {"ok": False, "message": "站点未返回 flow_token"}

        # 3. 拼装 GitHub 授权 URL（redirect_uri 固定为中转站回调）
        from urllib.parse import urlencode
        params = {
            "client_id": client_id,
            "redirect_uri": f"{base_url}/api/oauth/github",
            "state": flow_token,
            "scope": "user:email",
        }
        auth_url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"

        return {
            "ok": True,
            "url": auth_url,
            "flow_token": flow_token,
            "client_id": client_id,
            "message": "已生成 GitHub 授权链接，请在打开的新窗口完成 GitHub 授权，然后从回调页 JSON 或中转站 localStorage 复制 access_token 回填",
        }
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
        user_id = acc.get("user_id")
        need_relogin = False
        if token:
            balance, err = _pool_get_balance(acc["pool_type"], acc["base_url"], token, user_id)
            if balance is None:
                need_relogin = True
        else:
            need_relogin = True

        if need_relogin:
            token, user_id, err = _pool_relogin_with_fallback(acc)
            if not token:
                return {"ok": False, "message": f"登录失败，无法查询余额：{err}"}
            _update_pool_field(pool_id, access_token=token, user_id=user_id)
            balance, err = _pool_get_balance(acc["pool_type"], acc["base_url"], token, user_id)
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
        user_id = acc.get("user_id")
        need_relogin = False
        groups = None
        err = ""
        if token:
            groups, err = _pool_get_groups(acc["pool_type"], acc["base_url"], token, user_id)
            if groups is None:
                need_relogin = True
        else:
            need_relogin = True

        if need_relogin:
            token, user_id, err_login = _pool_relogin_with_fallback(acc)
            if not token:
                return {"ok": False, "message": f"登录失败，无法查询分组：{err_login}"}
            _update_pool_field(pool_id, access_token=token, user_id=user_id)
            groups, err = _pool_get_groups(acc["pool_type"], acc["base_url"], token, user_id)
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
                from datetime import datetime, timedelta as _dt
                last = _dt.fromisoformat(updated_at)
                if (_dt.now() - last).total_seconds() > 300:
                    needs_refresh = True
            except Exception:
                needs_refresh = True

        if needs_refresh:
            # 自动登录获取 token
            token = acc.get("access_token")
            user_id = acc.get("user_id")
            relogged_in = False
            if not token:
                token, user_id, err = _pool_relogin_with_fallback(acc)
                if not token:
                    return {"ok": False, "message": f"登录失败：{err}"}
                _update_pool_field(pool_id, access_token=token, user_id=user_id)
                relogged_in = True

            # 拉取分组
            groups, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token, user_id)
            # 若拉取失败且本次没有重登（说明 token 可能过期）→ 强制再登录一次再拉
            if groups is None and not relogged_in:
                token2, user_id2, err2 = _pool_relogin_with_fallback(acc)
                if token2:
                    _update_pool_field(pool_id, access_token=token2, user_id=user_id2)
                    groups, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token2, user_id2)
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


@app.route("/api/pool/accounts/<pool_id>/select-group", methods=["POST"])
def api_pool_select_group(pool_id):
    """选择账号池当前使用的分组，并同步到远程：把账号当前使用的密钥移动到目标分组。

    - 已有选中的密钥 → 远程移动到目标分组（new-api: PUT /api/token/）
    - 没有选中密钥 → 从当前分组（兜底任意分组）自动挑一个密钥远程移动过去
    - 完全没有可用密钥 / 未登录 → 只保存本地选择，并明确提示远程未同步
    """
    if not _require_admin():
        return {"ok": False, "message": "需要管理员权限"}, 401
    try:
        data = request.get_json(silent=True) or {}
        group_name = str(data.get("group_name") or "").strip()
        group_id = data.get("group_id")

        acc = _find_pool_account(pool_id)
        if not acc:
            return {"ok": False, "message": "账号不存在"}
        if not group_name:
            return {"ok": False, "message": "分组名称不能为空"}

        # 1. 保存本地选择
        _update_pool_field(pool_id, selected_group=group_name)

        # 2. 查找目标分组 ID：优先请求显式传入的 group_id，
        #    其次从缓存的 all_group_options / groups 中解析，最后用分组名兜底
        groups = acc.get("groups") or []
        target_group_id = group_id if group_id not in (None, "") else None
        if target_group_id is None:
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for opt in (g.get("all_group_options") or []):
                    if isinstance(opt, dict) and opt.get("name") == group_name:
                        target_group_id = opt.get("id")
                        break
                if target_group_id:
                    break
        if target_group_id is None:
            for g in groups:
                if not isinstance(g, dict):
                    continue
                if g.get("name") == group_name:
                    target_group_id = g.get("id") or group_name
                    break
                for sub in (g.get("sub_groups") or []):
                    if isinstance(sub, dict) and sub.get("name") == group_name:
                        target_group_id = sub.get("id") or group_name
                        break
                if target_group_id:
                    break
        if target_group_id is None:
            target_group_id = group_name

        def _find_token_in_group(group_list, target_name):
            for g in group_list or []:
                if not isinstance(g, dict):
                    continue
                if g.get("name") == target_name:
                    tokens = g.get("tokens") or g.get("keys") or []
                    for t in tokens:
                        if isinstance(t, dict) and t.get("id") and (t.get("key") or t.get("token")):
                            return t
                for sub in (g.get("sub_groups") or []):
                    if isinstance(sub, dict) and sub.get("name") == target_name:
                        tokens = sub.get("tokens") or sub.get("keys") or []
                        for t in tokens:
                            if isinstance(t, dict) and t.get("id") and (t.get("key") or t.get("token")):
                                return t
            return None

        def _find_any_token(group_list):
            for g in group_list or []:
                if not isinstance(g, dict):
                    continue
                for t in (g.get("tokens") or g.get("keys") or []):
                    if isinstance(t, dict) and t.get("id") and (t.get("key") or t.get("token")):
                        return t
                for sub in (g.get("sub_groups") or []):
                    if isinstance(sub, dict):
                        found = _find_any_token([sub])
                        if found:
                            return found
            return None

        token = acc.get("access_token")
        user_id = acc.get("user_id")

        # 3. 确定要移动的密钥：优先已选中的；否则从当前分组（兜底任意分组）挑一个
        move_key_id = acc.get("selected_token_id")
        move_key_name = acc.get("selected_token_name")
        if not move_key_id:
            found = _find_token_in_group(groups, acc.get("selected_group") or group_name)
            if not found:
                found = _find_any_token(groups)
            if found:
                move_key_id = found.get("id")
                move_key_name = found.get("name") or ""

        remote_ok = False
        remote_msgs = []
        eff_token, eff_uid = token, user_id

        # 4. 同步远程：把该密钥移动到目标分组（失败时自动重登再试一次）
        if token and move_key_id and target_group_id:
            ok, err = _pool_update_key_group(
                acc["pool_type"], acc["base_url"], token,
                move_key_id, target_group_id, user_id
            )
            if ok:
                remote_ok = True
                remote_msgs.append("远程密钥分组已更新")
            else:
                new_token, new_uid, login_err = _pool_relogin_with_fallback(acc)
                if new_token:
                    _update_pool_field(pool_id, access_token=new_token, user_id=new_uid)
                    eff_token, eff_uid = new_token, new_uid
                    ok2, err2 = _pool_update_key_group(
                        acc["pool_type"], acc["base_url"], new_token,
                        move_key_id, target_group_id, new_uid
                    )
                    if ok2:
                        remote_ok = True
                        remote_msgs.append("远程密钥分组已更新（已自动重登）")
                    else:
                        remote_msgs.append(f"远程同步失败：{err2}")
                else:
                    remote_msgs.append(f"远程同步失败（{err}，重登也失败：{login_err}）")

            # 5. 远程成功 → 更新本地选中的密钥信息。
            #    newapi 额外获取完整 key（GET /api/token/ 返回的是脱敏 key）
            if remote_ok:
                full_key = None
                if acc["pool_type"] == "newapi":
                    full_key, _ = _pool_get_full_token_key(
                        acc["pool_type"], acc["base_url"],
                        eff_token, move_key_id, eff_uid
                    )
                _update_pool_field(
                    pool_id,
                    selected_token_id=str(move_key_id),
                    selected_token_name=move_key_name or None,
                    selected_token_key=full_key or acc.get("selected_token_key"),
                )
        elif not token:
            remote_msgs.append("未登录远程站点，仅本地切换分组（远程未同步）")
        elif not move_key_id:
            remote_msgs.append("该账号下没有可用密钥，仅本地切换分组（远程未同步）")

        # 6. 刷新分组缓存（让远程变更立即反映），并验证密钥是否真的已到目标分组
        if eff_token and remote_ok:
            try:
                fresh_groups, _ = _pool_get_groups(
                    acc["pool_type"], acc["base_url"],
                    eff_token, eff_uid
                )
                if fresh_groups is not None:
                    now = datetime.now().isoformat()
                    _update_pool_field(pool_id, groups=fresh_groups, groups_updated_at=now)
                    remote_msgs.append("缓存已刷新")

                    # 验证密钥当前所在的远端分组，发现未生效时给出可操作的提示
                    def _locate_token(g_list):
                        for g in g_list or []:
                            if not isinstance(g, dict):
                                continue
                            for t in (g.get("tokens") or g.get("keys") or []):
                                if isinstance(t, dict) and str(t.get("id")) == str(move_key_id):
                                    return t.get("_group_name") or t.get("_group_id") or g.get("name")
                            for sub in (g.get("sub_groups") or []):
                                if isinstance(sub, dict):
                                    found = _locate_token([sub])
                                    if found:
                                        return found
                        return None

                    actual_group = _locate_token(fresh_groups)
                    if actual_group is not None and str(actual_group) != str(target_group_id) and str(actual_group) != str(group_name):
                        remote_msgs.append(
                            f"警告：远端密钥仍在分组「{actual_group}」而非「{group_name}」，"
                            "站点可能缓存了旧分组，请到站点令牌页手动保存一次以强制刷新"
                        )
            except Exception:
                pass

        msg = f"已选择分组「{group_name}」"
        if remote_msgs:
            msg += "，" + "；".join(remote_msgs)

        return {
            "ok": True,
            "message": msg,
            "selected_group": group_name,
            "remote_updated": remote_ok,
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
            ok, err = _pool_update_key_group(acc["pool_type"], acc["base_url"], token, key_id, target_group_id, acc.get("user_id"))
            if not ok:
                # Token 可能失效 → 尝试重新登录
                if "401" in err or "HTTP 4" in err:
                    need_relogin = True
                else:
                    msg = err
        else:
            need_relogin = True

        if need_relogin:
            token2, user_id2, err_login = _pool_relogin_with_fallback(acc)
            if not token2:
                return {"ok": False, "message": f"登录失败，无法修改分组：{err_login}"}
            _update_pool_field(pool_id, access_token=token2, user_id=user_id2)
            ok2, err2 = _pool_update_key_group(acc["pool_type"], acc["base_url"], token2, key_id, target_group_id, user_id2)
            if not ok2:
                return {"ok": False, "message": f"远程修改分组失败：{err2}"}
        elif msg:
            return {"ok": False, "message": f"远程修改分组失败：{msg}"}

        # 成功后重新读取远端分组，确认密钥确实已经切换，而不是仅返回 HTTP 200。
        groups_new, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token or token2, acc.get("user_id") or locals().get("user_id2"))
        if groups_new is not None and acc["pool_type"] == "newapi":
            remote_group = None
            def find_remote_group(group_list):
                for group in group_list or []:
                    for item in group.get("tokens") or []:
                        if str(item.get("id")) == str(key_id):
                            return item.get("_group_id") or item.get("_group_name") or group.get("name")
                    found = find_remote_group(group.get("sub_groups") or [])
                    if found is not None:
                        return found
                return None
            remote_group = find_remote_group(groups_new)
            if remote_group is not None and str(remote_group) != str(target_group_id) and str(remote_group) != str(target_group_name):
                return {"ok": False, "message": f"远程接口未生效：密钥当前仍在分组「{remote_group}」，目标是「{target_group_name or target_group_id}」"}
        now = datetime.now().isoformat()
        if groups_new is not None:
            _update_pool_field(pool_id, groups=groups_new, groups_updated_at=now)
        else:
            # 刷新失败没关系，标记一下下次要强制刷新
            _update_pool_field(pool_id, groups_updated_at="")

        name_hint = target_group_name or str(target_group_id)
        # 把最新分组摘要一起返回，前端拿到可直接同步到列表卡片，无需重拉列表
        groups_for_summary = groups_new if groups_new is not None else (acc.get("groups") or [])
        groups_summary = [
            {"name": g.get("name"), "count": g.get("count", (len(g.get("tokens") or [])) if isinstance(g, dict) else 0)}
            for g in groups_for_summary[:10]
        ]
        groups_count = len(groups_for_summary)
        groups_updated_at_val = now if groups_new is not None else (acc.get("groups_updated_at") or "")
        return {
            "ok": True,
            "message": f"已将密钥 #{key_id} 移动到分组「{name_hint}」",
            "target_group_id": target_group_id,
            "target_group_name": target_group_name,
            "refreshed_groups": groups_new is not None,
            "groups_summary": groups_summary,
            "groups_count": groups_count,
            "groups_updated_at": groups_updated_at_val,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "message": f"服务器异常：{e}"}, 500


@app.route("/api/pool/accounts/<pool_id>/clear-selection", methods=["POST"])
def api_pool_clear_selection(pool_id):
    """兼容旧接口：清除账号当前分组选择；同时清理旧版本遗留的密钥选择字段。"""
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
            # 登录：支持用户名密码，也支持手动绑定的 GitHub/OAuth access_token
            token, user_id, err = _pool_relogin_with_fallback(acc)
            if not token:
                r["login_ok"] = False
                r["login_err"] = err
                results.append(r)
                return
            r["login_ok"] = True
            # 余额
            balance, b_err = _pool_get_balance(acc["pool_type"], acc["base_url"], token, user_id or acc.get("user_id"))
            if balance is not None:
                r["balance"] = balance
                r["balance_ok"] = True
            else:
                r["balance_ok"] = False
                r["balance_err"] = b_err
            # 分组
            groups, g_err = _pool_get_groups(acc["pool_type"], acc["base_url"], token, user_id or acc.get("user_id"))
            if groups is not None:
                r["groups_count"] = len(groups)
                r["groups_ok"] = True
            else:
                r["groups_ok"] = False
                r["groups_err"] = g_err
            # 保存
            now = datetime.now().isoformat()
            fields = {"access_token": token, "user_id": user_id}
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


# 在模块加载时初始化数据库（支持 gunicorn）
try:
    _init_db()
    _migrate_old_data()
except Exception as _e:
    print(f"[ERROR] 数据库初始化失败: {_e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)