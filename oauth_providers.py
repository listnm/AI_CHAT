"""
OAuth 提供商配置和工具函数

基于 sub2api 的上游 AI 平台 OAuth 支持：
- OpenAI：ChatGPT Plus/Pro 订阅账号
- Grok (xAI)：xAI 订阅账号
- Gemini：Google Gemini 订阅账号
- Antigravity：国产 AI 编程助手
"""

import os
import secrets

# ================================================================
#  OAuth 提供商配置（上游 AI 平台订阅账号）
# ================================================================

PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "desc": "ChatGPT Plus / Pro 订阅账号",
        "icon": "🤖",
        "color": "#10a37f",
        "auth_url": "https://auth0.openai.com/authorize",
        "token_url": "https://auth0.openai.com/oauth/token",
        "userinfo_url": "https://auth0.openai.com/userinfo",
        "scope": "openid profile email",
        "client_id_env": "OPENAI_CLIENT_ID",
        "client_secret_env": "OPENAI_CLIENT_SECRET",
        "callback_path": "/oa/callback/openai",
        "auth_extra": {"audience": "https://api.openai.com/v1"},
    },
    "grok": {
        "name": "Grok (xAI)",
        "desc": "xAI Grok 订阅账号",
        "icon": "⚡",
        "color": "#1d1d1f",
        "auth_url": "https://accounts.x.ai/oauth2/auth",
        "token_url": "https://accounts.x.ai/oauth2/token",
        "userinfo_url": "https://api.x.ai/v1/user/me",
        "scope": "openid profile email",
        "client_id_env": "GROK_CLIENT_ID",
        "client_secret_env": "GROK_CLIENT_SECRET",
        "callback_path": "/oa/callback/grok",
        "auth_extra": {},
    },
    "gemini": {
        "name": "Gemini",
        "desc": "Google Gemini 订阅账号",
        "icon": "💎",
        "color": "#1a73e8",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "scope": "openid profile email",
        "client_id_env": "GEMINI_CLIENT_ID",
        "client_secret_env": "GEMINI_CLIENT_SECRET",
        "callback_path": "/oa/callback/gemini",
        "auth_extra": {"access_type": "offline", "prompt": "consent"},
    },
    "antigravity": {
        "name": "Antigravity",
        "desc": "国产 AI 编程助手",
        "icon": "🚀",
        "color": "#ff6b35",
        "auth_url": "https://antigravity.example.com/oauth2/authorize",
        "token_url": "https://antigravity.example.com/oauth2/token",
        "userinfo_url": "https://antigravity.example.com/api/userinfo",
        "scope": "openid profile email",
        "client_id_env": "ANTIGRAVITY_CLIENT_ID",
        "client_secret_env": "ANTIGRAVITY_CLIENT_SECRET",
        "callback_path": "/oa/callback/antigravity",
        "auth_extra": {},
    },
}


# ================================================================
#  OAuth 工具函数
# ================================================================

def generate_state():
    """生成 OAuth state 参数（CSRF 防护）"""
    return secrets.token_urlsafe(32)


def build_auth_url(provider_key: str, client_id: str, redirect_uri: str, state: str) -> str:
    """构建 OAuth 授权 URL"""
    provider = PROVIDERS.get(provider_key)
    if not provider:
        return ""

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider["scope"],
        "state": state,
    }

    # 合并提供商特殊参数
    params.update(provider.get("auth_extra", {}))

    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{provider['auth_url']}?{query}"


def exchange_code(provider_key: str, client_id: str, client_secret: str,
                  code: str, redirect_uri: str) -> dict:
    """用授权码换取 access_token 和 refresh_token"""
    import requests

    provider = PROVIDERS.get(provider_key)
    if not provider:
        return {"error": "未知的提供商"}

    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    # 合并提供商特殊参数
    data.update(provider.get("auth_extra", {}))

    try:
        resp = requests.post(provider["token_url"], data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"换取令牌失败: {str(e)[:100]}"}


def refresh_token(provider_key: str, client_id: str, client_secret: str,
                  refresh_tok: str) -> dict:
    """刷新 access_token"""
    import requests

    provider = PROVIDERS.get(provider_key)
    if not provider:
        return {"error": "未知的提供商"}

    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_tok,
    }

    try:
        resp = requests.post(provider["token_url"], data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"刷新令牌失败: {str(e)[:100]}"}


def get_user_info(provider_key: str, access_token: str) -> dict:
    """获取用户信息"""
    import requests

    provider = PROVIDERS.get(provider_key)
    if not provider:
        return {"error": "未知的提供商"}

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.get(provider["userinfo_url"], headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"获取用户信息失败: {str(e)[:100]}"}


def parse_user_info(provider_key: str, raw_info: dict) -> dict:
    """从原始用户信息中提取标准化字段"""
    if "error" in raw_info:
        return {"email": "unknown", "display_name": "Unknown"}

    if provider_key in ("openai", "grok"):
        return {
            "email": raw_info.get("email", ""),
            "display_name": raw_info.get("name", "") or raw_info.get("nickname", ""),
        }
    elif provider_key == "gemini":
        return {
            "email": raw_info.get("email", ""),
            "display_name": raw_info.get("name", ""),
        }
    elif provider_key == "antigravity":
        return {
            "email": raw_info.get("email", ""),
            "display_name": raw_info.get("name", "") or raw_info.get("username", ""),
        }
    return {"email": "", "display_name": ""}


def get_provider_config_for_flow(provider_key: str, base_url: str) -> tuple:
    """获取 OAuth 流程所需的配置，返回 (client_id, client_secret, redirect_uri)"""
    provider = PROVIDERS.get(provider_key)
    if not provider:
        return None, None, None

    client_id = os.environ.get(provider["client_id_env"], "")
    client_secret = os.environ.get(provider["client_secret_env"], "")
    redirect_uri = base_url.rstrip("/") + provider["callback_path"]

    return client_id, client_secret, redirect_uri
