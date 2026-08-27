"""Constants for the Lyse Charge integration."""

DOMAIN = "lyse_charge"

API_HOST = "https://cch-api.api.lyseenergi.no"
WS_URL = "wss://cch-api.api.lyseenergi.no/graphql"
AUTH_BASE = "https://auth.lyselading.no"
AUTH_URL = f"{AUTH_BASE}/oauth/token"
AUTH_REDIRECT_URI = "no.lyse.lading://auth.lyselading.no/ios/no.lyse.lading/callback"
AUTH_AUDIENCE = API_HOST
CLIENT_ID = "FyCrFTaMp0pjGTV5djrfMIHqPQMczKbs"
API_KEY = "0b932bb6e21447db8067d6ca10578584"

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
CONF_CHARGERS = "chargers"
CONF_CHARGER_ID = "charger_id"
CONF_ALIAS = "alias"
