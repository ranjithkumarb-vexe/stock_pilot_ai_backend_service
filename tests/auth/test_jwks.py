"""RS256 / ES256 verification through a cached JWKS (network mocked with respx)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from app.core.auth import TokenVerifier
from app.core.config import Settings
from app.core.errors import AppError

JWKS_URL = "https://proj.supabase.co/auth/v1/.well-known/jwks.json"


def _settings(**over: Any) -> Settings:
    return Settings(_env_file=None, auth_mode="supabase", supabase_jwks_url=JWKS_URL, **over)


def _claims() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "sub": str(uuid4()),
        "email": "k@example.test",
        "aud": "authenticated",
        "exp": int((now + timedelta(minutes=30)).timestamp()),
    }


def _rsa_pair(kid: str) -> tuple[Any, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, alg="RS256", use="sig")
    return key, jwk


def _ec_pair(kid: str) -> tuple[Any, dict[str, Any]]:
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, alg="ES256", use="sig")
    return key, jwk


@pytest.mark.parametrize(("alg", "maker"), [("RS256", _rsa_pair), ("ES256", _ec_pair)])
@respx.mock
async def test_jwks_verification_and_caching(alg: str, maker: Any) -> None:
    key, jwk = maker("kid-1")
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [jwk]}))
    verifier = TokenVerifier(_settings())
    claims = _claims()
    token = jwt.encode(claims, key, algorithm=alg, headers={"kid": "kid-1"})
    for _ in range(3):
        user = await verifier.verify(token)
        assert str(user.user_id) == claims["sub"] and user.email == "k@example.test"
    assert route.call_count == 1  # cached
    await verifier.aclose()


@respx.mock
async def test_unknown_kid_is_rejected_and_refetch_is_rate_limited() -> None:
    _, jwk = _rsa_pair("known")
    other, _ = _rsa_pair("unknown")
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [jwk]}))
    verifier = TokenVerifier(_settings())
    forged = jwt.encode(_claims(), other, algorithm="RS256", headers={"kid": "unknown"})
    for _ in range(5):
        with pytest.raises(AppError) as exc:
            await verifier.verify(forged)
        assert exc.value.status_code == 401
    assert route.call_count == 1  # attacker-chosen kids cannot make us hammer the JWKS endpoint
    await verifier.aclose()


@respx.mock
async def test_signature_must_match_the_kid_key() -> None:
    _, jwk = _rsa_pair("k1")
    attacker, _ = _rsa_pair("k1")
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [jwk]}))
    verifier = TokenVerifier(_settings())
    forged = jwt.encode(_claims(), attacker, algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(AppError) as exc:
        await verifier.verify(forged)
    assert exc.value.status_code == 401
    await verifier.aclose()


@respx.mock
async def test_algorithm_confusion_is_blocked() -> None:
    """HS256 token signed with the *public* key must not verify when only JWKS is configured."""
    _, jwk = _rsa_pair("k1")
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [jwk]}))
    verifier = TokenVerifier(_settings())
    token = jwt.encode(
        _claims(), "public-key-bytes-as-secret-0123456789", algorithm="HS256", headers={"kid": "k1"}
    )
    with pytest.raises(AppError):
        await verifier.verify(token)
    none_token = jwt.encode(_claims(), None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(AppError):
        await verifier.verify(none_token)
    await verifier.aclose()


@respx.mock
async def test_jwks_outage_fails_closed() -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(500))
    verifier = TokenVerifier(_settings())
    key, _ = _rsa_pair("k1")
    with pytest.raises(AppError) as exc:
        await verifier.verify(jwt.encode(_claims(), key, algorithm="RS256", headers={"kid": "k1"}))
    assert exc.value.status_code == 401
    await verifier.aclose()
