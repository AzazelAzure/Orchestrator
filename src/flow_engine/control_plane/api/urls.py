"""Versioned API routes."""

from django.urls import path

from flow_engine.control_plane.api.views_mcp import (
    McpDepartmentProfilesView,
    McpLaneInvokeView,
    McpLaneSnapshotView,
    McpLaneToolsView,
)
from flow_engine.control_plane.api.views_runtime import (
    DeliveryListView,
    RuntimeCancelView,
    RuntimeHeartbeatView,
    RuntimePauseView,
    RuntimePreviewView,
    RuntimeRecoverView,
    RuntimeResultView,
    RuntimeResumeView,
    RuntimeRunView,
    RuntimeShowView,
)
from flow_engine.control_plane.api.views_scripts import (
    ScheduleCompleteView,
    ScheduleRunOnDemandView,
    ScheduleStatusView,
    ScheduleTemplatesView,
    ScheduleTickView,
    ScriptAllowlistView,
    ScriptCancelView,
    ScriptExecuteView,
    ScriptShowView,
)

urlpatterns = [
    path("runtime/preview", RuntimePreviewView.as_view(), name="runtime-preview"),
    path("runtime/run", RuntimeRunView.as_view(), name="runtime-run"),
    path("runtime/runs/<str:run_id>", RuntimeShowView.as_view(), name="runtime-show"),
    path("runtime/heartbeat", RuntimeHeartbeatView.as_view(), name="runtime-heartbeat"),
    path("runtime/result", RuntimeResultView.as_view(), name="runtime-result"),
    path("runtime/recover", RuntimeRecoverView.as_view(), name="runtime-recover"),
    path("runtime/pause", RuntimePauseView.as_view(), name="runtime-pause"),
    path("runtime/resume", RuntimeResumeView.as_view(), name="runtime-resume"),
    path("runtime/cancel", RuntimeCancelView.as_view(), name="runtime-cancel"),
    path("delivery/jobs", DeliveryListView.as_view(), name="delivery-list"),
    path(
        "mcp/profiles",
        McpDepartmentProfilesView.as_view(),
        name="mcp-department-profiles",
    ),
    path(
        "mcp/lanes/<str:lane_id>/snapshot",
        McpLaneSnapshotView.as_view(),
        name="mcp-lane-snapshot",
    ),
    path(
        "mcp/lanes/<str:lane_id>/tools",
        McpLaneToolsView.as_view(),
        name="mcp-lane-tools",
    ),
    path(
        "mcp/lanes/<str:lane_id>/tools/invoke",
        McpLaneInvokeView.as_view(),
        name="mcp-lane-invoke",
    ),
    path("scripts/allowlist", ScriptAllowlistView.as_view(), name="script-allowlist"),
    path("scripts/execute", ScriptExecuteView.as_view(), name="script-execute"),
    path(
        "scripts/executions/<str:execution_id>",
        ScriptShowView.as_view(),
        name="script-show",
    ),
    path("scripts/cancel", ScriptCancelView.as_view(), name="script-cancel"),
    path(
        "schedules/templates",
        ScheduleTemplatesView.as_view(),
        name="schedule-templates",
    ),
    path("schedules/status", ScheduleStatusView.as_view(), name="schedule-status"),
    path("schedules/tick", ScheduleTickView.as_view(), name="schedule-tick"),
    path("schedules/complete", ScheduleCompleteView.as_view(), name="schedule-complete"),
    path("schedules/run", ScheduleRunOnDemandView.as_view(), name="schedule-run"),
]
