from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from scripts.orchestrator_live_acceptance import check_delegation_invoke, check_ops_summary


def test_check_ops_summary_sends_founder_bearer() -> None:
    body = json.dumps({"status": "ok"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp

    with patch("scripts.orchestrator_live_acceptance.urllib.request.urlopen") as urlopen:
        urlopen.return_value = mock_resp
        result = check_ops_summary(
            "http://127.0.0.1:8000/ops/summary/",
            founder_token="founder-secret",
        )

    assert result == {
        "passed": True,
        "status": "ok",
        "url": "http://127.0.0.1:8000/ops/summary/",
    }
    req = urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer founder-secret"
    assert req.get_header("Accept") == "application/json"


def test_check_ops_summary_degraded_counts_as_pass() -> None:
    body = json.dumps({"status": "degraded"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp

    with patch("scripts.orchestrator_live_acceptance.urllib.request.urlopen") as urlopen:
        urlopen.return_value = mock_resp
        result = check_ops_summary(
            "http://127.0.0.1:8000/ops/summary/",
            founder_token="founder-secret",
        )

    assert result["passed"] is True
    assert result["status"] == "degraded"


def test_check_ops_summary_other_status_fails() -> None:
    body = json.dumps({"status": "error"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp

    with patch("scripts.orchestrator_live_acceptance.urllib.request.urlopen") as urlopen:
        urlopen.return_value = mock_resp
        result = check_ops_summary(
            "http://127.0.0.1:8000/ops/summary/",
            founder_token="founder-secret",
        )

    assert result["passed"] is False
    assert result["status"] == "error"


def test_check_delegation_invoke_accepts_404_not_found() -> None:
    class FakeApi:
        def request(self, *args, **kwargs):
            return 404, {"detail": "NOT_FOUND"}

    result = check_delegation_invoke(FakeApi())
    assert result["passed"] is True
    assert result["http_status"] == 404
    assert result["negative_result"] == "NOT_FOUND"
