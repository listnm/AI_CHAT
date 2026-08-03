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
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, Response, stream_with_context

app = Flask(__name__)

# ================================================================
#  账号存储（JSON 文件，无需数据库）
# ================================================================

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
_accounts_lock = threading.Lock()


def _load_accounts() -> list:
    """从 JSON 文件加载所有账号"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def _save_accounts(accounts: list):
    """保存账号列表到 JSON 文件"""
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


def _find_account(account_id: str) -> dict | None:
    """按 ID 查找账号"""
    for acc in _load_accounts():
        if acc["id"] == account_id:
            return acc
    return None


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
#  账号管理 API
# ================================================================

@app.route("/api/accounts", methods=["GET"])
def api_accounts_list():
    """获取所有账号列表（不返回 API Key 完整内容，仅显示前 8 位）"""
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
    accounts = _load_accounts()
    new_list = [a for a in accounts if a["id"] != account_id]
    if len(new_list) == len(accounts):
        return {"ok": False, "message": "账号不存在"}
    _save_accounts(new_list)
    return {"ok": True, "message": "账号已删除"}


@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_accounts_update(account_id):
    """修改指定账号（名称、URL、Key、模型）"""
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
            # 修改后清除延迟记录，需要重新测速
            acc["latency_ms"] = None
            acc["last_speed_test"] = None
            _save_accounts(accounts)
            return {"ok": True, "message": "账号已更新"}
    return {"ok": False, "message": "账号不存在"}


@app.route("/api/accounts/speedtest", methods=["POST"])
def api_accounts_speedtest():
    """
    对所有账号进行测速（并行请求），返回按延迟排序的结果
    每个账号发送一个简短的 Chat 请求，记录响应时间
    """
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
#  对话管理（服务端存储，每日自动清理旧对话）
# ================================================================

CONVERSATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.json")


def _load_conversations() -> list:
    if not os.path.exists(CONVERSATIONS_FILE):
        return []
    try:
        with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_conversations(convs: list):
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(convs, f, ensure_ascii=False, indent=2)


def _cleanup_old_conversations():
    """删除超过 24 小时的对话"""
    convs = _load_conversations()
    now = datetime.now()
    keep = []
    for c in convs:
        try:
            updated = datetime.fromisoformat(c.get("updated_at", ""))
            if now - updated < timedelta(hours=24):
                keep.append(c)
        except (ValueError, TypeError):
            keep.append(c)
    if len(keep) != len(convs):
        _save_conversations(keep)


@app.route("/api/conversations", methods=["GET"])
def api_conversations_list():
    """获取所有对话列表（不含消息内容，仅元信息）"""
    _cleanup_old_conversations()
    convs = _load_conversations()
    # 按更新时间倒序
    convs.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    summary = [{"id": c["id"], "title": c.get("title", "新对话"), "created_at": c.get("created_at"), "updated_at": c.get("updated_at")} for c in convs]
    return {"ok": True, "conversations": summary}


@app.route("/api/conversations", methods=["POST"])
def api_conversations_create():
    """创建新对话"""
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
    convs = _load_conversations()
    for c in convs:
        if c["id"] == conv_id:
            return {"ok": True, "conversation": c}
    return {"ok": False, "message": "对话不存在"}


@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def api_conversations_delete(conv_id):
    """删除指定对话"""
    convs = _load_conversations()
    new_list = [c for c in convs if c["id"] != conv_id]
    if len(new_list) == len(convs):
        return {"ok": False, "message": "对话不存在"}
    _save_conversations(new_list)
    return {"ok": True, "message": "对话已删除"}


@app.route("/api/conversations/<conv_id>/messages", methods=["PUT"])
def api_conversations_save_messages(conv_id):
    """保存对话的消息列表"""
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
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)