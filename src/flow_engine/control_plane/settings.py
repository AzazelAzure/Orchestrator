"""Django settings for Orchestrator control-plane API (R4A)."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def validate_runtime_settings(
    *,
    secret_key: str | None,
    allowed_hosts: str | None,
    testing: bool,
) -> tuple[str, list[str]]:
    """Fail-closed secret/host validation (unit-testable)."""
    secret = (secret_key or "").strip()
    if not secret:
        if testing:
            secret = "test-only-insecure-secret"
        else:
            raise RuntimeError(
                "DJANGO_SECRET_KEY is required (fail closed; no default secret)"
            )

    hosts_raw = (allowed_hosts or "").strip()
    if not hosts_raw or hosts_raw == "*":
        if testing:
            hosts_raw = "testserver,localhost,127.0.0.1"
        else:
            raise RuntimeError(
                "DJANGO_ALLOWED_HOSTS must be set to an explicit host list (no wildcards)"
            )
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    return secret, hosts


_testing = os.environ.get("ORCH_TESTING", "0") == "1"
SECRET_KEY, ALLOWED_HOSTS = validate_runtime_settings(
    secret_key=os.environ.get("DJANGO_SECRET_KEY"),
    allowed_hosts=os.environ.get("DJANGO_ALLOWED_HOSTS"),
    testing=_testing,
)

# Default DEBUG off.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "flow_engine.control_plane.api.apps.ControlPlaneApiConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "flow_engine.control_plane.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True
TIME_ZONE = "UTC"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "flow_engine.control_plane.api.authentication.OrchestratorPrincipalAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Orchestrator Control Plane API",
    "DESCRIPTION": "Versioned REST adapter over the state coordinator command boundary.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://coordinator:9001")

_cors_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(",") if o.strip()]
CORS_ALLOW_CREDENTIALS = False

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
