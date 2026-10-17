"""WeChat OAuth client.

Three modes share one interface:

``open``  PC 网站应用扫码登录（open.weixin.qq.com, scope=snsapi_login）
``mp``    公众号网页授权（scope=snsapi_userinfo，需在微信内打开）
``mock``  本地模拟授权，不访问微信，用于没有 AppID 时演练整个回调链路

The HTTP calls are deliberately thin: WeChat answers ``200 OK`` even for failures
and reports them as ``errcode``, so every response is checked for that field
instead of trusting the status code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from stockmaster.config import settings
from stockmaster.core.logging import get_logger

log = get_logger(__name__)

AUTHORIZE_OPEN = "https://open.weixin.qq.com/connect/qrconnect"
AUTHORIZE_MP = "https://open.weixin.qq.com/connect/oauth2/authorize"
TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"

# WeChat error codes worth explaining to the operator; everything else is passed through.
ERROR_HINTS = {
    40029: "授权码无效或已使用，请重新发起登录",
    40013: "AppID 无效",
    40163: "授权码已被使用",
    41008: "缺少 code 参数",
    42003: "授权码已过期",
    48001: "该账号未获得网页授权权限（公众号需认证服务号，网站应用需通过审核）",
    40125: "AppSecret 无效",
}


class WechatError(Exception):
    """WeChat OAuth failure with a human-readable message."""

    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


@dataclass
class WechatProfile:
    """The subset of WeChat user info StockMaster keeps."""

    openid: str
    unionid: str = ""
    nickname: str = ""
    avatar_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.nickname or "微信用户"


class WechatClient:
    """Client bound to the *effective* mode (credentials already validated)."""

    def __init__(
        self,
        *,
        mode: str,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        timeout: float,
        mock_base: str = "/api/v1/auth/wechat/mock/authorize",
    ) -> None:
        self.mode = mode
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.timeout = timeout
        self.mock_base = mock_base

    # ------------------------------------------------------------------ helpers
    @classmethod
    def from_settings(cls, redirect_uri: str) -> WechatClient:
        return cls(
            mode=settings.wechat_mode,
            app_id=settings.wechat_app_id,
            app_secret=settings.wechat_app_secret,
            redirect_uri=redirect_uri,
            timeout=settings.wechat_timeout,
        )

    def authorize_url(self, state: str) -> str:
        """Where the browser must be sent to obtain an authorisation code."""
        params = {
            "appid": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self.mode == "mock":
            return f"{self.mock_base}?{urlencode({'state': state, 'redirect_uri': self.redirect_uri})}"
        if self.mode == "mp":
            params["scope"] = "snsapi_userinfo"
            return f"{AUTHORIZE_MP}?{urlencode(params)}#wechat_redirect"
        params["scope"] = "snsapi_login"
        return f"{AUTHORIZE_OPEN}?{urlencode(params)}#wechat_redirect"

    # -------------------------------------------------------------------- flow
    def exchange_code(self, code: str) -> WechatProfile:
        """Trade an authorisation code (or a mock code) for a profile."""
        if self.mode == "mock" or code.startswith("mock:"):
            return self._mock_profile(code)
        if not code:
            raise WechatError("缺少授权码 code")

        token_payload = self._get(
            TOKEN_URL,
            {
                "appid": self.app_id,
                "secret": self.app_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        access_token = str(token_payload.get("access_token") or "")
        openid = str(token_payload.get("openid") or "")
        if not access_token or not openid:
            raise WechatError("微信未返回 access_token/openid")
        unionid = str(token_payload.get("unionid") or "")

        profile_payload = self._get(
            USERINFO_URL,
            {"access_token": access_token, "openid": openid, "lang": "zh_CN"},
            allow_error=True,
        )
        # userinfo can fail while the token is still valid (e.g. scope=snsapi_base);
        # a bare openid is enough to identify the account, so degrade gracefully.
        if profile_payload.get("errcode"):
            log.info("wechat userinfo unavailable (%s); falling back to openid", profile_payload.get("errmsg"))
            return WechatProfile(openid=openid, unionid=unionid, raw={"token": token_payload})

        return WechatProfile(
            openid=str(profile_payload.get("openid") or openid),
            unionid=str(profile_payload.get("unionid") or unionid),
            nickname=str(profile_payload.get("nickname") or ""),
            avatar_url=str(profile_payload.get("headimgurl") or ""),
            raw=profile_payload,
        )

    # ------------------------------------------------------------------ internal
    def _mock_profile(self, code: str) -> WechatProfile:
        """Deterministic fake identity so repeated mock logins hit one account."""
        slug = code.split(":", 1)[1].strip() if ":" in code else "demo"
        slug = "".join(ch for ch in slug if ch.isalnum() or ch in "-_") or "demo"
        return WechatProfile(
            openid=f"mock-{slug}",
            unionid=f"mock-union-{slug}",
            nickname=f"模拟用户 {slug}",
            avatar_url="",
            raw={"mock": True, "slug": slug},
        )

    def _get(self, url: str, params: dict[str, str], *, allow_error: bool = False) -> dict:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise WechatError(f"无法连接微信服务器：{exc}") from exc
        except ValueError as exc:
            raise WechatError("微信返回的不是合法 JSON") from exc

        if not isinstance(payload, dict):
            raise WechatError("微信返回结构异常")

        errcode = payload.get("errcode")
        if errcode in (None, 0):
            return payload
        message = str(payload.get("errmsg") or "微信接口返回错误")
        hint = ERROR_HINTS.get(int(errcode), "")
        full = f"{message}（errcode={errcode}{'，' + hint if hint else ''}）"
        if allow_error:
            return dict(payload, errcode=errcode, errmsg=full)
        raise WechatError(full, errcode=int(errcode))


def build_client(redirect_uri: str) -> WechatClient:
    """Factory used by the API layer (single seam for tests to patch)."""
    return WechatClient.from_settings(redirect_uri)
