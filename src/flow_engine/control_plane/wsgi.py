"""WSGI entrypoint for control-plane API."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flow_engine.control_plane.settings")

application = get_wsgi_application()
