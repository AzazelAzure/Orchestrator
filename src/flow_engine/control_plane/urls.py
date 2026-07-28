"""URL routing for versioned control-plane API."""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("api/v1/", include("flow_engine.control_plane.api.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("health/", include("flow_engine.control_plane.api.health_urls")),
    path("ops/summary/", include("flow_engine.control_plane.api.ops_urls")),
]
