"""Tests for the Lyse Charge API client, against a scripted fake HTTP session."""

from __future__ import annotations

from dataclasses import dataclass, field
from http.cookies import SimpleCookie
import json
import time
from typing import Any, Self
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.lyse_charge.api import LyseAPI, LyseAPIError, LyseAuthError
from custom_components.lyse_charge.const import AUTH_AUDIENCE, AUTH_BASE, AUTH_REDIRECT_URI

GRAPHQL_URL = "https://cch-api.lyse.cloud/graphql"
WS_URL = "wss://cch-api.lyse.cloud/graphql"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
AUTHORIZE_URL = f"{AUTH_BASE}/authorize"
IDENTIFIER_URL = f"{AUTH_BASE}/u/login/identifier?state=st"
PASSWORD_URL = f"{AUTH_BASE}/u/login/password?state=st"
RESUME_URL = f"{AUTH_BASE}/authorize/resume?state=st"


# --- A minimal aiohttp.ClientSession stand-in -------------------------------
#
# Covers only what LyseAPI uses: post/request as async context managers, and
# responses with status, ok, headers, cookies, text() and json(). Requests are
# matched on method and exact URL (query parameters passed as params= are
# recorded, not matched), so a request to any other host fails the test.


class FakeResponse:
    """A canned HTTP response."""

    def __init__(
        self,
        status: int = 200,
        *,
        payload: Any = None,
        body: str = "",
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        """Build a response with a JSON payload or a text body."""
        self.status = status
        self.ok = status < 400
        self._body = json.dumps(payload) if payload is not None else body
        self.headers = headers or {}
        self.cookies = SimpleCookie(cookies or {})

    async def text(self) -> str:
        """Return the body as text."""
        return self._body

    async def json(self) -> Any:
        """Return the body parsed as JSON."""
        return json.loads(self._body)

    async def __aenter__(self) -> Self:
        """Enter the response context."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave the response context."""


@dataclass
class Call:
    """One request the client made."""

    method: str
    url: str
    kwargs: dict[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        """Return request headers with lower-cased names."""
        return {k.lower(): v for k, v in (self.kwargs.get("headers") or {}).items()}


@dataclass
class FakeSession:
    """Serve queued responses per (method, URL) and record every request."""

    routes: dict[tuple[str, str], list[FakeResponse | Exception]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def add(self, method: str, url: str, *responses: FakeResponse | Exception) -> None:
        """Queue responses (or exceptions to raise) for a route; the last one repeats once the others are used."""
        self.routes.setdefault((method, url), []).extend(responses)

    def requests(self, method: str, url: str) -> list[Call]:
        """Return the recorded requests to one route."""
        return [c for c in self.calls if (c.method, c.url) == (method, url)]

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Record a request and return its next queued response."""
        self.calls.append(Call(method, url, kwargs))
        queue = self.routes.get((method, url))
        if not queue:
            raise AssertionError(f"Unexpected request: {method} {url}")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """Record a POST request."""
        return self.request("POST", url, **kwargs)

    def ws_connect(self, url: str, **kwargs: Any) -> Any:
        """Websockets are patched per test."""
        raise NotImplementedError


@pytest.fixture
def session() -> FakeSession:
    """Return a fresh fake session."""
    return FakeSession()


@pytest.fixture(autouse=True)
def no_retry_delay() -> None:
    """Skip the delay between GraphQL retries."""
    with patch("custom_components.lyse_charge.api.GQL_RETRY_DELAY", 0):
        yield


def _api(session: FakeSession, **tokens: Any) -> LyseAPI:
    return LyseAPI(session, "user@example.com", "hunter2", **tokens)  # type: ignore[arg-type]


def _valid_token(**overrides: Any) -> dict[str, Any]:
    token: dict[str, Any] = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": time.time() + 3600,
        "audience": AUTH_AUDIENCE,
    }
    token.update(overrides)
    return token


def _add_password_login(session: FakeSession) -> None:
    """Queue Auth0's identifier-first redirect chain and the code exchange."""
    session.add(
        "GET",
        AUTHORIZE_URL,
        FakeResponse(302, headers={"Location": "/u/login/identifier?state=st"}, cookies={"auth0": "c1"}),
    )
    session.add("POST", IDENTIFIER_URL, FakeResponse(302, headers={"Location": "/u/login/password?state=st"}))
    session.add("POST", PASSWORD_URL, FakeResponse(302, headers={"Location": "/authorize/resume?state=st"}))
    session.add(
        "GET", RESUME_URL, FakeResponse(302, headers={"Location": f"{AUTH_REDIRECT_URI}?code=the-code&state=st"})
    )
    session.add(
        "POST",
        TOKEN_URL,
        FakeResponse(payload={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 86400}),
    )


# --- Endpoint and headers ---------------------------------------------------


async def test_graphql_goes_to_new_host_with_bearer_only(session: FakeSession) -> None:
    """Requests hit cch-api.lyse.cloud with only a bearer token, as app 1.5.23 does.

    The app no longer sends the x-api-key / client-id headers, which belonged to
    the retired cch-api.api.lyseenergi.no gateway.
    """
    session.add("POST", GRAPHQL_URL, FakeResponse(payload={"data": {"activeChargingSessions": []}}))

    assert await _api(session, **_valid_token()).get_active_sessions() == []

    (call,) = session.requests("POST", GRAPHQL_URL)
    assert call.headers["authorization"] == "Bearer access"
    assert "x-api-key" not in call.headers
    assert "client-id" not in call.headers


@pytest.mark.parametrize(
    ("method", "field_name"), [("start_charging", "startCharging"), ("stop_charging", "stopCharging")]
)
async def test_charging_mutations_send_flat_variables(session: FakeSession, method: str, field_name: str) -> None:
    """Start/stop pass chargerId and socketId as top-level variables, matching the app's mutations."""
    session.add("POST", GRAPHQL_URL, FakeResponse(payload={"data": {field_name: "Accepted"}}))

    assert await getattr(_api(session, **_valid_token()), method)("SMARTLY-0001", 1) == "Accepted"

    (call,) = session.requests("POST", GRAPHQL_URL)
    assert call.kwargs["json"]["variables"] == {"chargerId": "SMARTLY-0001", "socketId": 1}


async def test_graphql_errors_raise_after_retries(session: FakeSession) -> None:
    """A GraphQL error response raises LyseAPIError once the retries are spent."""
    session.add("POST", GRAPHQL_URL, FakeResponse(payload={"errors": [{"message": "Cannot query field"}]}))

    with pytest.raises(LyseAPIError, match="Cannot query field"):
        await _api(session, **_valid_token()).get_active_sessions()
    assert len(session.requests("POST", GRAPHQL_URL)) == 3


async def test_graphql_retries_gateway_error(session: FakeSession) -> None:
    """A gateway error, like the 504s the old host returned, is retried."""
    session.add(
        "POST",
        GRAPHQL_URL,
        FakeResponse(504, body="Gateway Time-out"),
        FakeResponse(payload={"data": {"activeChargingSessions": [{"chargerId": "C"}]}}),
    )

    assert await _api(session, **_valid_token()).get_active_sessions() == [{"chargerId": "C"}]


# --- Token lifecycle --------------------------------------------------------


async def test_valid_token_for_current_audience_is_reused(session: FakeSession) -> None:
    """An unexpired token issued for the current API makes no auth requests."""
    assert await _api(session, **_valid_token()).ensure_token() is False
    assert not session.calls


@pytest.mark.parametrize("audience", ["", "https://cch-api.api.lyseenergi.no"])
async def test_token_for_other_audience_forces_password_login(session: FakeSession, audience: str) -> None:
    """A token minted for the old API host is discarded, even if unexpired.

    Refreshing would keep the old audience, so the refresh token is not used either.
    Tokens stored before the audience was recorded have none, and count as old.
    """
    _add_password_login(session)
    api = _api(session, **_valid_token(audience=audience))

    assert await api.ensure_token() is True

    (exchange,) = session.requests("POST", TOKEN_URL)
    assert exchange.kwargs["json"]["grant_type"] == "authorization_code"
    assert api.token_data["access_token"] == "new-access"
    assert api.token_data["audience"] == AUTH_AUDIENCE


async def test_password_login_flow(session: FakeSession) -> None:
    """The login walks Auth0's identifier-first pages and exchanges the code with PKCE."""
    _add_password_login(session)
    api = _api(session)

    assert await api.ensure_token() is True

    (authorize,) = session.requests("GET", AUTHORIZE_URL)
    params = authorize.kwargs["params"]
    assert params["audience"] == "https://cch-api.lyse.cloud"
    assert params["redirect_uri"] == AUTH_REDIRECT_URI
    assert "offline_access" in params["scope"]

    (identifier,) = session.requests("POST", IDENTIFIER_URL)
    assert identifier.kwargs["data"] == {"state": "st", "username": "user@example.com"}
    assert identifier.headers["cookie"] == "auth0=c1"

    (password,) = session.requests("POST", PASSWORD_URL)
    assert password.kwargs["data"]["password"] == "hunter2"

    (exchange,) = session.requests("POST", TOKEN_URL)
    assert exchange.kwargs["json"]["code"] == "the-code"
    assert exchange.kwargs["json"]["code_verifier"]
    assert api.token_data == {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": pytest.approx(time.time() + 86400 - 3600, abs=60),
        "audience": AUTH_AUDIENCE,
    }


async def test_wrong_password_raises_auth_error(session: FakeSession) -> None:
    """Auth0 re-rendering the password page instead of redirecting is an auth failure."""
    session.add("GET", AUTHORIZE_URL, FakeResponse(302, headers={"Location": "/u/login/identifier?state=st"}))
    session.add("POST", IDENTIFIER_URL, FakeResponse(302, headers={"Location": "/u/login/password?state=st"}))
    session.add("POST", PASSWORD_URL, FakeResponse(400, body="<html>Wrong email or password</html>"))

    with pytest.raises(LyseAuthError, match="unexpected status 400"):
        await _api(session).ensure_token()


@pytest.mark.parametrize("status", [503, 429])
async def test_auth_server_outage_is_not_an_auth_error(session: FakeSession, status: int) -> None:
    """An Auth0 outage or rate limit raises LyseAPIError (retryable), not LyseAuthError (reauth)."""
    session.add("GET", AUTHORIZE_URL, FakeResponse(status, body="Service Unavailable"))

    with pytest.raises(LyseAPIError) as excinfo:
        await _api(session).ensure_token()
    assert not isinstance(excinfo.value, LyseAuthError)


@pytest.mark.parametrize("exc", [aiohttp.ClientConnectionError("refused"), TimeoutError()])
async def test_login_network_error_is_wrapped(session: FakeSession, exc: Exception) -> None:
    """Network errors and timeouts during login surface as LyseAPIError, not raw aiohttp errors."""
    session.add("GET", AUTHORIZE_URL, exc)

    with pytest.raises(LyseAPIError) as excinfo:
        await _api(session).ensure_token()
    assert not isinstance(excinfo.value, LyseAuthError)


async def test_code_exchange_server_error_is_not_an_auth_error(session: FakeSession) -> None:
    """A 5xx from the token endpoint after a successful login page is retryable."""
    _add_password_login(session)
    session.routes[("POST", TOKEN_URL)] = [FakeResponse(502, body="Bad Gateway")]

    with pytest.raises(LyseAPIError) as excinfo:
        await _api(session).ensure_token()
    assert not isinstance(excinfo.value, LyseAuthError)


async def test_auth_requests_have_a_timeout(session: FakeSession) -> None:
    """Every auth request is bounded, so a hung Auth0 can't stall Home Assistant setup."""
    _add_password_login(session)

    await _api(session).ensure_token()

    assert session.calls
    for call in session.calls:
        assert isinstance(call.kwargs.get("timeout"), aiohttp.ClientTimeout), f"{call.method} {call.url}"


async def test_expired_token_is_refreshed(session: FakeSession) -> None:
    """An expired token for the current audience is refreshed without a password login."""
    session.add("POST", TOKEN_URL, FakeResponse(payload={"access_token": "refreshed", "expires_in": 86400}))
    api = _api(session, **_valid_token(expires_at=0))

    assert await api.ensure_token() is True

    (call,) = session.requests("POST", TOKEN_URL)
    assert call.kwargs["json"]["grant_type"] == "refresh_token"
    assert api.token_data["access_token"] == "refreshed"
    assert api.token_data["refresh_token"] == "refresh"  # kept when not rotated


async def test_failed_refresh_falls_back_to_password_login(session: FakeSession) -> None:
    """A rejected refresh token falls back to logging in with the stored password."""
    session.add("POST", TOKEN_URL, FakeResponse(403, payload={"error": "invalid_grant"}))
    _add_password_login(session)
    api = _api(session, **_valid_token(expires_at=0))

    assert await api.ensure_token() is True

    grants = [c.kwargs["json"]["grant_type"] for c in session.requests("POST", TOKEN_URL)]
    assert grants == ["refresh_token", "authorization_code"]
    assert api.token_data["access_token"] == "new-access"


# --- Websocket subscription -------------------------------------------------


class FakeWebSocket:
    """A scripted graphql-ws server that sends queued frames and records client frames."""

    def __init__(self, server_frames: list[dict]) -> None:
        """Queue the frames the server sends, in order."""
        self._frames = list(server_frames)
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        """Record a frame sent by the client."""
        self.sent.append(data)

    def __aiter__(self) -> FakeWebSocket:
        """Iterate over server frames."""
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        """Return the next server frame as a text message."""
        if not self._frames:
            raise StopAsyncIteration
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(self._frames.pop(0)), None)

    async def __aenter__(self) -> Self:
        """Enter the connection context."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave the connection context."""


async def test_subscription_protocol(session: FakeSession) -> None:
    """The subscription speaks legacy graphql-ws to the new host and delivers activities."""
    activity = {"chargerStatus": "Charging", "socketStatus": "Occupied"}
    ws = FakeWebSocket(
        [
            {"type": "connection_ack"},
            {"type": "ka"},
            {"type": "data", "id": "1", "payload": {"data": {"chargerActivityStream": activity}}},
            {"type": "complete", "id": "1"},
        ]
    )
    events: list[dict] = []

    with patch.object(session, "ws_connect", return_value=ws) as ws_connect:
        await _api(session, **_valid_token()).subscribe_activity("SMARTLY-0001", 1, events.append)

    (url,), kwargs = ws_connect.call_args
    assert url == WS_URL
    assert kwargs["protocols"] == ["graphql-ws"]
    assert kwargs["headers"] == {"Authorization": "Bearer access"}

    init, start = ws.sent
    assert init == {"type": "connection_init", "payload": {}}
    assert start["type"] == "start"
    assert start["payload"]["variables"] == {"chargerId": "SMARTLY-0001", "socketId": 1}
    assert events == [activity]


async def test_subscription_errors_raise(session: FakeSession) -> None:
    """GraphQL errors in a subscription payload end the subscription with an error."""
    ws = FakeWebSocket(
        [{"type": "connection_ack"}, {"type": "data", "id": "1", "payload": {"errors": [{"message": "nope"}]}}]
    )

    with patch.object(session, "ws_connect", return_value=ws), pytest.raises(LyseAPIError, match="nope"):
        await _api(session, **_valid_token()).subscribe_activity("SMARTLY-0001", 1, lambda _: None)


async def test_code_exchange_rate_limit_is_not_an_auth_error(session: FakeSession) -> None:
    """A 429 from the token endpoint is retryable, as it is on the login pages."""
    _add_password_login(session)
    session.routes[("POST", TOKEN_URL)] = [FakeResponse(429, payload={"error": "too_many_requests"})]

    with pytest.raises(LyseAPIError) as excinfo:
        await _api(session).ensure_token()
    assert not isinstance(excinfo.value, LyseAuthError)


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(
            lambda session: session.routes.__setitem__(
                ("POST", TOKEN_URL), [FakeResponse(payload={"access_token": "a", "expires_in": 86400})]
            ),
            id="no-refresh-token",
        ),
        pytest.param(
            lambda session: session.routes.__setitem__(("POST", TOKEN_URL), [FakeResponse(body="<html>oops</html>")]),
            id="non-json-token-response",
        ),
        pytest.param(
            lambda session: session.routes.__setitem__(
                ("GET", AUTHORIZE_URL), [FakeResponse(302, headers={"Location": "/u/login/identifier"})]
            ),
            id="no-state",
        ),
    ],
)
async def test_unexpected_auth_response_is_an_api_error(session: FakeSession, setup: Any) -> None:
    """A malformed auth response raises LyseAPIError (retry), never a raw exception or LyseAuthError (reauth)."""
    _add_password_login(session)
    setup(session)

    with pytest.raises(LyseAPIError) as excinfo:
        await _api(session).ensure_token()
    assert not isinstance(excinfo.value, LyseAuthError)


async def test_malformed_refresh_response_falls_back_to_password_login(session: FakeSession) -> None:
    """A refresh response without an access token is treated like a failed refresh."""
    session.add("POST", TOKEN_URL, FakeResponse(payload={"expires_in": 86400}))
    _add_password_login(session)
    api = _api(session, **_valid_token(expires_at=0))

    assert await api.ensure_token() is True
    assert api.token_data["access_token"] == "new-access"
