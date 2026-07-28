from django.apps import AppConfig


class ControlPlaneApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "flow_engine.control_plane.api"
    label = "orch_control_plane_api"
