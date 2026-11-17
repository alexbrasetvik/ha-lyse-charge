"""Constants for the Lyse Charge integration."""

DOMAIN = "lyse_charge"

# Moved from cch-api.api.lyseenergi.no in app 1.5.23 (Nov 2026); the old host is gone.
API_HOST = "https://cch-api.lyse.cloud"
WS_URL = "wss://cch-api.lyse.cloud/graphql"
AUTH_BASE = "https://auth.lyselading.no"
AUTH_URL = f"{AUTH_BASE}/oauth/token"
AUTH_REDIRECT_URI = "no.lyse.lading://auth.lyselading.no/ios/no.lyse.lading/callback"
AUTH_AUDIENCE = API_HOST
CLIENT_ID = "FyCrFTaMp0pjGTV5djrfMIHqPQMczKbs"

POLL_INTERVAL = 30
WS_MAX_BACKOFF = 120
TOKEN_REFRESH_BUFFER = 3600
REQUEST_TIMEOUT = 10
GQL_RETRY_ATTEMPTS = 3
GQL_RETRY_DELAY = 2
MAX_CONSECUTIVE_FAILURES = 3

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_EXPIRES_AT = "expires_at"
CONF_AUDIENCE = "audience"
CONF_CHARGERS = "chargers"
CONF_CHARGER_ID = "charger_id"
CONF_ALIAS = "alias"
