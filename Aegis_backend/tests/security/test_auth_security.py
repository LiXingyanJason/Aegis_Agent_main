"""OIDC JWT 验证单元测试。"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config.settings import Settings
from app.security.auth_security import AuthenticationError, OIDCAuthenticator

ISSUER = "http://127.0.0.1:8080/realms/aegis"


def make_settings() -> Settings:
    """构造 JWT 验证测试使用的完整 OIDC 配置。"""
    return Settings(
        _env_file=None,
        database_url="postgresql://aegis:secret@localhost:5432/aegis_pa",
        redis_url="redis://localhost:6379/0",
        oidc_issuer_url=ISSUER,
        oidc_audience="aegis-pa-api",
        oidc_client_id="aegis-pa-web",
        model_provider="openai",
        model_api_base="https://api.openai.com/v1",
        model_api_key="test-key",
        model_default_name="test-model",
    )


def make_key_and_token(*, audience: object = "aegis-pa-api") -> tuple[dict, str]:
    """生成临时 RSA 密钥、对应 JWKS 和已签名测试 access token。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "test-key"
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "keycloak-user-id",
            "aud": audience,
            "azp": "aegis-pa-web",
            "email": "jason@example.com",
            "name": "Jason Li",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    return {"keys": [public_jwk]}, token


def make_http_client(jwks: dict) -> httpx.AsyncClient:
    """模拟 Keycloak Discovery 与 JWKS HTTP 响应，不访问真实认证服务。"""
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    jwks_url = f"{ISSUER}/protocol/openid-connect/certs"

    def handle_request(request: httpx.Request) -> httpx.Response:
        if str(request.url) == discovery_url:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": jwks_url})
        if str(request.url) == jwks_url:
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle_request))


@pytest.mark.asyncio
async def test_authenticator_validates_signature_issuer_audience_and_identity_claims() -> None:
    """测试验证器能通过 JWKS 校验合法 token，并提取 issuer、subject、邮箱和显示名。"""
    jwks, token = make_key_and_token(audience=["account", "aegis-pa-api"])
    async with make_http_client(jwks) as client:
        identity = await OIDCAuthenticator(make_settings(), client).authenticate(token)

    assert identity.issuer == ISSUER
    assert identity.subject == "keycloak-user-id"
    assert identity.email == "jason@example.com"
    assert identity.display_name == "Jason Li"


@pytest.mark.asyncio
async def test_authenticator_rejects_token_for_another_api_audience() -> None:
    """测试签名正确但未包含 Aegis API audience 的 token 会被拒绝。"""
    jwks, token = make_key_and_token(audience="another-api")
    async with make_http_client(jwks) as client:
        authenticator = OIDCAuthenticator(make_settings(), client)
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate(token)
