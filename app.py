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
from datetime import datetime
from urllib.parse import quote

from flask import (Flask, render_template, request, Response,
                   stream_with_context, session, redirect)

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
#  数据库（SQLite，单表）
# ================================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        # 兼容旧库：补 selected_model 列
        try:
            conn.execute("ALTER TABLE stations ADD COLUMN selected_model TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            pass  # 列已存在
    finally:
        conn.close()


# Gunicorn 导入 app 时也必须完成幂等初始化。
init_db()


def _row_to_station(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "api_key": _decrypt(row["api_key_encrypted"]),
        "models": json.loads(row["models"] or "[]"),
        "selected_model": row["selected_model"] or "",
        "latency_ms": row["latency_ms"],
        "last_test_at": row["last_test_at"],
        "is_default": bool(row["is_default"]),
        "remark": row["remark"] or "",
    }


def load_stations() -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM stations ORDER BY is_default DESC, latency_ms ASC"
        ).fetchall()
        return [_row_to_station(r) for r in rows]
    finally:
        conn.close()


def find_station(station_id: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        return _row_to_station(row) if row else None
    finally:
        conn.close()


def save_station(st: dict):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO stations
                (id, name, base_url, api_key_encrypted, models, selected_model, latency_ms,
                 last_test_at, is_default, remark, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                st["id"],
                st["name"],
                st["base_url"],
                _encrypt(st["api_key"]),
                json.dumps(st.get("models", [])),
                st.get("selected_model", ""),
                st.get("latency_ms"),
                st.get("last_test_at"),
                1 if st.get("is_default") else 0,
                st.get("remark", ""),
                st.get("created_at") or datetime.now().isoformat(timespec="seconds"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_station(station_id: str, fields: dict):
    """按字段更新单个中转站（fields 里的键对应列名）"""
    conn = get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE stations SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), datetime.now().isoformat(timespec="seconds"), station_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_station(station_id: str):
    conn = get_db()
    try:
        conn.execute("DELETE FROM stations WHERE id = ?", (station_id,))
        conn.commit()
    finally:
        conn.close()


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


@app.route("/")
def index():
    if not _is_admin():
        return redirect("/login")
    return render_template(
        "index.html",
        logged_in=True,
        proxy_key=PROXY_API_KEY,
        proxy_url=f"http://{HOST}:{PORT}/v1/chat/completions",
        port=PORT,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect("/")
        error = "密码错误"
    if _is_admin():
        return redirect("/")
    return render_template("index.html", logged_in=False, error=error,
                           proxy_key="", proxy_url="", port=PORT)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


def _require_admin():
    if not _is_admin():
        return {"ok": False, "message": "请先登录"}, 401
    return None


# ================================================================
#  中转站管理 API
# ================================================================

@app.route("/api/stations", methods=["GET"])
def api_stations_list():
    guard = _require_admin()
    if guard:
        return guard
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
        conn = get_db()
        try:
            conn.execute("UPDATE stations SET api_key_encrypted = ? WHERE id = ?",
                         (_encrypt(st["api_key"]), station_id))
            conn.commit()
        finally:
            conn.close()
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
        conn.execute("UPDATE stations SET is_default = 0")
        conn.execute("UPDATE stations SET is_default = 1 WHERE id = ?", (station_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "message": "已设为默认中转站"}


@app.route("/api/stations/<station_id>/default", methods=["DELETE"])
def api_stations_unset_default(station_id):
    """取消默认：不设默认后，转发时自动按延迟从低到高选最快的中转站"""
    guard = _require_admin()
    if guard:
        return guard
    if not find_station(station_id):
        return {"ok": False, "message": "中转站不存在"}
    update_station(station_id, {"is_default": 0})
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
            "UPDATE stations SET latency_ms = ?, last_test_at = ?, models = ? WHERE id = ?",
            (st["latency_ms"], st["last_test_at"], json.dumps(st["models"]), station_id),
        )
        conn.commit()
    finally:
        conn.close()
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
                "UPDATE stations SET latency_ms = ?, last_test_at = ?, models = ? WHERE id = ?",
                (st["latency_ms"], st["last_test_at"], json.dumps(st["models"]), st["id"]),
            )
        conn.commit()
    finally:
        conn.close()

    results.sort(key=lambda r: (0 if r["ok"] else 1, r["latency_ms"] if r["latency_ms"] is not None else 99999))
    return {"ok": True, "results": results}


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
    """游乐场对话：直接向指定中转站转发（stream=True），SSE 返回"""
    guard = _require_admin()
    if guard:
        return guard
    data = request.get_json(force=True) or {}
    station_id = data.get("station_id", "")
    model = (data.get("model") or "").strip()
    messages = data.get("messages", [])

    if not station_id:
        return _sse_error("请先选择中转站")
    st = find_station(station_id)
    if not st:
        return _sse_error("中转站不存在")
    if not messages:
        return _sse_error("消息不能为空")

    if not model:
        model = st["selected_model"] or (st["models"][0] if st["models"] else "")
    if not model:
        return _sse_error("该中转站没有可用模型，请先在管理页测速获取模型列表")

    api_url = normalize_chat_url(st["base_url"])
    if not api_url or not st["api_key"]:
        return _sse_error("中转站地址或 Key 为空")

    headers = {"Authorization": f"Bearer {st['api_key']}", "Content-Type": "application/json"}
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
            last_error = f"{st['name']} 没有可用模型（请先在管理页测速刷新模型列表，或指定具体模型名）"
            continue

        headers = {"Authorization": f"Bearer {st['api_key']}", "Content-Type": "application/json"}
        payload = {k: v for k, v in data.items() if k != "account"}
        payload["model"] = effective_model
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
            # 4xx 是客户端/Key 的问题，直接返回不换中转站；5xx/429 才切换
            if status and status < 500 and status != 429:
                return {"error": {"message": body or f"HTTP {status}", "type": "upstream_error", "code": status}}, status
            last_error = f"HTTP {status}" + (f"：{body[:80]}" if body else "")
            continue
        except requests.exceptions.Timeout:
            last_error = f"{st['name']} 连接超时"
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"{st['name']}：{str(e)[:100]}"
            continue

        provider_headers = {
            "X-Provider-Name": _ascii_header(st["name"]),
            "X-Provider-Model": _ascii_header(effective_model),
        }
        if stream:
            return Response(
                stream_with_context(_relay_stream(upstream, st)),
                mimetype="text/event-stream; charset=utf-8",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                         "Connection": "keep-alive", **provider_headers},
            )
        return app.response_class(
            response=upstream.content,
            mimetype="application/json; charset=utf-8",
            headers=provider_headers,
        )

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


@app.route("/healthz")
def healthz():
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"ok": True, "status": "healthy"}
    except Exception:
        return {"ok": False, "status": "unhealthy"}, 503


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
