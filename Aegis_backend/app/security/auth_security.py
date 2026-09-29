"""基于 OIDC Discovery 与 JWKS 的 access token 验证。"""

from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
import jwt
from jwt import InvalidTokenError, PyJWKSet

from app.config.settings import Settings


class AuthenticationError(Exception):
    """令牌缺失、格式错误、签名无效或声明校验失败时抛出。"""


@dataclass(frozen=True, slots=True)
class OIDCIdentity:
    """从已验证 access token 中提取的标准身份声明。"""

    issuer: str
    subject: str
    email: str
    display_name: str
    claims: dict[str, Any] # claims是JWT解码后的原始对象，我们可能只用其中的一部分，所以重新包装OIDCIdentity对象


class OIDCAuthenticator:
    """发现 OIDC 公钥并验证 Keycloak 签发的 RS256 access token。"""

    _cache_ttl_seconds = 300.0  # JWKS 公钥缓存 300 秒

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._jwks: PyJWKSet | None = None  # Keycloak 公钥集合
        self._jwks_expires_at = 0.0  # 过期时间

    @property
    def discovery_url(self) -> str:
        """返回当前 issuer 的 OpenID Discovery 地址(Keycloak 公钥地址)。"""
        return f"{self._settings.oidc_issuer}/.well-known/openid-configuration"

    async def authenticate(self, token: str) -> OIDCIdentity:
        """验证 token，并返回可用于本地用户映射的稳定身份信息。"""
        if not token:
            raise AuthenticationError("缺少 access token")

        try:
            header = jwt.get_unverified_header(token) # 读取 JWT Header 的 kid
            # kid: Key ID（密钥标识）标识“这个 JWT 是由哪一把 Keycloak 签名密钥签发的，需要用哪一个公钥解码”
            key = await self._get_signing_key(header.get("kid"))  # 获取对应kid的公钥key

            claims = jwt.decode(
                # 使用 Keycloak 公钥验证 RS256 签名;验证 Token 未过期;验证 issuer;
                # 验证 audience 包含 aegis-pa-api;必须存在 exp、iat、iss、aud、sub。
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (InvalidTokenError, KeyError, TypeError, ValueError) as error:
            raise AuthenticationError("access token 无效或已过期") from error
        # token有azp则必须是：aegis-pa-web 防止本应签发给其他前端 Client 的 Token 被 Aegis 接受
        authorized_party = claims.get("azp")
        if authorized_party is not None and authorized_party != self._settings.oidc_client_id:
            raise AuthenticationError("access token 并非由允许的前端客户端获取")

        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject or not isinstance(email, str) or not email:
            raise AuthenticationError("access token 缺少用户 subject 或 email 声明")

        display_name = claims.get("name") or claims.get("preferred_username") or email
        if not isinstance(display_name, str):
            display_name = email

        # 验证成功后，将可信 Claims 统一封装为 OIDCIdentity
        # claims是JWT解码后的原始对象，我们可能只用其中的一部分，所以重新包装OIDCIdentity对象
        return OIDCIdentity(
            issuer=self._settings.oidc_issuer,
            subject=subject,
            email=email,
            display_name=display_name[:128],
            claims=claims, # JWT解码后的原始对象
        )

    async def _get_signing_key(self, key_id: str | None) -> Any:
        """按 JWT 的 kid 取 Keycloak JWKS 公钥，并短暂缓存公钥集合。"""
        if not key_id:
            raise AuthenticationError("access token 缺少 kid")

        if self._jwks is None or monotonic() >= self._jwks_expires_at:
            await self._refresh_jwks()

        assert self._jwks is not None
        for jwk in self._jwks.keys:
            if jwk.key_id == key_id:
                return jwk.key

        # Keycloak 密钥轮换后，旧缓存可能没有新的 kid；刷新一次再查找。
        await self._refresh_jwks()
        assert self._jwks is not None
        for jwk in self._jwks.keys:
            if jwk.key_id == key_id:
                return jwk.key
        raise AuthenticationError("未找到 access token 对应的签名公钥")

    async def _refresh_jwks(self) -> None:
        """通过 Discovery 读取并缓存 JWKS；严格核对 Discovery 的 issuer。"""
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            discovery_response = await client.get(self.discovery_url)
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            if discovery.get("issuer") != self._settings.oidc_issuer:
                raise AuthenticationError("OIDC Discovery 返回的 issuer 与配置不一致")
            jwks_uri = discovery.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith(("https://", "http://")):
                raise AuthenticationError("OIDC Discovery 未提供有效 JWKS 地址")
            jwks_response = await client.get(jwks_uri)
            jwks_response.raise_for_status()
            self._jwks = PyJWKSet.from_dict(jwks_response.json())
            self._jwks_expires_at = monotonic() + self._cache_ttl_seconds
        except httpx.HTTPError as error:
            raise AuthenticationError("无法获取 OIDC 公钥") from error
        finally:
            if owns_client:
                await client.aclose()

