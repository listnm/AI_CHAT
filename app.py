"""
AI 对话网页应用 - Flask 后端
提供 /chat 流式接口，代理到用户指定的 OpenAI 兼容 API
"""

import os
import json
import requests
from flask import Flask, render_template, request, Response, stream_with_context

app = Flask(__name__)


def normalize_api_url(url: str) -> str:
    """
    自动补全 API URL 路径
    如果用户输入的是 base URL（如 https://aihub.top），
    自动追加 /v1/chat/completions
    """
    url = url.rstrip("/")
    # 如果已经包含完整路径，直接返回
    if url.endswith("/chat/completions"):
        return url
    # 如果以 /v1 结尾，补全 chat/completions
    if url.endswith("/v1"):
        return url + "/chat/completions"
    # 否则认为是 base URL，追加 /v1/chat/completions
    return url + "/v1/chat/completions"


@app.route("/")
def index():
    """渲染主页面"""
    return render_template("index.html")


@app.route("/api/check", methods=["POST"])
def api_check():
    """
    API 连通性测试接口
    向用户指定的 API 发送一个简单请求，验证地址、Key、模型是否可用
    """
    data = request.get_json(force=True)
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
        # 检查是否包含预期字段
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


@app.route("/api/models", methods=["POST"])
def api_models():
    """
    获取可用模型列表
    向用户指定 API 的 /v1/models 接口查询可用模型并返回
    """
    data = request.get_json(force=True)
    api_url = data.get("api_url", "")
    api_key = data.get("api_key", "")

    if not api_url or not api_key:
        return {"ok": False, "models": [], "message": "请先填写 API 地址和 Key"}

    # 从 base URL 构造 models 接口地址
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


@app.route("/chat", methods=["POST"])
def chat():
    """
    流式对话接口
    接收前端传来的 API 地址、Key、模型名、对话消息，代理请求到真实的 API，
    以 SSE (Server-Sent Events) 格式逐块返回响应。
    """
    data = request.get_json(force=True)

    # 从请求体中提取参数，并自动补全 URL 路径
    api_url = normalize_api_url(data.get("api_url", ""))
    api_key = data.get("api_key", "")
    model = data.get("model", "")
    messages = data.get("messages", [])

    # 参数校验
    if not api_url or not api_key or not model or not messages:
        def error_gen():
            yield f"data: {json.dumps({'error': '缺少必要参数（api_url、api_key、model、messages）'})}\n\n"
            yield "data: [DONE]\n\n"
        return Response(
            stream_with_context(error_gen()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    # 构造请求头，发送给上游 API
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 请求体，启用流式输出
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
    }

    try:
        # 向上游 API 发起流式请求
        upstream_response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=60,
        )
        upstream_response.raise_for_status()  # 检查 HTTP 状态码
        # 强制使用 UTF-8 解码，避免中文乱码
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
        """逐块读取上游 API 的流式响应，转发给前端"""
        buffer = ""
        for chunk in upstream_response.iter_content(chunk_size=None, decode_unicode=True):
            if not chunk:
                continue
            buffer += chunk
            # 按行分割，处理 SSE 格式
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                # 跳过 SSE 注释
                if line.startswith(":"):
                    continue
                # 解析 data: 开头的行
                if line.startswith("data: "):
                    data_content = line[6:]
                    # 如果是结束标记，直接透传
                    if data_content.strip() == "[DONE]":
                        yield "data: [DONE]\n\n"
                        return
                    # 透传原始数据块给前端
                    yield f"data: {data_content}\n\n"
                # 如果是 event: 或其他 SSE 字段，直接透传（目前只关心 data）
                elif line.startswith("event: "):
                    yield f"{line}\n"
                elif line.startswith("id: "):
                    yield f"{line}\n"
                elif line.startswith("retry: "):
                    yield f"{line}\n"
        # 处理剩余的 buffer
        if buffer.strip():
            if buffer.startswith("data: "):
                data_content = buffer[6:]
                if data_content.strip() != "[DONE]":
                    yield f"data: {data_content}\n\n"
                else:
                    yield "data: [DONE]\n\n"
            else:
                # 非标准格式，尝试作为普通 JSON 数据发送
                yield f"data: {buffer}\n\n"
        # 确保最终发送结束标记
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
    # 从环境变量读取端口，默认 5000，用于 Render 部署
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)