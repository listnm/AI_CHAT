"""
OAuth 提供商配置和工具函数

基于 sub2api 的上游 AI 平台 OAuth 实现：
- OpenAI：ChatGPT Plus/Pro 订阅账号（公开 PKCE 客户端）
- Grok (xAI)：xAI 订阅账号（公开 PKCE 客户端）
- Gemini：Google Gemini 订阅账号（code_assist 模式）

关键设计：
- 使用内置的固定 Client ID，用户无需创建 OAuth 应用
- 所有提供商都使用 PKCE（Proof Key for Code Exchange）
- Client Secret 不需要（公开客户端）
"""

import os
import secrets
import hashlib
import base64

# ================================================================
#  OAuth 提供商配置（基于 sub2api 实现）
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
        # sub2api 使用的官方 Codex CLI 客户端 ID（公开，无需 secret）
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "client_secret": "",  # 公开客户端，不需要 secret
        "scope": "openid profile email offline_access",
        "callback_path": "/oa/callback/openai",
        "auth_extra": {
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
        # sub2api 使用的 xAI 客户端 ID（公开，无需 secret）
        "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
        "client_secret": "",
        "scope": "openid profile email offline_access grok-cli:access api:access",
        "callback_path": "/oa/callback/grok",
        "auth_extra": {},
    },
    "gemini": {
        "name": "Gemini",
        "desc": "Google Gemini 订阅账号（Code Assist）",
        "icon": "💎",
        "color": "#1a73e8",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        # sub2api 使用的 Gemini CLI 客户端（code_assist 模式）
        "client_id": "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
        "client_secret_env": "GEMINI_CLI_CLIENT_SECRET",  # 需要在环境变量中设置
        "scope": "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile",
        "callback_path": "/oa/callback/gemini",
        # Gemini code_assist 的重定向地址是 Google 自己的域名
        "redirect_override": "https://codeassist.google.com/authcode",
        "auth_extra": {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    },
}


# ================================================================
#  PKCE 工具函数
# ================================================================

def generate_pkce_pair() -> tuple:
    """
    生成 PKCE code_verifier 和 code_challenge。

    OpenAI 使用 hex 编码（64 字节），其他使用 base64url（32 字节）。
    返回 (verifier, challenge, method)
    """
    # 生成 32 字节随机数据
    random_bytes = os.urandom(32)

    # code_verifier: base64url 编码（无填充）
    verifier = base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")

    # code_challenge: SHA-256(verifier), base64url 编码（无填充）
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    return verifier, challenge, "S256"


def generate_pkce_pair_openai() -> tuple:
    """
    OpenAI 专用 PKCE：verifier 使用 hex 编码（64 字节随机数据）。
    """
    random_bytes = os.urandom(64)
    verifier = random_bytes.hex()

    digest = hashlib.sha256(random_bytes).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    return verifier, challenge, "S256"


# ================================================================
#  OAuth 工具函数
# ================================================================

def generate_state():
    """生成 OAuth state 参数（CSRF 防护）"""
    return secrets.token_urlsafe(32)


def build_auth_url(provider_key: str, redirect_uri: str, state: str) -> str:
    """构建 OAuth 授权 URL（带 PKCE）"""
    provider = PROVIDERS.get(provider_key)
    if not provider:
        return ""

    # 生成 PKCE
    if provider_key == "openai":
        verifier, challenge, method = generate_pkce_pair_openai()
    else:
        verifier, challenge, method = generate_pkce_pair()

    # 使用重定向覆盖（Gemini code_assist 用 Google 自己的域名）
    actual_redirect = provider.get("redirect_override", redirect_uri)

    params = {
        "response_type": "code",
        "client_id": provider["client_id"],
        "redirect_uri": actual_redirect,
        "scope": provider["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": method,
    }

    # 合并提供商特殊参数
    params.update(provider.get("auth_extra", {}))

    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{provider['auth_url']}?{query}"


def exchange_code(provider_key: str, code: str, redirect_uri: str,
                  code_verifier: str = "") -> dict:
    """用授权码换取 access_token 和 refresh_token"""
    import requests

    provider = PROVIDERS.get(provider_key)
    if not provider:
        return {"error": "未知的提供商"}

    # 使用重定向覆盖
    actual_redirect = provider.get("redirect_override", redirect_uri)

    data = {
        "grant_type": "authorization_code",
        "client_id": provider["client_id"],
        "code": code,
        "redirect_uri": actual_redirect,
    }

    # Gemini code_assist 需要 client_secret（从环境变量读取）
    client_secret = os.environ.get(provider.get("client_secret_env", ""), "")
    if client_secret:
        data["client_secret"] = client_secret

    # PKCE: 添加 code_verifier
    if code_verifier:
        data["code_verifier"] = code_verifier

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        resp = requests.post(provider["token_url"], data=data, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        error_body = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                error_body = e.response.json().get("error_description", "")
            except Exception:
                error_body = e.response.text[:200]
        return {"error": f"换取令牌失败: {str(e)[:100]} {error_body}"}


def refresh_token(provider_key: str, refresh_tok: str) -> dict:
    """刷新 access_token"""
    import requests

    provider = PROVIDERS.get(provider_key)
    if not provider:
        return {"error": "未知的提供商"}

    data = {
        "grant_type": "refresh_token",
        "client_id": provider["client_id"],
        "refresh_token": refresh_tok,
    }

    # Gemini 需要 client_secret（从环境变量读取）
    client_secret = os.environ.get(provider.get("client_secret_env", ""), "")
    if client_secret:
        data["client_secret"] = client_secret
        data["scope"] = provider["scope"]
    else:
        # OpenAI/Grok 刷新时的 scope
        data["scope"] = "openid profile email"

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        resp = requests.post(provider["token_url"], data=data, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        # 如果没返回新的 refresh_token，保留旧的
        if not result.get("refresh_token"):
            result["refresh_token"] = refresh_tok
        return result
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
    return {"email": "", "display_name": ""}
