"""
OAuth 订阅账号模块（基于 sub2api 实现）

核心功能：
1. PKCE 安全认证
2. 固定 Client ID（公开客户端，无需 Secret）
3. 令牌自动刷新
4. 安全存储（Fernet 加密）

支持的提供商：
- OpenAI：ChatGPT Plus/Pro 订阅
- Grok (xAI)：xAI 订阅
- Gemini：Google Gemini 订阅（Code Assist）
"""

import os
import secrets
import hashlib
import base64
import json
import time
from datetime import datetime, timezone, timedelta


# ================================================================
#  提供商配置（基于 sub2api 源码）
# ================================================================

PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "desc": "ChatGPT Plus / Pro 订阅账号",
        "icon": "🤖",
        "color": "#10a37f",
        "auth_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "userinfo_url": "https://auth.openai.com/userinfo",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "scope": "openid profile email offline_access",
        "callback_path": "/oa/callback/openai",
        "extra_params": {
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        },
    },
    "grok": {
        "name": "Grok (xAI)",
        "desc": "xAI Grok 订阅账号",
        "icon": "⚡",
        "color": "#1d1d1f",
        "auth_url": "https://auth.x.ai/oauth2/authorize",
        "token_url": "https://auth.x.ai/oauth2/token",
        "userinfo_url": "https://api.x.ai/v1/user/me",
        "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
        "scope": "openid profile email offline_access grok-cli:access api:access",
        "callback_path": "/oa/callback/grok",
        "extra_params": {},
    },
    "gemini": {
        "name": "Gemini",
        "desc": "Google Gemini 订阅账号",
        "icon": "💎",
        "color": "#4285f4",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "client_id": "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
        "scope": "openid profile email",
        "callback_path": "/oa/callback/gemini",
        "extra_params": {
            "access_type": "offline",
            "prompt": "consent",
        },
    },
}


# ================================================================
#  PKCE 工具（sub2api 实现）
# ================================================================

def _generate_pkce_openai():
    """OpenAI PKCE: verifier = hex(64 bytes), challenge = base64url(sha256(verifier))"""
    verifier = secrets.token_hex(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _generate_pkce_standard():
    """标准 PKCE: verifier = base64url(32 bytes), challenge = base64url(sha256(verifier))"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state():
    return secrets.token_urlsafe(32)


# ================================================================
#  OAuth 流程
# ================================================================

def build_authorize_url(provider_key, redirect_uri, state, code_challenge):
    """构建授权 URL"""
    p = PROVIDERS[provider_key]
    params = {
        "response_type": "code",
        "client_id": p["client_id"],
        "redirect_uri": redirect_uri,
        "scope": p["scope"],
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    params.update(p.get("extra_params", {}))
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{p['auth_url']}?{query}"


def exchange_code_for_tokens(provider_key, code, redirect_uri, code_verifier):
    """用授权码 + PKCE 换取令牌"""
    import requests as req
    p = PROVIDERS[provider_key]
    data = {
        "grant_type": "authorization_code",
        "client_id": p["client_id"],
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        r = req.post(p["token_url"], data=data,
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        desc = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                desc = e.response.json().get("error_description", "")
            except Exception:
                pass
        return {"error": f"{str(e)[:80]} {desc}".strip()}


def refresh_access_token(provider_key, refresh_token_val):
    """刷新 access_token"""
    import requests as req
    p = PROVIDERS[provider_key]
    data = {
        "grant_type": "refresh_token",
        "client_id": p["client_id"],
        "refresh_token": refresh_token_val,
    }
    try:
        r = req.post(p["token_url"], data=data,
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     timeout=30)
        r.raise_for_status()
        result = r.json()
        if not result.get("refresh_token"):
            result["refresh_token"] = refresh_token_val
        return result
    except Exception as e:
        return {"error": str(e)[:100]}


def fetch_user_info(provider_key, access_token):
    """获取用户信息"""
    import requests as req
    p = PROVIDERS[provider_key]
    try:
        r = req.get(p["userinfo_url"],
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def extract_user_info(provider_key, token_data, user_info):
    """从令牌和用户信息中提取标准化字段"""
    email = user_info.get("email", "")
    name = user_info.get("name", "") or user_info.get("nickname", "")

    # OpenAI: 从 id_token 解析额外信息
    extra = {}
    if provider_key == "openai" and token_data.get("id_token"):
        try:
            payload = token_data["id_token"].split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email = claims.get("email", email)
            name = claims.get("name", name)
            extra["chatgpt_account_id"] = claims.get("chatgpt_account_id", "")
            extra["plan_type"] = claims.get("chatgpt_plan_type", "")
        except Exception:
            pass

    return {
        "email": email,
        "display_name": name,
        "extra": extra,
        "expires_in": token_data.get("expires_in", 0),
        "scope": token_data.get("scope", ""),
    }
