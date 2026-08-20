"""
多供应商格式转换模块

支持 Claude Messages API 和 Gemini API 格式与 OpenAI 格式之间的双向转换。
上游中转站保持 OpenAI 格式不变，本模块在入口层做格式适配。
"""

import json
import uuid
import time


# ================================================================
#  Claude Messages API ↔ OpenAI Chat Completions
# ================================================================

def claude_to_openai(body: dict) -> dict:
    """将 Claude Messages API 请求转换为 OpenAI Chat Completions 格式"""
    messages = []

    # system prompt（Claude 用顶层 system 字段）
    system = body.get("system")
    if system:
        if isinstance(system, list):
            # 多段 system prompt
            system_text = "\n\n".join(
                block.get("text", "") for block in system if block.get("type") == "text"
            )
        else:
            system_text = str(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # 消息转换
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # 多模态内容（text + image）
            parts = []
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    # 转为 OpenAI vision 格式
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{source.get('media_type', 'image/jpeg')};base64,{source.get('data', '')}"
                            }
                        })
                    elif source.get("type") == "url":
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": source.get("url", "")}
                        })
            if len(parts) == 1 and isinstance(parts[0], str):
                messages.append({"role": role, "content": parts[0]})
            else:
                messages.append({"role": role, "content": parts})
        else:
            messages.append({"role": role, "content": str(content)})

    result = {
        "model": body.get("model", ""),
        "messages": messages,
        "stream": body.get("stream", False),
    }

    # 参数映射
    if body.get("max_tokens") is not None:
        result["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        result["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        result["stop"] = body["stop_sequences"]
    if body.get("frequency_penalty") is not None:
        result["frequency_penalty"] = body["frequency_penalty"]
    if body.get("presence_penalty") is not None:
        result["presence_penalty"] = body["presence_penalty"]
    if body.get("metadata"):
        result["metadata"] = body["metadata"]

    return result


def openai_to_claude(openai_resp: dict) -> dict:
    """将 OpenAI Chat Completions 响应转换为 Claude Messages 格式"""
    msg = openai_resp.get("choices", [{}])[0].get("message", {})
    content_text = msg.get("content", "") or ""

    result = {
        "id": openai_resp.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content_text}],
        "model": openai_resp.get("model", ""),
        "stop_reason": _map_stop_reason(openai_resp),
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_resp.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_resp.get("usage", {}).get("completion_tokens", 0),
        },
    }
    return result


def _map_stop_reason(openai_resp: dict) -> str:
    """将 OpenAI stop_reason 映射为 Claude stop_reason"""
    finish = openai_resp.get("choices", [{}])[0].get("finish_reason", "")
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    return mapping.get(finish, "end_turn")


# ================================================================
#  OpenAI SSE → Claude SSE 流式转换
# ================================================================

def relay_stream_claude(upstream_resp):
    """
    将上游 OpenAI SSE 流实时转换为 Claude Messages SSE 格式。

    Claude 流式事件序列：
    - message_start: 消息开始
    - content_block_start: 内容块开始
    - content_block_delta: 内容增量
    - content_block_stop: 内容块结束
    - message_delta: 消息级更新（stop_reason, usage）
    - message_stop: 消息结束
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    model = ""

    # 发送 message_start
    yield _sse_line({
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    })

    # 发送 content_block_start
    yield _sse_line({
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""}
    })

    buffer = b""
    finished = False

    for chunk in upstream_resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data: "):
                continue
            payload = line[6:]
            if payload.strip() == b"[DONE]":
                finished = True
                break
            try:
                evt = json.loads(payload)
            except Exception:
                continue

            # 提取 model
            if not model and evt.get("model"):
                model = evt["model"]

            # 提取增量内容
            delta = evt.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                yield _sse_line({
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": content}
                })

        if finished:
            break

    # 处理残留 buffer
    if buffer.strip() and buffer.startswith(b"data: "):
        payload = buffer[6:]
        if payload.strip() != b"[DONE]":
            try:
                evt = json.loads(payload)
                delta = evt.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield _sse_line({
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": content}
                    })
            except Exception:
                pass

    # content_block_stop
    yield _sse_line({"type": "content_block_stop", "index": 0})

    # message_delta（stop_reason）
    yield _sse_line({
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0}
    })

    # message_stop
    yield _sse_line({"type": "message_stop"})


def _sse_line(data: dict) -> bytes:
    """构造一行 SSE data"""
    return b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n"


# ================================================================
#  Gemini API ↔ OpenAI Chat Completions
# ================================================================

def gemini_to_openai(body: dict, model_name: str = "") -> dict:
    """将 Gemini API 请求转换为 OpenAI Chat Completions 格式"""
    messages = []

    for content in body.get("contents", []):
        role = content.get("role", "user")
        # Gemini 角色映射
        if role == "model":
            role = "assistant"
        elif role == "user":
            role = "user"
        else:
            role = "user"

        parts = content.get("parts", [])
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        text = "\n".join(text_parts) if text_parts else ""

        # 处理图片
        image_parts = [p for p in parts if "inlineData" in p]
        if image_parts and text:
            content_list = [{"type": "text", "text": text}]
            for img in image_parts:
                data = img.get("inlineData", {})
                content_list.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{data.get('mimeType', 'image/jpeg')};base64,{data.get('data', '')}"
                    }
                })
            messages.append({"role": role, "content": content_list})
        elif image_parts:
            content_list = []
            for img in image_parts:
                data = img.get("inlineData", {})
                content_list.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{data.get('mimeType', 'image/jpeg')};base64,{data.get('data', '')}"
                    }
                })
            messages.append({"role": role, "content": content_list})
        else:
            messages.append({"role": role, "content": text})

    # system instruction（Gemini 用顶层 systemInstruction）
    system_instruction = body.get("systemInstruction", {})
    if system_instruction:
        parts = system_instruction.get("parts", [])
        system_text = "\n".join(p.get("text", "") for p in parts if "text" in p)
        if system_text:
            messages.insert(0, {"role": "system", "content": system_text})

    # 参数映射
    gen_config = body.get("generationConfig", {})
    result = {
        "model": model_name or body.get("model", ""),
        "messages": messages,
        "stream": body.get("stream", False),
    }
    if gen_config.get("temperature") is not None:
        result["temperature"] = gen_config["temperature"]
    if gen_config.get("topP") is not None:
        result["top_p"] = gen_config["topP"]
    if gen_config.get("maxOutputTokens") is not None:
        result["max_tokens"] = gen_config["maxOutputTokens"]
    if gen_config.get("stopSequences"):
        result["stop"] = gen_config["stopSequences"]
    if gen_config.get("frequencyPenalty") is not None:
        result["frequency_penalty"] = gen_config["frequencyPenalty"]
    if gen_config.get("presencePenalty") is not None:
        result["presence_penalty"] = gen_config["presencePenalty"]

    # Safety settings（Gemini 特有，透传供上游参考）
    if body.get("safetySettings"):
        result["safety_settings"] = body["safetySettings"]

    return result


def openai_to_gemini(openai_resp: dict) -> dict:
    """将 OpenAI Chat Completions 响应转换为 Gemini 格式"""
    choice = openai_resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content_text = msg.get("content", "") or ""
    finish = choice.get("finish_reason", "stop")

    # Gemini stop_reason 映射
    stop_map = {"stop": "STOP", "length": "MAX_TOKENS", "tool_calls": "OTHER"}
    gemini_stop = stop_map.get(finish, "STOP")

    usage = openai_resp.get("usage", {})

    result = {
        "candidates": [{
            "content": {
                "parts": [{"text": content_text}],
                "role": "model"
            },
            "finishReason": gemini_stop,
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": openai_resp.get("model", ""),
    }
    return result


# ================================================================
#  OpenAI SSE → Gemini SSE 流式转换
# ================================================================

def relay_stream_gemini(upstream_resp):
    """
    将上游 OpenAI SSE 流实时转换为 Gemini 流式格式。

    Gemini 流式格式：
    每个 chunk 是一个完整的 Gemini response 对象（含 candidates）
    """
    buffer = b""
    finished = False

    for chunk in upstream_resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data: "):
                continue
            payload = line[6:]
            if payload.strip() == b"[DONE]":
                finished = True
                break
            try:
                evt = json.loads(payload)
            except Exception:
                continue

            delta = evt.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            model = evt.get("model", "")

            gemini_chunk = {
                "candidates": [{
                    "content": {
                        "parts": [{"text": content}] if content else [],
                        "role": "model"
                    },
                    "finishReason": None,
                    "index": 0,
                }],
            }
            if model:
                gemini_chunk["modelVersion"] = model

            yield b"data: " + json.dumps(gemini_chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"

        if finished:
            break

    # 处理残留 buffer
    if buffer.strip() and buffer.startswith(b"data: "):
        payload = buffer[6:]
        if payload.strip() != b"[DONE]":
            try:
                evt = json.loads(payload)
                delta = evt.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    gemini_chunk = {
                        "candidates": [{
                            "content": {"parts": [{"text": content}], "role": "model"},
                            "finishReason": None, "index": 0,
                        }],
                    }
                    yield b"data: " + json.dumps(gemini_chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"
            except Exception:
                pass

    # 最后一个 chunk 带 finishReason
    yield b"data: " + json.dumps({
        "candidates": [{
            "content": {"parts": [], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }]
    }, ensure_ascii=False).encode("utf-8") + b"\n\n"
