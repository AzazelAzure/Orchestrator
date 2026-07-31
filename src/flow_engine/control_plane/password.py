"""Django password hasher helpers for coordinator-backed user accounts.

Uses Django's make_password / check_password only. Coordinator processes may
not have full control-plane settings; configure a minimal hasher surface when
needed without loading DJANGO_SECRET_KEY / ALLOWED_HOSTS.
"""

from __future__ import annotations


def _ensure_hashers_ready() -> None:
    from django.conf import settings

    if settings.configured:
        return
    settings.configure(
        PASSWORD_HASHERS=[
            "django.contrib.auth.hashers.PBKDF2PasswordHasher",
            "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
        ],
        USE_TZ=True,
    )


def hash_password(raw_password: str) -> str:
    _ensure_hashers_ready()
    from django.contrib.auth.hashers import make_password

    return make_password(raw_password)


def verify_password(raw_password: str, encoded: str) -> bool:
    _ensure_hashers_ready()
    from django.contrib.auth.hashers import check_password

    if not raw_password or not encoded:
        return False
    return bool(check_password(raw_password, encoded))
