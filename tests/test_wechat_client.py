"""WeChat OAuth client unit tests.

The API-level login flow is covered in ``test_auth.py``; this file targets the
client itself, especially the branches that only trigger with real HTTP answers
(success, errcode, non-JSON, transport failure). Those branches were previously
only reachable against the live WeChat servers, i.e. never in CI.
"""

from __future__ import annotations

import httpx
import pytest

from stockmaster.plugins.auth.services import wechat


def make_client(**overrides) -> wechat.WechatClient:
    params = {
        "mode": "open",
        "app_id": "wx-app-id",
        "app_secret": "wx-secret",
        "redirect_uri": "https://example.com/api/v1/auth/wechat/callback",
        "timeout": 1.0,
    }
    params.update(overrides)
    return wechat.WechatClient(**params)


class FakeResponse:
    def __init__(self, payload=None, *, status: int = 200, json_error: bool = False):
        self._payload = payload
        self.status_code = status
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def http(monkeypatch):
    """Patch ``httpx.Client`` so ``_get`` never opens a socket.

    ``state["queue"]`` 用来模拟多次调用返回不同结构（token → userinfo）；
    为空时所有调用都返回 ``state["response"]``。
    """

    calls: list[dict] = []
    state = {"response": FakeResponse({"ok": 1}), "error": None, "queue": []}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            calls.append({"url": url, "params": params, "timeout": self.timeout})
            if state["error"] is not None:
                raise state["error"]
            if state["queue"]:
                return state["queue"].pop(0)
            return state["response"]

    monkeypatch.setattr(wechat.httpx, "Client", FakeClient)
    return {"calls": calls, "state": state}


class TestAuthorizeUrl:
    def test_open_mode_uses_qr_scope_and_redirect(self):
        url = make_client().authorize_url("state-1")
        assert url.startswith(wechat.AUTHORIZE_OPEN)
        assert "scope=snsapi_login" in url
        assert "state=state-1" in url
        assert url.endswith("#wechat_redirect")

    def test_mp_mode_uses_official_account_endpoint(self):
        url = make_client(mode="mp").authorize_url("s2")
        assert url.startswith(wechat.AUTHORIZE_MP)
        assert "scope=snsapi_userinfo" in url

    def test_mock_mode_stays_local(self):
        url = make_client(mode="mock", mock_base="/api/v1/auth/wechat/mock/authorize").authorize_url("s3")
        assert url.startswith("/api/v1/auth/wechat/mock/authorize?")
        assert "state=s3" in url


class TestMockProfile:
    def test_is_deterministic_and_sanitised(self):
        client = make_client(mode="mock")
        first = client.exchange_code("mock:alice")
        second = client.exchange_code("mock:alice")
        assert first.openid == second.openid == "mock-alice"
        assert first.unionid == "mock-union-alice"

    def test_strips_unsafe_characters_and_falls_back(self):
        client = make_client(mode="mock")
        assert client.exchange_code("mock:../../etc").openid == "mock-etc"
        # 全是非法字符时回落到 demo，而不是空 openid
        assert client.exchange_code("mock:!!!").openid == "mock-demo"

    def test_bare_mock_code_uses_demo_slug(self):
        client = make_client(mode="mock")
        profile = client.exchange_code("mock")
        assert profile.openid == "mock-demo"
        assert profile.display_name == "模拟用户 demo"


class TestExchangeCode:
    def test_success_returns_profile(self, http):
        http["state"]["response"] = FakeResponse(
            {"access_token": "token-1", "openid": "openid-1", "unionid": "union-1"}
        )
        profile = make_client().exchange_code("code-1")

        assert profile.openid == "openid-1"
        assert profile.unionid == "union-1"
        first = http["calls"][0]["params"]
        assert first["appid"] == "wx-app-id"
        assert first["secret"] == "wx-secret"
        assert first["code"] == "code-1"
        assert first["grant_type"] == "authorization_code"

    def test_userinfo_contains_profile_fields(self, http):
        http["state"]["queue"] = [
            FakeResponse({"access_token": "t", "openid": "o", "unionid": "u"}),
            FakeResponse({"openid": "o", "nickname": "小明", "headimgurl": "http://a/b.png"}),
        ]

        profile = make_client().exchange_code("code-2")

        assert profile.nickname == "小明"
        assert profile.avatar_url == "http://a/b.png"
        assert profile.unionid == "u"

    def test_missing_access_token_raises(self, http):
        http["state"]["response"] = FakeResponse({"openid": "o"})
        with pytest.raises(wechat.WechatError, match="access_token"):
            make_client().exchange_code("code-3")

    def test_errcode_from_token_endpoint_raises_with_hint(self, http):
        http["state"]["response"] = FakeResponse({"errcode": 40029, "errmsg": "invalid code"})
        with pytest.raises(wechat.WechatError) as info:
            make_client().exchange_code("code-4")
        assert info.value.errcode == 40029
        assert "invalid code" in str(info.value)

    def test_userinfo_error_degrades_to_openid(self, http):
        http["state"]["queue"] = [
            FakeResponse({"access_token": "t", "openid": "o", "unionid": "u"}),
            FakeResponse({"errcode": 48001, "errmsg": "api unauthorized"}),
        ]

        profile = make_client().exchange_code("code-5")

        assert profile.openid == "o"
        assert profile.nickname == ""

    def test_empty_code_raises(self, http):
        with pytest.raises(wechat.WechatError, match="授权码"):
            make_client().exchange_code("")

    def test_http_error_is_wrapped(self, http):
        http["state"]["error"] = httpx.ConnectTimeout("timeout")
        with pytest.raises(wechat.WechatError, match="无法连接微信服务器"):
            make_client().exchange_code("code-6")

    def test_http_status_error_is_wrapped(self, http):
        http["state"]["response"] = FakeResponse({}, status=500)
        with pytest.raises(wechat.WechatError, match="无法连接微信服务器"):
            make_client().exchange_code("code-7")

    def test_non_json_body_is_wrapped(self, http):
        http["state"]["response"] = FakeResponse(None, json_error=True)
        with pytest.raises(wechat.WechatError, match="合法 JSON"):
            make_client().exchange_code("code-8")

    def test_non_dict_payload_is_wrapped(self, http):
        http["state"]["response"] = FakeResponse(["unexpected"])
        with pytest.raises(wechat.WechatError, match="结构异常"):
            make_client().exchange_code("code-9")
