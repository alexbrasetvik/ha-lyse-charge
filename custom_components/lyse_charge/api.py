"""API client for the Lyse Charge cloud service."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import hashlib
import json
import logging
import secrets
import time
from typing import Any
import urllib.parse

import aiohttp

from .const import (
    API_HOST,
    AUTH_AUDIENCE,
    AUTH_BASE,
    AUTH_REDIRECT_URI,
    AUTH_URL,
    CLIENT_ID,
    GQL_RETRY_ATTEMPTS,
    GQL_RETRY_DELAY,
    REQUEST_TIMEOUT,
    TOKEN_REFRESH_BUFFER,
    WS_URL,
)

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_ERROR_DETAIL_LEN = 200

_LOGGER = logging.getLogger(__name__)

_GQL_CHARGERS_BY_IDS = """
query ChargersByIds($ids: [String!]!) {
  chargersByIds(chargerIds: $ids) {
    chargerId chargerName chargerStatus
    chargerModel serialNumber
    groupName publicGroupName
    customProperties { alias public }
    sockets { id status }
  }
}
"""

_GQL_ACTIVE_SESSIONS = """
query GetActiveChargingSessions {
  activeChargingSessions {
    chargerId socketId transactionId
    timeStamp action authorizedIdTag meterRead
    customData { alias }
  }
}
"""

_GQL_ME = """
query UserProfile {
  me {
    auth0Id
    dataverse { fullName email hasValidContract }
  }
}
"""

_GQL_SESSION_HISTORY = """
query ChargingSessions {
  chargingSessions(year: null) {
    chargingSessions {
      chargerId chargerAlias
    }
  }
}
"""

_GQL_START = """
mutation StartCharging($chargerId: String!, $socketId: Int!) {
  startCharging(input: { chargerId: $chargerId, socketId: $socketId })
}
"""

_GQL_STOP = """
mutation StopCharging($chargerId: String!, $socketId: Int!) {
  stopCharging(input: { chargerId: $chargerId, socketId: $socketId })
}
"""

_GQL_WATCH = """
subscription Watch($chargerId: String!, $socketId: Int!) {
  chargerActivityStream(chargerId: $chargerId, socketId: $socketId) {
    timestamp chargerStatus socketStatus message errorCode
    latestChargerInfo latestSocketInfo
    activeChargeSession {
      transactionId startTime
      currentMeterValueKwh currentPowerKw
      currentEnergyChargedKwh currentCostInclMva
    }
    tariff {
      currency
      energyComponent { priceInclMva mvaPercentage }
      spotComponent { priceInclMva margin }
      flatComponent { priceInclMva }
    }
  }
}
"""


def _safe_error_detail(body: str) -> str:
    """Extract a safe, human-readable error detail from an OAuth error response.

    Surface only the standard "error"/"error_description" fields, so the raw
    response body, which could reflect request contents, stays out of logs
    and exception messages.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "unreadable response"
    if isinstance(parsed, dict):
        detail = parsed.get("error_description") or parsed.get("error")
        if detail:
            return str(detail)[:_MAX_ERROR_DETAIL_LEN]
    return "unrecognized response"


def _operation_name(query: str) -> str:
    """Return the GraphQL operation name (e.g. "query ChargersByIds") for logging."""
    return query.strip().splitlines()[0].split("(")[0].strip()


class LyseAPIError(Exception):
    """Raised when a Lyse Charge API call fails."""


class LyseAuthError(LyseAPIError):
    """Raised when authentication with the Lyse Charge API fails."""


class LyseAPI:
    """Client for the Lyse Charge cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        access_token: str = "",
        refresh_token: str = "",
        expires_at: float = 0,
    ) -> None:
        """Initialize the API client with credentials and, optionally, a previously-obtained token."""
        self._session = session
        self._email = email
        self._password = password
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at

    @property
    def token_data(self) -> dict:
        """The current token state, suitable for persisting to a config entry."""
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
        }

    async def ensure_token(self) -> bool:
        """Ensure the access token is valid, refreshing or logging in again as needed.

        Tries the refresh token first, if one is available, and falls back to a fresh
        email/password login otherwise (or if the refresh fails). Returns True if a new
        token was obtained, meaning the caller should persist `token_data`.
        """
        if time.time() < self._expires_at:
            return False

        token_data = None
        if self._refresh_token:
            try:
                token_data = await self._refresh_access_token()
            except LyseAPIError as err:
                _LOGGER.debug("Token refresh failed, falling back to password login: %s", err)

        if token_data is None:
            token_data = await self._login_with_password()

        self._access_token = token_data["access_token"]
        self._refresh_token = token_data["refresh_token"]
        self._expires_at = token_data["expires_at"]
        return True

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "content-type": "application/json",
            "accept": "application/graphql-response+json, application/json",
        }

    async def _refresh_access_token(self) -> dict:
        """Exchange the stored refresh token for a new access token."""
        _LOGGER.debug("Refreshing access token")
        payload = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": self._refresh_token,
        }
        async with self._session.post(AUTH_URL, json=payload) as resp:
            if not resp.ok:
                detail = _safe_error_detail(await resp.text())
                _LOGGER.error("Token refresh failed with HTTP %s: %s", resp.status, detail)
                if resp.status in (401, 403):
                    raise LyseAuthError(f"Token refresh failed {resp.status}: {detail}")
                raise LyseAPIError(f"Token refresh failed {resp.status}: {detail}")
            data = await resp.json()
        expires_at = time.time() + data.get("expires_in", 86400) - TOKEN_REFRESH_BUFFER
        _LOGGER.debug("Access token refreshed, expires at %s", expires_at)
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", self._refresh_token),
            "expires_at": expires_at,
        }

    async def _login_with_password(self) -> dict:
        """Log in via Auth0's hosted login (identifier-first + PKCE) and exchange the code for tokens."""
        _LOGGER.debug("Starting password login")
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()

        authorize_params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": AUTH_REDIRECT_URI,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "audience": AUTH_AUDIENCE,
        }

        cookies: dict[str, str] = {}
        identifier_url = urllib.parse.urljoin(
            AUTH_BASE, await self._auth_redirect("GET", f"{AUTH_BASE}/authorize", cookies, params=authorize_params)
        )
        state = urllib.parse.parse_qs(urllib.parse.urlparse(identifier_url).query)["state"][0]

        password_url = urllib.parse.urljoin(
            AUTH_BASE,
            await self._auth_redirect(
                "POST",
                identifier_url,
                cookies,
                referer=identifier_url,
                data={"state": state, "username": self._email},
            ),
        )

        resume_url = urllib.parse.urljoin(
            AUTH_BASE,
            await self._auth_redirect(
                "POST",
                password_url,
                cookies,
                referer=password_url,
                data={"state": state, "username": self._email, "password": self._password},
            ),
        )

        callback_url = await self._auth_redirect("GET", resume_url, cookies, referer=password_url)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query).get("code")
        if not code:
            _LOGGER.error("Login failed: no authorization code returned")
            raise LyseAuthError("Login failed: no authorization code returned")

        payload = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code[0],
            "redirect_uri": AUTH_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        async with self._session.post(AUTH_URL, json=payload) as resp:
            if not resp.ok:
                detail = _safe_error_detail(await resp.text())
                _LOGGER.error("Code exchange failed with HTTP %s: %s", resp.status, detail)
                raise LyseAuthError(f"Code exchange failed {resp.status}: {detail}")
            data = await resp.json()

        expires_at = time.time() + data.get("expires_in", 86400) - TOKEN_REFRESH_BUFFER
        _LOGGER.debug("Password login succeeded, token expires at %s", expires_at)
        return {"access_token": data["access_token"], "refresh_token": data["refresh_token"], "expires_at": expires_at}

    async def _auth_redirect(
        self,
        method: str,
        url: str,
        cookies: dict[str, str],
        *,
        params: dict | None = None,
        data: dict | None = None,
        referer: str | None = None,
    ) -> str:
        """Make one hop of the Auth0 login flow, tracking cookies manually, and return the Location header."""
        headers = {}
        if referer:
            headers["Referer"] = referer
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        # Log the path only: the request body (data=) may carry the email/password
        # for this hop, and the response body may echo request content back.
        path = urllib.parse.urlsplit(url).path
        _LOGGER.debug("Auth redirect hop: %s %s", method, path)

        async with self._session.request(
            method, url, params=params, data=data, headers=headers, allow_redirects=False
        ) as resp:
            for key, morsel in resp.cookies.items():
                cookies[key] = morsel.value
            if resp.status not in _REDIRECT_STATUSES:
                _LOGGER.error("Login failed at %s: unexpected status %s", path, resp.status)
                raise LyseAuthError(f"Login failed at {path}: unexpected status {resp.status}")
            location = resp.headers.get("Location")
            if not location:
                _LOGGER.error("Login failed at %s: redirect missing Location header", path)
                raise LyseAuthError(f"Login failed at {path}: redirect missing Location header")
            return location

    async def _post_graphql(self, operation: str, body: dict[str, Any]) -> dict:
        """Post a single GraphQL request and return its data payload, or raise LyseAPIError."""
        async with self._session.post(
            f"{API_HOST}/graphql",
            json=body,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise LyseAPIError(f"{operation} failed with HTTP {resp.status}: {text[:_MAX_ERROR_DETAIL_LEN]}")
            data = await resp.json()
        if errors := data.get("errors"):
            raise LyseAPIError(f"{operation} returned GraphQL errors: {errors}")
        return data.get("data") or {}

    async def execute_graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query or mutation, retrying transient failures, and return its data payload."""
        operation = _operation_name(query)
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables

        for attempt in range(1, GQL_RETRY_ATTEMPTS + 1):
            _LOGGER.debug("GraphQL request: %s (attempt %s/%s)", operation, attempt, GQL_RETRY_ATTEMPTS)
            try:
                return await self._post_graphql(operation, body)
            except (LyseAPIError, aiohttp.ClientError, TimeoutError) as err:
                if attempt >= GQL_RETRY_ATTEMPTS:
                    _LOGGER.error("%s failed after %s attempt(s): %s", operation, attempt, err)
                    if isinstance(err, LyseAPIError):
                        raise
                    raise LyseAPIError(f"{operation} failed after {attempt} attempt(s): {err}") from err
                _LOGGER.debug(
                    "%s attempt %s/%s failed, retrying in %ss: %s",
                    operation,
                    attempt,
                    GQL_RETRY_ATTEMPTS,
                    GQL_RETRY_DELAY,
                    err,
                )
                await asyncio.sleep(GQL_RETRY_DELAY)
        raise AssertionError("unreachable")  # loop above always returns or raises

    async def get_me(self) -> dict:
        """Return the current user's profile."""
        _LOGGER.debug("Fetching user profile")
        data = await self.execute_graphql(_GQL_ME)
        return data.get("me") or {}

    async def get_charger_ids_from_history(self) -> list[dict]:
        """Return chargers the user has previously charged at, from session history."""
        data = await self.execute_graphql(_GQL_SESSION_HISTORY)
        sessions = (data.get("chargingSessions") or {}).get("chargingSessions") or []
        seen: dict[str, dict] = {}
        for s in sessions:
            cid = s.get("chargerId")
            if cid and cid not in seen:
                seen[cid] = {
                    "charger_id": cid,
                    "alias": s.get("chargerAlias") or cid,
                }
        _LOGGER.debug("Discovered %d charger(s) from session history", len(seen))
        return list(seen.values())

    async def get_chargers(self, charger_ids: list[str]) -> list[dict]:
        """Return charger details for the given charger IDs."""
        data = await self.execute_graphql(_GQL_CHARGERS_BY_IDS, {"ids": charger_ids})
        return data.get("chargersByIds") or []

    async def get_active_sessions(self) -> list[dict]:
        """Return all currently active charging sessions."""
        data = await self.execute_graphql(_GQL_ACTIVE_SESSIONS)
        return data.get("activeChargingSessions") or []

    async def start_charging(self, charger_id: str, socket_id: int) -> str | None:
        """Start a charging session on the given charger socket."""
        data = await self.execute_graphql(_GQL_START, {"chargerId": charger_id, "socketId": socket_id})
        result = data.get("startCharging")
        return str(result) if result is not None else None

    async def stop_charging(self, charger_id: str, socket_id: int) -> str | None:
        """Stop the charging session on the given charger socket."""
        data = await self.execute_graphql(_GQL_STOP, {"chargerId": charger_id, "socketId": socket_id})
        result = data.get("stopCharging")
        return str(result) if result is not None else None

    async def subscribe_activity(
        self,
        charger_id: str,
        socket_id: int,
        on_event: Callable[[dict], None],
    ) -> None:
        """Connect to chargerActivityStream and call on_event for each activity.

        Runs until the connection drops or an error occurs.
        The caller handles reconnecting.
        """
        ws_headers = {
            "Authorization": f"Bearer {self._access_token}",
        }
        async with self._session.ws_connect(
            WS_URL,
            headers=ws_headers,
            protocols=["graphql-ws"],
            heartbeat=30,
        ) as ws:
            _LOGGER.debug("WS connected for charger %s socket %s", charger_id, socket_id)
            await ws.send_json({"type": "connection_init", "payload": {}})

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    frame = json.loads(msg.data)
                    msg_type = frame.get("type")

                    if msg_type == "connection_ack":
                        _LOGGER.debug("WS subscribing to charger %s socket %s", charger_id, socket_id)
                        await ws.send_json(
                            {
                                "type": "start",
                                "id": "1",
                                "payload": {
                                    "query": _GQL_WATCH,
                                    "variables": {
                                        "chargerId": charger_id,
                                        "socketId": socket_id,
                                    },
                                },
                            }
                        )
                    elif msg_type == "ka":
                        pass  # graphql-ws keep-alive ping; no action needed
                    elif msg_type == "data":
                        payload = frame.get("payload") or {}
                        if gql_errors := payload.get("errors"):
                            raise LyseAPIError(f"Subscription errors: {gql_errors}")
                        activity = (payload.get("data") or {}).get("chargerActivityStream")
                        if activity:
                            on_event(activity)
                    elif msg_type == "connection_error":
                        raise LyseAPIError(f"WS connection_error: {frame}")
                    elif msg_type == "complete":
                        _LOGGER.debug("WS subscription complete for charger %s socket %s", charger_id, socket_id)
                        break

                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise LyseAPIError(f"WS closed/error: {msg}")
