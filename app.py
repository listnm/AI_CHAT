"""
AI 对话网页应用 - Flask 后端
提供 /chat 流式接口，代理到用户指定的 OpenAI 兼容 API
"""

import os
import json
import requests
from flask import Flask, render_template, request, Response, stream_with_context

app = Flask(__name__)


@app.route("/")
def index():
    """渲染主页面"""
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    """
    流式对话接口
    接收前端传来的 API 地址、Key、模型名、对话消息，代理请求到真实的 API，
    以 SSE (Server-Sent Events) 格式逐块返回响应。
    """
    data = request.get_json(force=True)

    # 从请求体中提取参数
    api_url = data.get("api_url", "")
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
        def error_gen():
            yield f"data: {json.dumps({'error': f'请求失败: {str(e)}'})}\n\n"
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