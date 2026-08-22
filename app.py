"""
中转站管理池 · 本地精简版

一个纯本地的 AI 中转站管理工具：
- 管理多个中转站（名称 / Base URL / API Key / 模型列表 / 备注）
- 一键测速（轻量模式，走 /models 不消耗 token，不支持时自动回退到同步对话）
- OpenAI 兼容转发接口 /v1/chat/completions，自动选择默认 / 最快的中转站，
  失败自动切换到下一个可用中转站（failover）
- 数据存本地 SQLite，Key 用 Fernet 加密存储，默认只监听 127.0.0.1

用法：双击 start.bat（自动建虚拟环境、装依赖、启动并打开浏览器）
"""

import os
import sys
import json
import time
import uuid
import base64
import hashlib
import sqlite3
import threading
import requests
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
except ImportError:
    psycopg = None
    dict_row = None
    Json = None

from flask import (Flask, render_template, request, Response,
                   stream_with_context, session, redirect)
from providers import (
    claude_to_openai, openai_to_claude, relay_stream_claude,
    gemini_to_openai, openai_to_gemini, relay_stream_gemini,
)
from oauth_providers import (
    PROVIDERS, generate_state, build_authorize_url, exchange_code_for_tokens,
    refresh_access_token, fetch_user_info, extract_user_info,
    _generate_pkce_openai, _generate_pkce_standard,
    import_token, grok_password_login,
)

# ================================================================
#  基础配置（可用环境变量覆盖，start.bat 里可改）
# ================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PyInstaller onefile 会把只读资源解压到临时目录；用户数据必须放到
# 稳定且可写的 LocalAppData，避免重启后数据库和加密密钥丢失。
if getattr(sys, "frozen", False):
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
    DATA_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "中转站管理池",
    )
else:
    RESOURCE_DIR = BASE_DIR
    DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "data.db")
DATABASE_DIR = DATA_DIR

_IS_RENDER = bool(os.environ.get("RENDER"))
HOST = os.environ.get("HOST", "0.0.0.0" if _IS_RENDER else "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY")
if not PROXY_API_KEY:
    if _IS_RENDER:
        raise RuntimeError("生产环境必须设置 PROXY_API_KEY")
    PROXY_API_KEY = "sk-local-" + hashlib.md5(ADMIN_PASSWORD.encode()).hexdigest()[:16]

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
if not FLASK_SECRET_KEY:
    if _IS_RENDER:
        raise RuntimeError("生产环境必须设置 FLASK_SECRET_KEY")
    FLASK_SECRET_KEY = "local-relay-pool-secret-key"

if _IS_RENDER and ADMIN_PASSWORD == "admin123":
    raise RuntimeError("生产环境不能使用默认 ADMIN_PASSWORD")


app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, "templates"))
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=_IS_RENDER,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
# 让 JSON 接口输出中文而非 \uXXXX
try:
    app.json.ensure_ascii = False  # Flask 3
except Exception:
    app.config["JSON_AS_ASCII"] = False  # Flask 2


# ================================================================
#  API Key 加密 / 解密（优先 Fernet，失败降级 base64）
# ================================================================

try:
    from cryptography.fernet import Fernet as _Fernet

    def _get_encryption_key() -> bytes:
        env_key = os.environ.get("ENCRYPTION_KEY")
        if env_key:
            return env_key.encode()
        if _IS_RENDER:
            raise RuntimeError("生产环境必须设置 ENCRYPTION_KEY")
        key_file = os.path.join(DATA_DIR, ".encryption_key")
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
    print(f"[WARN] 加密初始化失败 ({_e})，降级为 base64 编码")

    def _encrypt(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    def _decrypt(text: str) -> str:
        return base64.b64decode(text.encode()).decode()


# ================================================================
#  数据库（Render 使用 PostgreSQL，本地默认 SQLite）
# ================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_USE_PG = bool(DATABASE_URL)
_PG_POOL = None

_STATION_COLUMNS = (
    "id, name, base_url, api_key_encrypted, models, selected_model, "
    "latency_ms, last_test_at, is_default, remark, group_name, is_active, created_at, updated_at, sso_token_encrypted"
)
_UPDATEABLE_FIELDS = {
    "name", "base_url", "models", "selected_model", "latency_ms",
    "last_test_at", "is_default", "remark", "api_key_encrypted",
    "group_name", "is_active", "sso_token_encrypted",
}


def _utc_now():
    return datetime.now(timezone.utc)


def _db_now():
    now = _utc_now()
    return now if _USE_PG else now.isoformat(timespec="seconds")


def _db_datetime(value):
    if not _USE_PG or value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def get_db():
    if _USE_PG:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL 已设置，但未安装 psycopg[binary]")
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return conn

    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _close_db(conn, commit=False):
    try:
        if commit:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()


def _sql(query: str) -> str:
    return query.replace("?", "%s") if _USE_PG else query


def _models_value(models):
    if _USE_PG:
        return Json(models or [])
    return json.dumps(models or [])


def _models_from_row(value):
    if isinstance(value, str):
        return json.loads(value or "[]")
    return value or []


def init_db():
    conn = get_db()
    try:
        if _USE_PG:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    models JSONB NOT NULL DEFAULT '[]'::jsonb,
                    selected_model TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER,
                    last_test_at TIMESTAMPTZ,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    remark TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS stations_one_default_idx "
                "ON stations (is_default) WHERE is_default = TRUE"
            )
            # 兼容旧表：为已存在的表添加新列
            for col, typedef in [
                ("group_name", "TEXT NOT NULL DEFAULT ''"),
                ("is_active", "BOOLEAN NOT NULL DEFAULT TRUE"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE stations ADD COLUMN {col} {typedef}")
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    models TEXT DEFAULT '[]',
                    selected_model TEXT DEFAULT '',
                    latency_ms INTEGER,
                    last_test_at TEXT,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    remark TEXT DEFAULT '',
                    group_name TEXT DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for col, default in [("selected_model", "''"), ("group_name", "''"), ("is_active", "1"), ("sso_token_encrypted", "''")]:
                try:
                    conn.execute(f"ALTER TABLE stations ADD COLUMN {col} DEFAULT {default}")
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

        # OAuth 账号表
        if _USE_PG:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS oauth_accounts (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    access_token_encrypted TEXT NOT NULL,
                    refresh_token_encrypted TEXT DEFAULT '',
                    token_type TEXT NOT NULL DEFAULT 'Bearer',
                    expires_at TEXT,
                    scope TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)
            conn.commit()
            for col, typedef in [("model", "TEXT DEFAULT ''"), ("sso_token_encrypted", "TEXT DEFAULT ''")]:
                try:
                    cur = conn.execute(f"ALTER TABLE oauth_accounts ADD COLUMN {col} {typedef}")
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS oauth_accounts (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    display_name TEXT DEFAULT '',
                    access_token_encrypted TEXT NOT NULL,
                    refresh_token_encrypted TEXT DEFAULT '',
                    token_type TEXT DEFAULT 'Bearer',
                    expires_at TEXT,
                    scope TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            for col in ["model", "sso_token_encrypted"]:
                try:
                    conn.execute(f"ALTER TABLE oauth_accounts ADD COLUMN {col} TEXT DEFAULT ''")
                except Exception:
                    pass
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn, commit=False)
        raise


# Gunicorn 导入 app 时也必须完成幂等初始化。
init_db()


def _row_to_station(row) -> dict:
    sso_token = ""
    try:
        val = row["sso_token_encrypted"]
        if val:
            sso_token = _decrypt(val)
    except Exception:
        pass
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "api_key": _decrypt(row["api_key_encrypted"]),
        "models": _models_from_row(row["models"]),
        "selected_model": row["selected_model"] or "",
        "latency_ms": row["latency_ms"],
        "last_test_at": row["last_test_at"],
        "is_default": bool(row["is_default"]),
        "remark": row["remark"] or "",
        "group_name": row["group_name"] or "",
        "is_active": bool(row["is_active"]),
        "sso_token": sso_token,
    }


def load_stations() -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            _sql(f"SELECT {_STATION_COLUMNS} FROM stations ORDER BY is_default DESC, latency_ms ASC")
            if not _USE_PG else
            f"SELECT {_STATION_COLUMNS} FROM stations ORDER BY is_default DESC, latency_ms ASC NULLS LAST"
        ).fetchall()
        return [_row_to_station(r) for r in rows]
    finally:
        _close_db(conn)


def find_station(station_id: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute(
            _sql(f"SELECT {_STATION_COLUMNS} FROM stations WHERE id = ?"),
            (station_id,),
        ).fetchone()
        return _row_to_station(row) if row else None
    finally:
        _close_db(conn)


def save_station(st: dict):
    conn = get_db()
    try:
        sso_val = st.get("sso_token", "")
        conn.execute(
            _sql(
                f"""INSERT INTO stations
                ({_STATION_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            ),
            (
                st["id"], st["name"], st["base_url"], _encrypt(st["api_key"]),
                _models_value(st.get("models", [])), st.get("selected_model", ""),
                st.get("latency_ms"), _db_datetime(st.get("last_test_at")),
                bool(st.get("is_default")), st.get("remark", ""),
                st.get("group_name", ""), bool(st.get("is_active", True)),
                _db_datetime(st.get("created_at")) or _db_now(), _db_now(),
                _encrypt(sso_val) if sso_val else "",
            ),
        )
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise


def update_station(station_id: str, fields: dict):
    """按白名单字段更新单个中转站。"""
    if not fields:
        return
    invalid = set(fields) - _UPDATEABLE_FIELDS
    if invalid:
        raise ValueError(f"不允许更新字段: {', '.join(sorted(invalid))}")
    conn = get_db()
    try:
        names = list(fields)
        sets = ", ".join(f"{k} = ?" for k in names)
        values = [
            _models_value(fields[k]) if k == "models" else fields[k]
            for k in names
        ]
        values.extend([_db_now(), station_id])
        conn.execute(
            _sql(f"UPDATE stations SET {sets}, updated_at = ? WHERE id = ?"),
            values,
        )
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise


def delete_station(station_id: str):
    conn = get_db()
    try:
        conn.execute(_sql("DELETE FROM stations WHERE id = ?"), (station_id,))
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise


# ================================================================
#  URL 规范化
# ================================================================

def normalize_chat_url(url: str) -> str:
    """把 Base URL 补全为 /v1/chat/completions 地址"""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if url.endswith(suffix):
            return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def models_url_of(chat_url: str) -> str:
    return chat_url.replace("/chat/completions", "/models")


# ================================================================
#  测速（轻量模式，不消耗 token；不支持 /models 时回退到同步对话）
# ================================================================

def test_station(st: dict):
    """
    测速单个中转站。返回 (ok, latency_ms, message, model_ids)。
    轻量模式优先调 GET /models：可顺带刷新模型列表，且不消耗 token。
    某些中转站不实现 /models，此时回退到 stream=False + max_tokens=1 的
    同步对话，同样基本不消耗 token。
    """
    api_url = normalize_chat_url(st["base_url"])
    headers = {"Authorization": f"Bearer {st['api_key']}", "Content-Type": "application/json"}

    # ---- 优先轻量：GET /models ----
    try:
        start = time.time()
        resp = requests.get(models_url_of(api_url), headers=headers, timeout=10)
        if resp.status_code in (404, 405):
            raise requests.exceptions.HTTPError("upstream has no /models")
        resp.raise_for_status()
        data = resp.json()
        model_ids = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
        if not model_ids:
            raise requests.exceptions.HTTPError("bad /models response")
        latency = int((time.time() - start) * 1000)
        return True, latency, f"连接正常（/models，{len(model_ids)} 个模型，不消耗 token）", model_ids
    except requests.exceptions.HTTPError:
        pass  # 上游不支持 /models，走回退
    except requests.exceptions.Timeout:
        return False, None, "连接超时，请检查地址或网络", None
    except requests.exceptions.ConnectionError:
        return False, None, "无法连接，请检查地址或网络", None
    except requests.exceptions.RequestException as e:
        return False, None, f"请求失败：{str(e)[:80]}", None
    except Exception as e:
        return False, None, f"测试失败：{str(e)[:80]}", None

    # ---- 回退：同步对话验证（stream=False + max_tokens=1） ----
    model = st["models"][0] if st.get("models") else ""
    if not model:
        return False, None, "不支持 /models 且没有模型可验证（请先手动填模型或检查地址）", None
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        start = time.time()
        resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not (data.get("choices")):
            return False, None, f"响应格式异常：{json.dumps(data)[:120]}", None
        latency = int((time.time() - start) * 1000)
        return True, latency, "连接正常（同步对话回退，不消耗 token）", None
    except requests.exceptions.Timeout:
        return False, None, "连接超时，请检查地址或网络", None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 401:
            return False, None, "认证失败（401），请检查 API Key", None
        if status == 404:
            return False, None, "地址不存在（404），请检查地址", None
        return False, None, f"HTTP {status}：{e.response.text[:120] if e.response is not None else ''}", None
    except requests.exceptions.ConnectionError:
        return False, None, "无法连接，请检查地址或网络", None
    except Exception as e:
        return False, None, f"测试失败：{str(e)[:80]}", None


def _apply_test_result(st: dict, ok: bool, latency, message: str, model_ids):
    """把测速结果写回内存对象（之后由调用方统一落库）"""
    st["latency_ms"] = latency if ok else None
    st["last_test_at"] = datetime.now().isoformat(timespec="seconds")
    if model_ids:
        st["models"] = model_ids
    return {
        "id": st["id"],
        "name": st["name"],
        "ok": ok,
        "latency_ms": latency,
        "message": message,
        "models": st["models"],
    }


# ================================================================
#  页面与登录
# ================================================================

def _is_admin() -> bool:
    return bool(session.get("admin_logged_in"))


def _proxy_url():
    """根据当前请求自动构建转发地址（Render / 本地均正确）"""
    return request.host_url.rstrip("/") + "/v1/chat/completions"


@app.route("/")
def index():
    """主页 = 游乐场，无需登录"""
    return render_template(
        "playground.html",
        proxy_key=PROXY_API_KEY,
        proxy_url=_proxy_url(),
        logged_in=_is_admin(),
    )


@app.route("/playground")
def playground():
    return redirect("/")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
        return redirect("/admin")
    if not _is_admin():
        db_info = "PostgreSQL（Render）" if _USE_PG else "本地 SQLite (data.db)"
        return render_template("admin.html", logged_in=False, error="",
                               proxy_key="", proxy_url="", db_info=db_info)
    db_info = "PostgreSQL（Render）" if _USE_PG else "本地 SQLite (data.db)"
    return render_template(
        "admin.html",
        logged_in=True,
        proxy_key=PROXY_API_KEY,
        proxy_url=_proxy_url(),
        db_info=db_info,
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


def _require_admin():
    if not _is_admin():
        return {"ok": False, "message": "请先登录"}, 401
    return None


# ================================================================
#  中转站管理 API
# ================================================================

@app.route("/api/stations", methods=["GET"])
def api_stations_list():
    """中转站列表（Key 脱敏，公开接口供游乐场使用）"""
    stations = load_stations()
    safe = []
    for s in stations:
        safe.append({
            "id": s["id"],
            "name": s["name"],
            "base_url": s["base_url"],
            "api_key_preview": (s["api_key"][:8] + "****") if s["api_key"] else "",
            "has_key": bool(s["api_key"]),
            "models": s["models"],
            "selected_model": s["selected_model"],
            "latency_ms": s["latency_ms"],
            "last_test_at": s["last_test_at"],
            "is_default": s["is_default"],
            "remark": s["remark"],
            "group_name": s["group_name"],
            "is_active": s["is_active"],
        })
    return {"ok": True, "stations": safe}


@app.route("/api/stations", methods=["POST"])
def api_stations_add():
    guard = _require_admin()
    if guard:
        return guard
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    remark = (data.get("remark") or "").strip()
    selected_model = (data.get("selected_model") or "").strip()

    if not name:
        return {"ok": False, "message": "请输入名称"}
    if not base_url:
        return {"ok": False, "message": "请输入 Base URL"}
    if not api_key:
        return {"ok": False, "message": "请输入 API Key"}

    st = {
        "id": str(uuid.uuid4()),
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "models": [],
        "selected_model": selected_model,
        "latency_ms": None,
        "last_test_at": None,
        "is_default": False,
        "remark": remark,
        "group_name": (data.get("group_name") or "").strip(),
        "is_active": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_station(st)
    return {"ok": True, "message": f"中转站「{name}」已添加", "id": st["id"]}


@app.route("/api/stations/<station_id>", methods=["PUT"])
def api_stations_update(station_id):
    guard = _require_admin()
    if guard:
        return guard
    st = find_station(station_id)
    if not st:
        return {"ok": False, "message": "中转站不存在"}
    data = request.get_json(force=True) or {}
    fields = {}
    if "name" in data and data["name"].strip():
        fields["name"] = data["name"].strip()
    if "base_url" in data and data["base_url"].strip():
        fields["base_url"] = data["base_url"].strip()
    if "remark" in data:
        fields["remark"] = data["remark"].strip()
    if "selected_model" in data:
        fields["selected_model"] = (data["selected_model"] or "").strip()
    if data.get("api_key", "").strip():
        # 需要先拿到明文再加密入库，走 save 逻辑
        st["api_key"] = data["api_key"].strip()
        fields.pop("api_key_encrypted", None)
    if fields:
        update_station(station_id, fields)
    if data.get("api_key", "").strip():
        update_station(station_id, {"api_key_encrypted": _encrypt(st["api_key"])})
    # 核心信息变更后清除旧的延迟记录
    if any(k in fields for k in ("name", "base_url")) or data.get("api_key", "").strip():
        update_station(station_id, {"latency_ms": None, "last_test_at": None})
    return {"ok": True, "message": "已保存"}


@app.route("/api/stations/<station_id>", methods=["DELETE"])
def api_stations_delete(station_id):
    guard = _require_admin()
    if guard:
        return guard
    delete_station(station_id)
    return {"ok": True, "message": "已删除"}


@app.route("/api/stations/<station_id>/default", methods=["POST"])
def api_stations_set_default(station_id):
    guard = _require_admin()
    if guard:
        return guard
    if not find_station(station_id):
        return {"ok": False, "message": "中转站不存在"}
    conn = get_db()
    try:
        conn.execute(_sql("UPDATE stations SET is_default = FALSE" if _USE_PG else "UPDATE stations SET is_default = 0"))
        conn.execute(_sql("UPDATE stations SET is_default = TRUE WHERE id = ?"), (station_id,))
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise
    return {"ok": True, "message": "已设为默认中转站"}


@app.route("/api/stations/<station_id>/default", methods=["DELETE"])
def api_stations_unset_default(station_id):
    """取消默认：不设默认后，转发时自动按延迟从低到高选最快的中转站"""
    guard = _require_admin()
    if guard:
        return guard
    if not find_station(station_id):
        return {"ok": False, "message": "中转站不存在"}
    update_station(station_id, {"is_default": False})
    return {"ok": True, "message": "已取消默认，转发时将自动选择最快的中转站"}


@app.route("/api/stations/<station_id>/reveal", methods=["POST"])
def api_stations_reveal(station_id):
    """返回完整 API Key（仅本地管理员使用，供界面查看/复制）"""
    guard = _require_admin()
    if guard:
        return guard
    st = find_station(station_id)
    if not st:
        return {"ok": False, "message": "中转站不存在"}
    return {"ok": True, "api_key": st["api_key"]}


@app.route("/api/stations/<station_id>/test", methods=["POST"])
def api_stations_test(station_id):
    guard = _require_admin()
    if guard:
        return guard
    st = find_station(station_id)
    if not st:
        return {"ok": False, "message": "中转站不存在"}
    ok, latency, message, model_ids = test_station(st)
    _apply_test_result(st, ok, latency, message, model_ids)
    conn = get_db()
    try:
        conn.execute(
            _sql("UPDATE stations SET latency_ms = ?, last_test_at = ?, models = ? WHERE id = ?"),
            (st["latency_ms"], st["last_test_at"], _models_value(st["models"]), station_id),
        )
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise
    return {"ok": ok, "message": message, "latency_ms": latency, "models": st["models"]}


@app.route("/api/test-all", methods=["POST"])
def api_test_all():
    guard = _require_admin()
    if guard:
        return guard
    stations = load_stations()
    if not stations:
        return {"ok": False, "message": "中转站池为空，请先添加"}

    results = [None] * len(stations)

    def work(i: int, st: dict):
        ok, latency, message, model_ids = test_station(st)
        results[i] = _apply_test_result(st, ok, latency, message, model_ids)

    threads = [threading.Thread(target=work, args=(i, s)) for i, s in enumerate(stations)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = get_db()
    try:
        for st in stations:
            conn.execute(
                _sql("UPDATE stations SET latency_ms = ?, last_test_at = ?, models = ? WHERE id = ?"),
                (st["latency_ms"], st["last_test_at"], _models_value(st["models"]), st["id"]),
            )
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise

    results.sort(key=lambda r: (0 if r["ok"] else 1, r["latency_ms"] if r["latency_ms"] is not None else 99999))
    return {"ok": True, "results": results}


# ================================================================
#  渠道管理 API（启用/禁用、分组、批量操作）
# ================================================================

@app.route("/api/channels/<channel_id>/toggle", methods=["POST"])
def api_channel_toggle(channel_id):
    """启用/禁用渠道"""
    guard = _require_admin()
    if guard:
        return guard
    st = find_station(channel_id)
    if not st:
        return {"ok": False, "message": "渠道不存在"}
    new_status = not st["is_active"]
    update_station(channel_id, {"is_active": new_status})
    return {"ok": True, "is_active": new_status, "message": f"已{'启用' if new_status else '禁用'}「{st['name']}」"}


@app.route("/api/channels/<channel_id>/group", methods=["PUT"])
def api_channel_group(channel_id):
    """设置渠道分组"""
    guard = _require_admin()
    if guard:
        return guard
    st = find_station(channel_id)
    if not st:
        return {"ok": False, "message": "渠道不存在"}
    data = request.get_json(force=True) or {}
    group_name = (data.get("group_name") or "").strip()
    update_station(channel_id, {"group_name": group_name})
    return {"ok": True, "message": f"已将「{st['name']}」移至「{group_name or '默认分组'}」"}


@app.route("/api/channels/groups", methods=["GET"])
def api_channel_groups():
    """获取所有分组列表"""
    guard = _require_admin()
    if guard:
        return guard
    stations = load_stations()
    groups = {}
    for s in stations:
        g = s["group_name"] or "默认分组"
        if g not in groups:
            groups[g] = {"name": g, "count": 0, "active_count": 0}
        groups[g]["count"] += 1
        if s["is_active"]:
            groups[g]["active_count"] += 1
    return {"ok": True, "groups": list(groups.values())}


@app.route("/api/channels/batch", methods=["POST"])
def api_channels_batch():
    """批量操作渠道"""
    guard = _require_admin()
    if guard:
        return guard
    data = request.get_json(force=True) or {}
    action = data.get("action", "")
    ids = data.get("ids", [])

    if not ids:
        return {"ok": False, "message": "请选择要操作的渠道"}
    if action not in ("test", "delete", "enable", "disable", "set_group"):
        return {"ok": False, "message": "不支持的操作"}

    stations = load_stations()
    id_set = set(ids)
    targets = [s for s in stations if s["id"] in id_set]

    if action == "delete":
        for st in targets:
            delete_station(st["id"])
        return {"ok": True, "message": f"已删除 {len(targets)} 个渠道"}

    if action in ("enable", "disable"):
        val = action == "enable"
        for st in targets:
            update_station(st["id"], {"is_active": val})
        return {"ok": True, "message": f"已{'启用' if val else '禁用'} {len(targets)} 个渠道"}

    if action == "set_group":
        group_name = (data.get("group_name") or "").strip()
        for st in targets:
            update_station(st["id"], {"group_name": group_name})
        return {"ok": True, "message": f"已将 {len(targets)} 个渠道移至「{group_name or '默认分组'}」"}

    if action == "test":
        # 并行测速
        results = [None] * len(targets)
        def work(i, st):
            ok, latency, message, model_ids = test_station(st)
            results[i] = _apply_test_result(st, ok, latency, message, model_ids)
        threads = [threading.Thread(target=work, args=(i, s)) for i, s in enumerate(targets)]
        for t in threads: t.start()
        for t in threads: t.join()
        # 写入数据库
        conn = get_db()
        try:
            for st in targets:
                conn.execute(
                    _sql("UPDATE stations SET latency_ms = ?, last_test_at = ?, models = ? WHERE id = ?"),
                    (st["latency_ms"], st["last_test_at"], _models_value(st["models"]), st["id"]),
                )
            _close_db(conn, commit=True)
        except Exception:
            _close_db(conn)
            raise
        ok_n = sum(1 for r in results if r and r["ok"])
        return {"ok": True, "message": f"测速完成：{ok_n}/{len(targets)} 个可用", "results": results}

    return {"ok": False, "message": "未知操作"}


# ================================================================
#  游乐场对话接口（SSE 流式，管理会话认证，直接走指定中转站）
# ================================================================

def _sse_error(message: str) -> Response:
    def gen():
        yield f"data: {json.dumps({'error': {'message': message}})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _relay_sse(upstream: requests.Response):
    """把上游 SSE 流原样透传给前端（data: 行，UTF-8 安全）"""
    buffer = b""
    for chunk in upstream.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith(b"data: "):
                content = line[6:]
                if content.strip() == b"[DONE]":
                    yield "data: [DONE]\n\n"
                    return
                yield b"data: " + content + b"\n\n"
    if buffer.strip():
        if buffer.startswith(b"data: "):
            dc = buffer[6:]
            if dc.strip() != b"[DONE]":
                yield b"data: " + dc + b"\n\n"
        else:
            yield b"data: " + buffer + b"\n\n"
    yield "data: [DONE]\n\n"


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """游乐场对话：直接向指定中转站转发（stream=True），SSE 返回（无需登录）"""
    data = request.get_json(force=True) or {}
    station_id = data.get("station_id", "")
    model = (data.get("model") or "").strip()
    messages = data.get("messages", [])

    if not messages:
        return _sse_error("消息不能为空")

    # 自动选择中转站：指定 id → 用指定的；未指定 → 默认中转站 → 按延迟排序第一个
    st = None
    if station_id:
        st = find_station(station_id)
    if not st:
        stations = load_stations()
        st = next((s for s in stations if s["is_default"]), stations[0] if stations else None)
    if not st:
        return _sse_error("没有可用的中转站，请先在管理后台添加")

    if not model:
        model = st["selected_model"] or (st["models"][0] if st["models"] else "")
    if not model:
        return _sse_error("该中转站没有可用模型，请先在管理页测速获取模型列表")

    api_url = normalize_chat_url(st["base_url"])
    if not api_url or not st["api_key"]:
        return _sse_error("中转站地址或 Key 为空")

    headers = {"Authorization": f"Bearer {st['api_key']}", "Content-Type": "application/json"}
    # Grok CLI 代理需要特殊 headers
    if "cli-chat-proxy.grok.com" in st.get("base_url", "") or "grok" in st.get("remark", "").lower():
        headers.update({
            "X-Grok-Client-Version": "1.0.0",
            "User-Agent": "GrokCLI/1.0",
            "Accept": "application/json, text/event-stream",
        })
    payload = {"model": model, "messages": messages, "stream": True}
    if data.get("temperature") is not None:
        try:
            payload["temperature"] = float(data["temperature"])
        except (TypeError, ValueError):
            pass
    if data.get("max_tokens"):
        try:
            payload["max_tokens"] = int(data["max_tokens"])
        except (TypeError, ValueError):
            pass

    try:
        upstream = requests.post(api_url, headers=headers, json=payload, stream=True, timeout=(30, 600))
        upstream.raise_for_status()
        upstream.encoding = "utf-8"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        body = ""
        if e.response is not None:
            try:
                err = e.response.json().get("error", {})
                body = err.get("message", "") if isinstance(err, dict) else str(err)
            except Exception:
                body = e.response.text[:300]
        return _sse_error(body or f"上游返回 HTTP {status}")
    except requests.exceptions.Timeout:
        return _sse_error("连接超时，请检查中转站地址或网络")
    except requests.exceptions.RequestException as e:
        return _sse_error(f"请求失败：{str(e)[:150]}")

    return Response(
        stream_with_context(_relay_sse(upstream)),
        mimetype="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ================================================================
#  OpenAI 兼容转发接口
# ================================================================

def _ordered_candidates(stations: list, account_name: str) -> list:
    """决定转发时的尝试顺序：
    - 指定了 account → 只尝试该中转站
    - 未指定 → 默认中转站优先，其余按延迟从低到高（作为 failover 顺序）
    """
    if account_name:
        for s in stations:
            if s["name"].strip() == account_name.strip():
                return [s]
        return []
    ordered = [s for s in stations if s["is_default"]]
    rest = [s for s in stations if not s["is_default"]]
    rest.sort(key=lambda s: (s["latency_ms"] if s["latency_ms"] is not None else 99999))
    ordered.extend(rest)
    return ordered


def _ascii_header(value: str) -> str:
    """响应头只允许 ASCII，账号名/模型名含中文时做百分号编码"""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return quote(value, safe="")


def _relay_stream(upstream: requests.Response, station: dict):
    """把上游 SSE 流原样转发（严格 OpenAI 兼容，只输出 data: 行）"""
    buffer = b""
    for chunk in upstream.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith(b"data: "):
                content = line[6:]
                if content.strip() == b"[DONE]":
                    yield b"data: [DONE]\n\n"
                    return
                yield b"data: " + content + b"\n\n"
    # 上游关闭连接但没发 [DONE]，补一个干净的结尾
    if buffer.strip():
        if buffer.startswith(b"data: "):
            dc = buffer[6:]
            if dc.strip() != b"[DONE]":
                yield b"data: " + dc + b"\n\n"
        else:
            yield b"data: " + buffer + b"\n\n"
    yield b"data: [DONE]\n\n"


# ================================================================
#  Grok 网页版 REST API（直接调用 grok.com，不走 CLI 代理）
# ================================================================

import uuid as _uuid

def _grok_web_headers(sso_token):
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Cookie": f"sso={sso_token};sso-rw={sso_token}",
        "x-xai-request-id": str(_uuid.uuid4()),
    }


def _grok_web_chat(sso_token, model, messages, stream=True):
    """
    通过 grok.com REST API 发送聊天请求。
    返回 OpenAI 兼容的 (status_code, headers, generator) 或 (status_code, headers, json_bytes)。
    """
    import requests as req
    import json as _json

    # 将 messages 转成单一 message 文本
    text_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        text_parts.append(f"[{role}]\n{content}")
    message = "\n\n".join(text_parts)

    url = "https://grok.com/rest/app-chat/conversations/new"
    body = {
        "temporary": False,
        "modelName": model,
        "message": message,
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": False,
        "enableImageGeneration": True,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": False,
        "imageGenerationCount": 1,
        "forceConcise": False,
        "toolOverrides": {
            "imageGen": False, "webSearch": True, "xSearch": True,
            "xMediaSearch": True, "trendsSearch": True, "xPostAnalyze": True,
        },
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "customPersonality": "",
        "deepsearchPreset": "",
        "isReasoning": False,
        "disableTextFollowUps": True,
    }

    try:
        r = req.post(url, headers=_grok_web_headers(sso_token), json=body, stream=stream, timeout=(30, 600))
    except Exception as e:
        return 502, {}, _json.dumps({"error": {"message": str(e)[:200]}}).encode()

    if r.status_code != 200:
        err_msg = r.text[:300]
        return r.status_code, {}, _json.dumps({"error": {"message": err_msg}}).encode()

    if not stream:
        # 非流式：累积所有 token 后返回
        full_text = ""
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = _json.loads(line)
                token = obj.get("result", {}).get("response", {}).get("token", "")
                if token:
                    full_text += token
            except Exception:
                pass
        resp_body = _json.dumps({
            "id": f"chatcmpl-{_uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }).encode()
        return 200, {"Content-Type": "application/json"}, resp_body

    # 流式：逐行解析 JSON，转成 OpenAI SSE 格式
    def stream_gen():
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = _json.loads(line)
                token = obj.get("result", {}).get("response", {}).get("token", "")
                if token:
                    chunk = {
                        "id": f"chatcmpl-{_uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
                    }
                    yield f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n"
            except Exception:
                pass
        yield "data: [DONE]\n\n"

    return 200, {"Content-Type": "text/event-stream"}, stream_gen()


@app.route("/v1/chat/completions", methods=["POST"])
def proxy_chat_completions():
    """
    OpenAI 兼容转发端点，认证用 Bearer {PROXY_API_KEY}。

    请求体约定：
    - model = "auto" 或留空 → 使用中转站配置的第一个模型
    - 传 "account": "中转站名称" → 只走该中转站
    - 不传 account → 默认中转站优先，失败自动切换下一个（failover）
    """
    auth = request.headers.get("Authorization", "")
    if auth != "Bearer " + PROXY_API_KEY:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}}, 401

    data = request.get_json(force=True) or {}
    account_name = data.get("account", "")
    model = data.get("model", "")
    is_auto = not model or model == "auto"
    messages = data.get("messages", [])
    stream = bool(data.get("stream", False))

    if not messages:
        return {"error": {"message": "messages is required", "type": "invalid_request", "code": 400}}, 400

    stations = load_stations()
    if not stations:
        return {"error": {"message": "中转站池为空，请先在管理页添加", "type": "no_station", "code": 503}}, 503

    candidates = _ordered_candidates(stations, account_name)
    if not candidates:
        return {"error": {"message": f"找不到名为「{account_name}」的中转站", "type": "not_found", "code": 404}}, 404

    # 额度/用量相关的错误关键词，触发模型轮询
    _QUOTA_KEYWORDS = ("usage", "quota", "limit", "credit", "exceed", "used all", "free usage", "滚动")

    def _is_quota_error(status, body):
        if status in (429,):
            return True
        if status and 400 <= status < 500:
            bl = (body or "").lower()
            return any(kw in bl for kw in _QUOTA_KEYWORDS)
        return False

    last_error = "unknown"
    for st in candidates:
        # 如果中转站有 SSO token，直接走 grok.com 网页 API（独立配额）
        sso_token = st.get("sso_token", "")
        if sso_token and ("grok" in st.get("remark", "").lower() or "grok" in st.get("base_url", "").lower()):
            if is_auto:
                model_list = st.get("models") or []
                if st.get("selected_model"):
                    model_list = [st["selected_model"]] + [m for m in model_list if m != st["selected_model"]]
            else:
                model_list = [model]
            for try_model in (model_list or ["grok-3"]):
                status, resp_headers, body = _grok_web_chat(sso_token, try_model, messages, stream)
                if status == 200:
                    if stream:
                        return Response(body, mimetype="text/event-stream",
                                        headers={"X-Provider-Name": st["name"], "X-Provider-Model": try_model})
                    else:
                        return Response(body, mimetype="application/json",
                                        headers={"X-Provider-Name": st["name"], "X-Provider-Model": try_model})
                last_error = f"{try_model}: HTTP {status}"
                print(f"[Proxy] grok.com {try_model} 失败: {last_error}")
            continue

        api_url = normalize_chat_url(st["base_url"])
        if not api_url or not st["api_key"]:
            last_error = f"{st['name']} 地址或 Key 为空"
            continue

        headers = {"Authorization": f"Bearer {st['api_key']}", "Content-Type": "application/json"}
        is_grok = "cli-chat-proxy.grok.com" in st.get("base_url", "") or "grok" in st.get("remark", "").lower()
        if is_grok:
            headers.update({
                "X-Grok-Client-Version": "1.0.0",
                "User-Agent": "GrokCLI/1.0",
                "Accept": "application/json, text/event-stream",
            })

        # 构建候选模型列表（用于轮询）
        if is_auto:
            model_list = st.get("models") or []
            if st.get("selected_model"):
                # selected_model 放第一位，其余跟上
                model_list = [st["selected_model"]] + [m for m in model_list if m != st["selected_model"]]
        else:
            model_list = [model]

        if not model_list:
            last_error = f"{st['name']} 没有可用模型"
            continue

        for try_model in model_list:
            payload = {k: v for k, v in data.items() if k != "account"}
            payload["model"] = try_model
            payload["messages"] = messages
            payload["stream"] = stream

            try:
                upstream = requests.post(
                    api_url, headers=headers, json=payload, stream=stream, timeout=(30, 600),
                )
                upstream.raise_for_status()
                upstream.encoding = "utf-8"
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                body = ""
                if e.response is not None:
                    try:
                        err = e.response.json().get("error", {})
                        body = err.get("message", "") if isinstance(err, dict) else str(err)
                    except Exception:
                        body = e.response.text[:200]
                # 额度用完 → 尝试下一个模型
                if _is_quota_error(status, body):
                    print(f"[Proxy] 模型 {try_model} 额度用完，尝试下一个...")
                    last_error = f"{try_model}: {body[:80]}"
                    continue
                # 4xx 其他错误 → 不换模型，直接返回
                if status and status < 500 and status != 429:
                    return {"error": {"message": body or f"HTTP {status}", "type": "upstream_error", "code": status}}, status
                # 5xx → 换下一个模型
                last_error = f"HTTP {status}" + (f"：{body[:80]}" if body else "")
                continue
            except requests.exceptions.RequestException as e:
                last_error = str(e)[:100]
                continue

            # 成功
            if stream:
                return Response(_relay_stream(upstream, st), mimetype="text/event-stream",
                                headers={"X-Provider-Name": st["name"], "X-Provider-Model": try_model})
            else:
                resp = Response(upstream.content, mimetype="application/json",
                                headers={"X-Provider-Name": st["name"], "X-Provider-Model": try_model})
                return resp
    return {"error": {"message": f"所有中转站均不可用：{last_error}", "type": "all_failed", "code": 502}}, 502


@app.route("/v1/models", methods=["GET"])
def proxy_models():
    """给客户端看的模型列表：合并池内所有中转站的模型（去重）"""
    auth = request.headers.get("Authorization", "")
    if auth != "Bearer " + PROXY_API_KEY:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}}, 401
    merged, seen = [], set()
    for s in load_stations():
        for m in s.get("models", []):
            if m not in seen:
                seen.add(m)
                merged.append(m)
    if not merged:
        return {"object": "list", "data": [{"id": "auto", "object": "model"}]}
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in merged]}


# ================================================================
#  多供应商认证（Claude / Gemini / OpenAI 统一）
# ================================================================

def _authenticate() -> str | None:
    """
    从请求中提取 API Key 并验证。
    支持多种认证方式：
    - Authorization: Bearer {key}（OpenAI / Claude）
    - x-api-key: {key}（Claude）
    - key={key} 查询参数（Gemini）
    - x-goog-api-key: {key}（Gemini）
    返回 None 表示认证通过，返回字符串表示错误 JSON。
    """
    auth = request.headers.get("Authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    goog_key = request.headers.get("x-goog-api-key", "")
    query_key = request.args.get("key", "")

    api_key = ""
    if auth.startswith("Bearer "):
        api_key = auth[7:]
    elif x_api_key:
        api_key = x_api_key
    elif goog_key:
        api_key = goog_key
    elif query_key:
        api_key = query_key

    if api_key != PROXY_API_KEY:
        return {"error": {"message": "Invalid API Key", "type": "auth_error", "code": 401}}
    return None


def _forward_to_upstream(openai_payload: dict, stream: bool):
    """
    复用现有逻辑：选择中转站 → 转发 OpenAI 格式请求 → 返回上游响应。
    返回 (upstream_response, station_dict) 或抛出异常。
    失败时直接返回 Flask Response（已含错误信息）。
    """
    account_name = openai_payload.pop("account", "")
    model = openai_payload.get("model", "")
    is_auto = not model or model == "auto"

    stations = load_stations()
    if not stations:
        return None, None, {"error": {"message": "中转站池为空", "type": "no_station", "code": 503}}, 503

    candidates = _ordered_candidates(stations, account_name)
    if not candidates:
        return None, None, {"error": {"message": f"找不到名为「{account_name}」的中转站", "type": "not_found", "code": 404}}, 404

    last_error = "unknown"
    for st in candidates:
        api_url = normalize_chat_url(st["base_url"])
        if not api_url or not st["api_key"]:
            last_error = f"{st['name']} 地址或 Key 为空"
            continue
        effective_model = (
            st["selected_model"] or (st["models"][0] if st["models"] else "")
        ) if is_auto else model
        if not effective_model:
            last_error = f"{st['name']} 没有可用模型"
            continue

        headers = {"Authorization": f"Bearer {st['api_key']}", "Content-Type": "application/json"}
        payload = {k: v for k, v in openai_payload.items() if k != "account"}
        payload["model"] = effective_model
        payload["stream"] = stream

        try:
            upstream = requests.post(
                api_url, headers=headers, json=payload, stream=stream, timeout=(30, 600),
            )
            upstream.raise_for_status()
            upstream.encoding = "utf-8"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            body = ""
            if e.response is not None:
                try:
                    err = e.response.json().get("error", {})
                    body = err.get("message", "") if isinstance(err, dict) else str(err)
                except Exception:
                    body = e.response.text[:200]
            if status and status < 500 and status != 429:
                return None, None, {"error": {"message": body or f"HTTP {status}", "type": "upstream_error", "code": status}}, status
            last_error = f"HTTP {status}" + (f"：{body[:80]}" if body else "")
            continue
        except requests.exceptions.Timeout:
            last_error = f"{st['name']} 连接超时"
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"{st['name']}：{str(e)[:100]}"
            continue

        return upstream, st, None, None

    return None, None, {"error": {"message": f"所有中转站均不可用：{last_error}", "type": "all_failed", "code": 502}}, 502


# ================================================================
#  Claude Messages API（/v1/messages）
# ================================================================

@app.route("/v1/messages", methods=["POST"])
def claude_messages():
    """
    Anthropic Claude Messages API 兼容端点。
    客户端可用 Claude SDK 直接调用。
    """
    auth_err = _authenticate()
    if auth_err:
        return auth_err, 401

    body = request.get_json(force=True) or {}
    stream = body.get("stream", False)

    # 转换为 OpenAI 格式
    openai_payload = claude_to_openai(body)

    # 转发
    upstream, st, err, status = _forward_to_upstream(openai_payload, stream)
    if err:
        return err, status

    provider_headers = {
        "X-Provider-Name": _ascii_header(st["name"]),
        "X-Provider-Model": _ascii_header(openai_payload.get("model", "")),
    }

    if stream:
        return Response(
            stream_with_context(relay_stream_claude(upstream)),
            mimetype="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive", **provider_headers},
        )

    # 非流式：转换响应
    try:
        openai_resp = upstream.json()
    except Exception:
        return upstream.content, upstream.status_code, {"Content-Type": "application/json"}

    claude_resp = openai_to_claude(openai_resp)
    return app.response_class(
        response=json.dumps(claude_resp, ensure_ascii=False).encode("utf-8"),
        mimetype="application/json; charset=utf-8",
        headers=provider_headers,
    )


# ================================================================
#  Gemini API（/v1beta/models/<model>:*）
# ================================================================

def _gemini_core(model_name: str, stream: bool):
    """Gemini API 核心处理逻辑（非流式和流式共用）"""
    auth_err = _authenticate()
    if auth_err:
        return auth_err, 401

    body = request.get_json(force=True) or {}

    # 转换为 OpenAI 格式
    openai_payload = gemini_to_openai(body, model_name)

    # 转发
    upstream, st, err, status = _forward_to_upstream(openai_payload, stream)
    if err:
        return err, status

    provider_headers = {
        "X-Provider-Name": _ascii_header(st["name"]),
        "X-Provider-Model": _ascii_header(openai_payload.get("model", "")),
    }

    if stream:
        return Response(
            stream_with_context(relay_stream_gemini(upstream)),
            mimetype="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive", **provider_headers},
        )

    # 非流式：转换响应
    try:
        openai_resp = upstream.json()
    except Exception:
        return upstream.content, upstream.status_code, {"Content-Type": "application/json"}

    gemini_resp = openai_to_gemini(openai_resp)
    return app.response_class(
        response=json.dumps(gemini_resp, ensure_ascii=False).encode("utf-8"),
        mimetype="application/json; charset=utf-8",
        headers=provider_headers,
    )


@app.route("/v1beta/models/<model>:generateContent", methods=["POST"])
@app.route("/v1/models/<model>:generateContent", methods=["POST"])
def gemini_generate(model):
    """Gemini 非流式 API"""
    return _gemini_core(model, stream=False)


@app.route("/v1beta/models/<model>:streamGenerateContent", methods=["POST"])
@app.route("/v1/models/<model>:streamGenerateContent", methods=["POST"])
def gemini_stream(model):
    """Gemini 流式 API"""
    return _gemini_core(model, stream=True)


@app.route("/healthz")
def healthz():
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        _close_db(conn)
        return {"ok": True, "status": "healthy"}
    except Exception:
        return {"ok": False, "status": "unhealthy"}, 503


# ================================================================
#  OAuth 订阅账号管理
# ================================================================

_oauth_states = {}  # {state: provider_key}，CSRF 防护

OAUTH_COLUMNS = (
    "id, provider, email, display_name, access_token_encrypted, "
    "refresh_token_encrypted, token_type, expires_at, scope, model, is_active, created_at, updated_at"
)


def _oauth_row_to_dict(row) -> dict:
    refresh_token = row["refresh_token_encrypted"] and _decrypt(row["refresh_token_encrypted"])
    sso_token = ""
    try:
        val = row["sso_token_encrypted"]
        if val:
            sso_token = _decrypt(val)
    except Exception:
        pass
    return {
        "id": row["id"],
        "provider": row["provider"],
        "email": row["email"],
        "display_name": row["display_name"],
        "access_token": _decrypt(row["access_token_encrypted"]),
        "refresh_token": refresh_token,
        "has_refresh_token": bool(refresh_token),
        "token_type": row["token_type"],
        "expires_at": row["expires_at"],
        "scope": row["scope"],
        "model": row["model"] or "",
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "sso_token": sso_token,
    }


def _load_oauth_accounts() -> list:
    conn = get_db()
    try:
        try:
            rows = conn.execute(f"SELECT {OAUTH_COLUMNS}, sso_token_encrypted FROM oauth_accounts ORDER BY created_at DESC").fetchall()
        except Exception:
            rows = conn.execute(f"SELECT {OAUTH_COLUMNS} FROM oauth_accounts ORDER BY created_at DESC").fetchall()
        return [_oauth_row_to_dict(r) for r in rows]
    finally:
        _close_db(conn)


def _find_oauth_account(account_id: str) -> dict | None:
    conn = get_db()
    try:
        try:
            row = conn.execute(
                _sql(f"SELECT {OAUTH_COLUMNS}, sso_token_encrypted FROM oauth_accounts WHERE id = ?"),
                (account_id,),
            ).fetchone()
        except Exception:
            row = conn.execute(
                _sql(f"SELECT {OAUTH_COLUMNS} FROM oauth_accounts WHERE id = ?"),
                (account_id,),
            ).fetchone()
        return _oauth_row_to_dict(row) if row else None
    finally:
        _close_db(conn)


def _auto_refresh_expired_tokens(accounts: list) -> int:
    """自动刷新即将过期的令牌（提前 1 小时刷新），返回刷新数量"""
    from datetime import datetime, timezone, timedelta
    refreshed = 0
    now = datetime.now(timezone.utc)
    threshold = now + timedelta(hours=1)  # 提前 1 小时刷新

    for acc in accounts:
        if not acc.get("is_active") or not acc.get("refresh_token"):
            continue
        expires_at = acc.get("expires_at", "")
        if not expires_at:
            continue
        try:
            exp_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp_time > threshold:
                continue  # 还没过期，跳过
        except Exception:
            continue

        # 令牌即将过期，尝试刷新
        provider = acc["provider"]
        token_data = refresh_access_token(provider, acc["refresh_token"], acc.get("sso_token", ""))
        if "error" in token_data:
            print(f"[OA] 自动刷新失败 {acc.get('email', acc['id'])}: {token_data['error']}")
            continue

        new_access = token_data.get("access_token", "")
        new_refresh = token_data.get("refresh_token", acc["refresh_token"])
        new_sso = token_data.get("sso_token", "")
        expires_in = token_data.get("expires_in", 0)
        new_expires_at = (now + timedelta(seconds=expires_in)).isoformat() if expires_in else ""

        if not new_access:
            print(f"[OA] 自动刷新返回空 access_token: {acc.get('email', acc['id'])}")
            continue

        conn = get_db()
        try:
            update_fields = "access_token_encrypted=?, refresh_token_encrypted=?, expires_at=?, updated_at=?"
            update_values = [_encrypt(new_access), _encrypt(new_refresh) if new_refresh else "",
                             new_expires_at, _db_now()]
            if new_sso:
                update_fields += ", sso_token_encrypted=?"
                update_values.append(_encrypt(new_sso))
            update_values.append(acc["id"])
            conn.execute(
                _sql(f"UPDATE oauth_accounts SET {update_fields} WHERE id=?"),
                update_values)
            _close_db(conn, commit=True)
            refreshed += 1
            print(f"[OA] 自动刷新成功: {acc.get('email', acc['id'])}")
        except Exception:
            _close_db(conn)

    return refreshed


@app.route("/oa")
def oa_page():
    """OAuth 账号管理页面"""
    if not _is_admin():
        return redirect("/admin")
    accounts = _load_oauth_accounts()

    # 自动刷新即将过期的令牌
    refreshed = _auto_refresh_expired_tokens(accounts)
    if refreshed:
        accounts = _load_oauth_accounts()  # 重新加载

    provider_configs = {}
    for key, cfg in PROVIDERS.items():
        cid = cfg.get("client_id", "")
        provider_configs[key] = {
            "name": cfg["name"],
            "desc": cfg["desc"],
            "icon": cfg["icon"],
            "color": cfg["color"],
            "client_id_preview": cid[:12] + "****" if len(cid) > 12 else cid,
        }
    return render_template("oa.html", accounts=accounts, providers=provider_configs,
                           refreshed_count=refreshed)


@app.route("/oa/login/<provider>")
def oa_login(provider):
    """跳转到 OAuth 授权页面（带 PKCE）"""
    if not _is_admin():
        return redirect("/admin")
    if provider not in PROVIDERS:
        return "未知提供商", 404

    cfg = PROVIDERS[provider]
    redirect_uri = request.host_url.rstrip("/") + cfg["callback_path"]

    # 生成 state + PKCE
    state = generate_state()
    if provider == "openai":
        verifier, challenge = _generate_pkce_openai()
    else:
        verifier, challenge = _generate_pkce_standard()

    # 存到 session（回调时取回 code_verifier）
    session["oauth_state"] = state
    session["oauth_provider"] = provider
    session["oauth_verifier"] = verifier

    auth_url = build_authorize_url(provider, redirect_uri, state, challenge)
    return redirect(auth_url)


@app.route("/oa/callback/<provider>")
def oa_callback(provider):
    """OAuth 回调：用 code + PKCE verifier 换取令牌"""
    if provider not in PROVIDERS:
        return "未知提供商", 404

    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return redirect(f"/oa?error={error}")
    if not code or state != session.get("oauth_state"):
        return redirect("/oa?error=invalid_state")

    verifier = session.pop("oauth_verifier", "")
    session.pop("oauth_state", None)
    session.pop("oauth_provider", None)

    cfg = PROVIDERS[provider]
    redirect_uri = request.host_url.rstrip("/") + cfg["callback_path"]

    # 换取令牌
    token_data = exchange_code_for_tokens(provider, code, redirect_uri, verifier)
    if "error" in token_data:
        return redirect(f"/oa?error={token_data['error']}")

    access_token = token_data.get("access_token", "")
    refresh_token_val = token_data.get("refresh_token", "")
    if not access_token:
        return redirect("/oa?error=no_access_token")

    # 提取用户信息
    raw_info = fetch_user_info(provider, access_token)
    info = extract_user_info(provider, token_data, raw_info)

    expires_in = info["expires_in"]
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else ""

    # 存入数据库
    conn = get_db()
    try:
        account_id = str(uuid.uuid4())
        now = _db_now()
        conn.execute(
            _sql("""INSERT INTO oauth_accounts
                (id, provider, email, display_name, access_token_encrypted,
                 refresh_token_encrypted, token_type, expires_at, scope,
                 is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
            (account_id, provider, info["email"], info["display_name"],
             _encrypt(access_token),
             _encrypt(refresh_token_val) if refresh_token_val else "",
             "Bearer", expires_at, info["scope"], True, now, now),
        )
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise

    return redirect("/oa?success=connected")


@app.route("/api/oa/accounts", methods=["GET"])
def oa_accounts_list():
    guard = _require_admin()
    if guard:
        return guard
    accounts = _load_oauth_accounts()
    safe = [{
        "id": a["id"], "provider": a["provider"], "email": a["email"],
        "display_name": a["display_name"], "expires_at": a["expires_at"],
        "is_active": a["is_active"], "created_at": a["created_at"],
        "has_refresh_token": bool(a["refresh_token"]),
    } for a in accounts]
    return {"ok": True, "accounts": safe}


@app.route("/api/oa/import", methods=["POST"])
def oa_import_token():
    """通过粘贴 Token/JSON 导入订阅账号（支持 sub2api 导出格式）"""
    guard = _require_admin()
    if guard:
        return guard
    data = request.get_json(force=True) or {}
    provider = data.get("provider", "")
    content = data.get("content", "").strip()

    if not content:
        return {"ok": False, "message": "请输入 Token 或 JSON 数据"}

    # 尝试从 JSON 中提取 platform（sub2api 导出格式）
    if not provider or provider not in PROVIDERS:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("accounts"):
                first = parsed["accounts"][0]
                provider = first.get("platform", "")
        except Exception:
            pass

    if provider not in PROVIDERS:
        return {"ok": False, "message": "未知提供商，请手动选择"}

    try:
        # sub2api 导出格式：提取 accounts 数组
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("accounts"):
                entries = parsed["accounts"]
                results = []
                for entry in entries[:1]:
                    from oauth_providers import normalize_import_entry, validate_token
                    normalized = normalize_import_entry(entry)
                    if not normalized.get("access_token"):
                        results.append({"ok": False, "message": "未找到 access_token"})
                        continue
                    validation = validate_token(provider, normalized["access_token"])
                    if not validation["valid"]:
                        results.append({"ok": False, "message": f"Token 无效: {validation['error']}"})
                        continue
                    results.append({
                        "ok": True,
                        "access_token": normalized["access_token"],
                        "refresh_token": normalized.get("refresh_token", ""),
                        "id_token": normalized.get("id_token", ""),
                        "email": validation["email"] or normalized["email"],
                        "display_name": entry.get("name", "") or validation["display_name"] or normalized["display_name"],
                        "extra": normalized.get("extra", {}),
                    })
                result = results[0] if results else {"ok": False, "message": "解析失败"}
            else:
                result = import_token(provider, content)
        except json.JSONDecodeError:
            result = import_token(provider, content)
    except Exception as e:
        return {"ok": False, "message": f"解析失败: {str(e)[:100]}"}

    if not result.get("ok"):
        return {"ok": False, "message": result.get("message", "导入失败")}

    # 存入数据库
    from datetime import datetime, timezone, timedelta
    expires_at = ""
    if result.get("extra", {}).get("expires_at"):
        try:
            exp_ts = result["extra"]["expires_at"]
            if exp_ts > 0:
                expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat()
        except Exception:
            pass

    conn = get_db()
    try:
        account_id = str(uuid.uuid4())
        now = _db_now()
        conn.execute(
            _sql("""INSERT INTO oauth_accounts
                (id, provider, email, display_name, access_token_encrypted,
                 refresh_token_encrypted, token_type, expires_at, scope,
                 is_active, created_at, updated_at, sso_token_encrypted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
            (account_id, provider, result["email"], result["display_name"],
             _encrypt(result["access_token"]),
             _encrypt(result["refresh_token"]) if result.get("refresh_token") else "",
             "Bearer", expires_at, "", True, now, now,
             _encrypt(result["sso_token"]) if result.get("sso_token") else ""),
        )
        _close_db(conn, commit=True)
    except Exception as e:
        _close_db(conn)
        return {"ok": False, "message": f"数据库错误: {str(e)[:100]}"}

    return {"ok": True, "message": f"已导入 {result['email'] or result['display_name'] or '账号'}"}


@app.route("/api/oa/password-login", methods=["POST"])
def oa_password_login():
    """通过账号密码登录 Grok"""
    guard = _require_admin()
    if guard:
        return guard
    data = request.get_json(force=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return {"ok": False, "message": "请输入邮箱和密码"}

    from oauth_providers import grok_password_login
    result = grok_password_login(email, password)
    if not result.get("ok"):
        return {"ok": False, "message": result.get("message", "登录失败")}

    # 存入数据库
    from datetime import datetime, timezone, timedelta
    expires_at = result.get("expires_at", "")

    conn = get_db()
    try:
        account_id = str(uuid.uuid4())
        now = _db_now()
        conn.execute(
            _sql("""INSERT INTO oauth_accounts
                (id, provider, email, display_name, access_token_encrypted,
                 refresh_token_encrypted, token_type, expires_at, scope,
                 is_active, created_at, updated_at, sso_token_encrypted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
            (account_id, "grok", result["email"], result["display_name"],
             _encrypt(result["access_token"]),
             _encrypt(result["refresh_token"]) if result.get("refresh_token") else "",
             "Bearer", expires_at, "", True, now, now,
             _encrypt(result["sso_token"]) if result.get("sso_token") else ""),
        )
        _close_db(conn, commit=True)
    except Exception as e:
        _close_db(conn)
        return {"ok": False, "message": f"数据库错误: {str(e)[:100]}"}

    return {"ok": True, "message": f"已登录 {result['email'] or result['display_name']}"}


@app.route("/api/oa/accounts/<account_id>/sync", methods=["POST"])
def oa_account_sync_to_station(account_id):
    """将 OAuth 账号同步为中转站，使其可用于转发"""
    guard = _require_admin()
    if guard:
        return guard
    acc = _find_oauth_account(account_id)
    if not acc:
        return {"ok": False, "message": "账号不存在"}

    # 每次同步都先刷新令牌，确保中转站拿到最新的 token
    from datetime import datetime, timezone, timedelta
    access_token = acc.get("access_token", "")
    token_data = refresh_access_token(acc["provider"], acc.get("refresh_token", ""), acc.get("sso_token", ""))
    if token_data.get("access_token"):
        access_token = token_data["access_token"]
        # 更新数据库中的令牌
        new_refresh = token_data.get("refresh_token", acc.get("refresh_token", ""))
        new_sso = token_data.get("sso_token", "")
        new_expires = token_data.get("expires_at", "")
        if not new_expires:
            expires_in = token_data.get("expires_in", 0)
            if expires_in:
                new_expires = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        try:
            conn = get_db()
            update_fields = "access_token_encrypted=?, refresh_token_encrypted=?, expires_at=?, updated_at=?"
            update_values = [_encrypt(access_token), _encrypt(new_refresh) if new_refresh else "",
                             new_expires, _db_now()]
            if new_sso:
                update_fields += ", sso_token_encrypted=?"
                update_values.append(_encrypt(new_sso))
            update_values.append(account_id)
            conn.execute(_sql(f"UPDATE oauth_accounts SET {update_fields} WHERE id=?"), update_values)
            _close_db(conn, commit=True)
        except Exception:
            pass

    # 确定 base_url
    # Grok 使用 CLI 代理（网页版后端），不需要 API 额度
    provider = acc["provider"]
    base_urls = {
        "openai": "https://api.openai.com/v1",
        "grok": "https://cli-chat-proxy.grok.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta",
    }
    base_url = base_urls.get(provider, "")

    # 检查是否已同步（同名或同 provider 的 OAuth 中转站）
    existing = None
    for s in load_stations():
        if s["name"] == acc["email"] or s["name"] == acc["display_name"]:
            existing = s
            break
        # 也匹配同 provider 的 OAuth 导入中转站
        if f"OAuth 导入 - {provider}" in (s.get("remark") or ""):
            existing = s
            break

    try:
        # 同步 OA 账号的模型设置到中转站
        oa_model = (acc.get("model") or "").strip()
        if existing:
            update_data = {
                "base_url": base_url,
                "api_key_encrypted": _encrypt(access_token),
            }
            # 同步 SSO token 到中转站（用于 grok.com 网页 API）
            sso_token = acc.get("sso_token", "")
            if sso_token:
                update_data["sso_token_encrypted"] = _encrypt(sso_token)
            if oa_model:
                update_data["selected_model"] = oa_model
            update_station(existing["id"], update_data)
            return {"ok": True, "message": f"已更新中转站「{existing['name']}」" + (f"，模型: {oa_model}" if oa_model else "")}
        else:
            name = acc["email"] or acc["display_name"] or f"{provider} OAuth"
            st = {
                "id": str(uuid.uuid4()),
                "name": name,
                "base_url": base_url,
                "api_key": access_token,
                "sso_token": acc.get("sso_token", ""),
                "models": [],
                "selected_model": oa_model,
                "latency_ms": None,
                "last_test_at": None,
                "is_default": False,
                "remark": f"OAuth 导入 - {provider}",
                "group_name": "OAuth",
                "is_active": True,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            save_station(st)
            return {"ok": True, "message": f"已创建中转站「{name}」" + (f"，模型: {oa_model}" if oa_model else "")}
    except Exception as e:
        return {"ok": False, "message": f"同步失败: {str(e)[:100]}"}


@app.route("/api/oa/accounts/<account_id>/model", methods=["PUT"])
def oa_account_set_model(account_id):
    """设置 OAuth 账号的模型，并同步到对应的中转站"""
    guard = _require_admin()
    if guard:
        return guard
    data = request.get_json(force=True) or {}
    model = data.get("model", "").strip()
    conn = get_db()
    try:
        conn.execute(
            _sql("UPDATE oauth_accounts SET model=?, updated_at=? WHERE id=?"),
            (model, _db_now(), account_id))
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise

    # 同步模型到对应的中转站（通过名称或 remark 匹配）
    try:
        acc = _find_oauth_account(account_id)
        if acc:
            provider = acc.get("provider", "")
            stations = load_stations()
            for s in stations:
                if s["name"] == acc["email"] or s["name"] == acc["display_name"]:
                    update_station(s["id"], {"selected_model": model})
                    break
                if f"OAuth 导入 - {provider}" in (s.get("remark") or ""):
                    update_station(s["id"], {"selected_model": model})
                    break
    except Exception:
        pass  # 同步失败不影响模型保存

    return {"ok": True, "message": f"模型已保存: {model or '自动'}"}


@app.route("/api/oa/accounts/<account_id>", methods=["DELETE"])
def oa_account_delete(account_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    try:
        conn.execute(_sql("DELETE FROM oauth_accounts WHERE id = ?"), (account_id,))
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise
    return {"ok": True, "message": "已删除"}


@app.route("/api/oa/accounts/<account_id>/toggle", methods=["POST"])
def oa_account_toggle(account_id):
    guard = _require_admin()
    if guard:
        return guard
    acc = _find_oauth_account(account_id)
    if not acc:
        return {"ok": False, "message": "账号不存在"}
    new_status = not acc["is_active"]
    conn = get_db()
    try:
        conn.execute(
            _sql("UPDATE oauth_accounts SET is_active=?, updated_at=? WHERE id=?"),
            (new_status, _db_now(), account_id))
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise
    return {"ok": True, "is_active": new_status, "message": f"已{'启用' if new_status else '禁用'}"}


@app.route("/api/oa/accounts/<account_id>/refresh", methods=["POST"])
def oa_account_refresh(account_id):
    guard = _require_admin()
    if guard:
        return guard
    acc = _find_oauth_account(account_id)
    if not acc:
        return {"ok": False, "message": "账号不存在"}
    if not acc["refresh_token"]:
        return {"ok": False, "message": "该账号无 refresh_token，请重新授权登录"}

    print(f"[OA] 手动刷新令牌: {acc.get('email', acc['id'])} (provider: {acc['provider']})")
    print(f"[OA] refresh_token 长度: {len(acc['refresh_token'])} 前20字符: {acc['refresh_token'][:20]}...")
    token_data = refresh_access_token(acc["provider"], acc["refresh_token"], acc.get("sso_token", ""))
    if "error" in token_data:
        print(f"[OA] 手动刷新失败: {token_data['error']}")
        hint = ""
        if acc["provider"] == "grok" and not acc.get("sso_token"):
            hint = " | 请通过「密码登录」重新授权以启用自动刷新"
        return {"ok": False, "message": f"刷新失败: {token_data['error']}{hint}"}

    from datetime import datetime, timezone, timedelta

    new_access = token_data.get("access_token", "")
    new_refresh = token_data.get("refresh_token", acc["refresh_token"])
    new_sso = token_data.get("sso_token", "")
    expires_in = token_data.get("expires_in", 0)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else acc["expires_at"]

    conn = get_db()
    try:
        update_fields = "access_token_encrypted=?, refresh_token_encrypted=?, expires_at=?, updated_at=?"
        update_values = [_encrypt(new_access), _encrypt(new_refresh) if new_refresh else "",
                         expires_at, _db_now()]
        if new_sso:
            update_fields += ", sso_token_encrypted=?"
            update_values.append(_encrypt(new_sso))
        update_values.append(account_id)
        conn.execute(
            _sql(f"UPDATE oauth_accounts SET {update_fields} WHERE id=?"),
            update_values)
        _close_db(conn, commit=True)
    except Exception:
        _close_db(conn)
        raise
    return {"ok": True, "message": "令牌已刷新"}


# ================================================================
#  启动
# ================================================================

def _wait_server_ready(timeout: float = 15) -> bool:
    """等待 Flask 服务就绪（桌面模式打开窗口前调用）"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def run_desktop():
    """桌面客户端模式：Flask 跑在后台线程，pywebview 弹出原生窗口"""
    try:
        import webview
    except Exception as e:
        print(f"[desktop] 缺少 pywebview：{e}")
        print("[desktop] 请先安装：.venv\\Scripts\\pip install pywebview")
        print("[desktop] 或改用网页模式：python app.py（浏览器访问）")
        input("按回车退出...")
        sys.exit(1)

    def serve():
        try:
            app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
        except Exception as e:
            print(f"[desktop] 服务启动失败：{e}")

    threading.Thread(target=serve, daemon=True).start()
    if not _wait_server_ready():
        print("[desktop] 服务未在预期时间内就绪，窗口可能显示异常")

    webview.create_window(
        "中转站管理池",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=840,
        min_size=(1000, 660),
        background_color="#0b1020",
    )
    webview.start()
    print("客户端窗口已关闭，正在退出...")
    os._exit(0)


if __name__ == "__main__":
    init_db()
    print("=" * 52)
    print("  中转站管理池 · 本地客户端")
    print(f"  管理页   : http://{HOST}:{PORT}")
    print(f"  转发接口 : http://{HOST}:{PORT}/v1/chat/completions")
    print(f"  管理密码 : {ADMIN_PASSWORD}（用环境变量 ADMIN_PASSWORD 修改）")
    print(f"  转发密钥 : {PROXY_API_KEY}")
    print("  按 Ctrl+C 停止")
    print("=" * 52)
    if "--desktop" in sys.argv or os.environ.get("DESKTOP") == "1":
        run_desktop()
    else:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
