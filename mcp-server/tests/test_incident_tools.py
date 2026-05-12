"""Tests for B-47 Privacy Incident MCP tools (GDPR Art. 33/34).

Covers the five tools: report_breach, list_incidents, assess_incident,
notify_authority, notify_data_subject. Uses mocked DB and OPA so the
suite runs without Docker.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from server import _dispatch


@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    mock_http = AsyncMock()
    mock_pool = AsyncMock()
    monkeypatch.setattr(server, "http", mock_http)
    monkeypatch.setattr(server, "pg_pool", mock_pool)

    async def _fake_get_pool():
        return mock_pool
    monkeypatch.setattr(server, "get_pg_pool", _fake_get_pool)

    async def _noop_log(*args, **kwargs):
        return None
    monkeypatch.setattr(server, "log_access", _noop_log)

    return mock_http, mock_pool


def _opa_response(mapping: dict):
    """Make a MagicMock response with a custom OPA result by URL suffix."""
    def _post(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        # match longest suffix
        for suffix, value in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            if url.endswith(suffix):
                resp.json.return_value = {"result": value}
                return resp
        resp.json.return_value = {"result": None}
        return resp
    return _post


def _mock_row(mapping: dict):
    row = MagicMock()
    row.__getitem__ = lambda self, k: mapping[k]
    return row


# ── report_breach ────────────────────────────────────────────


class TestReportBreach:
    async def test_viewer_can_report(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_report": True},
        )
        mock_pool.fetchrow.return_value = _mock_row({
            "id":          "11111111-1111-1111-1111-111111111111",
            "detected_at": datetime(2026, 5, 12, 9, 0, 0, tzinfo=timezone.utc),
            "status":      "detected",
        })

        result = await _dispatch(
            "report_breach",
            {"description": "Email leaked in chat", "source": "agent_report",
             "pii_types_found": ["EMAIL_ADDRESS"], "estimated_subjects": 1},
            "agent-7", "viewer",
        )

        payload = json.loads(result[0].text)
        assert payload["incident_id"] == "11111111-1111-1111-1111-111111111111"
        assert payload["status"] == "detected"
        # Verify the INSERT actually ran
        assert mock_pool.fetchrow.called
        sql = mock_pool.fetchrow.call_args[0][0]
        assert "INSERT INTO privacy_incidents" in sql

    async def test_unknown_role_denied(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_report": False},
        )

        result = await _dispatch(
            "report_breach",
            {"description": "x", "source": "agent_report"},
            "guest-1", "guest",
        )
        payload = json.loads(result[0].text)
        assert "not allowed" in payload["error"]

    async def test_missing_description_rejected(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_report": True},
        )
        result = await _dispatch(
            "report_breach",
            {"source": "manual_audit"},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "required" in payload["error"]

    async def test_invalid_source_rejected(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_report": True},
        )
        result = await _dispatch(
            "report_breach",
            {"description": "x", "source": "bogus"},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "invalid source" in payload["error"]


# ── list_incidents ───────────────────────────────────────────


class TestListIncidents:
    async def test_admin_can_list(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_list": True},
        )

        ts = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
        rows = [_mock_row({
            "id":                    "22222222-2222-2222-2222-222222222222",
            "detected_at":           ts,
            "detected_by":           "pii_scanner",
            "source":                "pii_scanner",
            "status":                "detected",
            "description":           "Test breach",
            "pii_types_found":       ["EMAIL_ADDRESS"],
            "data_category":         "customer_data",
            "notifiable_risk":       None,
            "authority_notified_at": None,
            "subject_notified_at":   None,
            "resolved_at":           None,
        })]
        mock_pool.fetch.return_value = rows

        result = await _dispatch("list_incidents", {}, "admin-1", "admin")
        payload = json.loads(result[0].text)
        assert payload["count"] == 1
        assert payload["incidents"][0]["incident_id"] == "22222222-2222-2222-2222-222222222222"

    async def test_analyst_denied(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_list": False},
        )
        result = await _dispatch("list_incidents", {}, "user-1", "analyst")
        payload = json.loads(result[0].text)
        assert "admin role" in payload["error"]

    async def test_attention_mode_uses_view(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_list": True},
        )

        ts = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        rows = [_mock_row({
            "id":                    "33333333-3333-3333-3333-333333333333",
            "detected_at":           ts,
            "hours_since_detection": 50.0,
            "status":                "detected",
            "source":                "manual_audit",
            "description":           "Approaching deadline",
            "pii_types_found":       ["PERSON"],
            "notifiable_risk":       True,
            "frist_warnung":         "CRITICAL: less than 24h until the 72h deadline",
        })]
        mock_pool.fetch.return_value = rows

        result = await _dispatch(
            "list_incidents", {"attention": True}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["count"] == 1
        assert payload["incidents"][0]["deadline_warning"].startswith("CRITICAL")
        # SQL should have referenced the view
        sql = mock_pool.fetch.call_args[0][0]
        assert "v_incidents_requiring_attention" in sql

    async def test_status_filter(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_list": True},
        )
        mock_pool.fetch.return_value = []
        await _dispatch(
            "list_incidents", {"status": "under_review"}, "admin-1", "admin",
        )
        sql = mock_pool.fetch.call_args[0][0]
        args = mock_pool.fetch.call_args[0][1:]
        assert "status = $1::incident_status" in sql
        assert args[0] == "under_review"


# ── assess_incident ──────────────────────────────────────────


class TestAssessIncident:
    async def test_admin_required(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_assess": False},
        )
        result = await _dispatch(
            "assess_incident", {"incident_id": "abc"}, "u", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "admin role" in payload["error"]

    async def test_incident_not_found(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_assess": True},
        )
        mock_pool.fetchrow.return_value = None

        result = await _dispatch(
            "assess_incident",
            {"incident_id": "44444444-4444-4444-4444-444444444444"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["error"] == "incident not found"

    async def test_auto_scores_and_updates(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response({
            "/v1/data/pb/incidents/allow_assess":  True,
            "/v1/data/pb/incidents/breakdown":     {
                "risk_score": 60,
                "notifiable": True,
                "base_score": 60,
                "high_hits": 2,
                "medium_hits": 0,
                "low_hits": 0,
                "subject_multiplier": 1.0,
                "category_multiplier": 1.0,
                "threshold": 50,
            },
        })

        mock_pool.fetchrow.side_effect = [
            # SELECT incident
            _mock_row({
                "id":                 "55555555-5555-5555-5555-555555555555",
                "pii_types_found":    ["EMAIL_ADDRESS", "PHONE_NUMBER"],
                "estimated_subjects": 1,
                "data_category":      "customer_data",
                "status":             "detected",
            }),
            # UPDATE incident
            _mock_row({
                "id":     "55555555-5555-5555-5555-555555555555",
                "status": "under_review",
            }),
        ]

        result = await _dispatch(
            "assess_incident",
            {"incident_id": "55555555-5555-5555-5555-555555555555"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["risk_score"] == 60
        assert payload["notifiable_risk"] is True
        assert payload["status"] == "under_review"
        assert "Auto-assessed" in payload["risk_assessment"]

    async def test_force_not_notifiable_requires_rationale(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_assess": True},
        )
        result = await _dispatch(
            "assess_incident",
            {"incident_id": "55555555-5555-5555-5555-555555555555",
             "force_not_notifiable": True},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "risk_assessment is required" in payload["error"]

    async def test_force_notifiable_overrides_score(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response({
            "/v1/data/pb/incidents/allow_assess": True,
            "/v1/data/pb/incidents/breakdown":    {
                "risk_score": 10,
                "notifiable": False,
            },
        })

        mock_pool.fetchrow.side_effect = [
            _mock_row({
                "id":                 "66666666-6666-6666-6666-666666666666",
                "pii_types_found":    ["ORG"],
                "estimated_subjects": 1,
                "data_category":      "internal",
                "status":             "detected",
            }),
            _mock_row({
                "id":     "66666666-6666-6666-6666-666666666666",
                "status": "under_review",
            }),
        ]

        result = await _dispatch(
            "assess_incident",
            {"incident_id": "66666666-6666-6666-6666-666666666666",
             "force_notifiable": True,
             "risk_assessment": "DPO judgment: sensitive context"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["notifiable_risk"] is True
        assert "DPO judgment" in payload["risk_assessment"]


# ── notify_authority ─────────────────────────────────────────


class TestNotifyAuthority:
    async def test_admin_required(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_notify_authority": False},
        )
        result = await _dispatch(
            "notify_authority",
            {"incident_id": "x", "authority_name": "BfDI"},
            "u", "developer",
        )
        payload = json.loads(result[0].text)
        assert "admin role" in payload["error"]

    async def test_happy_path_updates_status(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_notify_authority": True},
        )

        notified_at = datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc)
        mock_pool.fetchrow.return_value = _mock_row({
            "id":                    "77777777-7777-7777-7777-777777777777",
            "status":                "notified_authority",
            "authority_notified_at": notified_at,
            "authority_ref":         "BfDI | DPO-2026-0042 | via online_portal",
        })

        result = await _dispatch(
            "notify_authority",
            {"incident_id":         "77777777-7777-7777-7777-777777777777",
             "authority_name":      "BfDI",
             "authority_ref":       "DPO-2026-0042",
             "notification_method": "online_portal"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["status"] == "notified_authority"
        assert payload["authority_ref"].startswith("BfDI")

    async def test_missing_authority_name_rejected(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_notify_authority": True},
        )
        result = await _dispatch(
            "notify_authority",
            {"incident_id": "x"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "authority_name" in payload["error"]


# ── notify_data_subject ──────────────────────────────────────


class TestNotifyDataSubject:
    async def test_admin_required(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_notify_subject": False},
        )
        result = await _dispatch(
            "notify_data_subject",
            {"incident_id": "x", "subject_ref": "s", "channel": "email"},
            "u", "viewer",
        )
        payload = json.loads(result[0].text)
        assert "admin role" in payload["error"]

    async def test_invalid_channel_rejected(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_notify_subject": True},
        )
        result = await _dispatch(
            "notify_data_subject",
            {"incident_id": "x", "subject_ref": "s", "channel": "smoke_signal"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "invalid channel" in payload["error"]

    async def test_first_call_moves_status(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = _opa_response(
            {"/v1/data/pb/incidents/allow_notify_subject": True},
        )

        notified_at = datetime(2026, 5, 12, 15, 0, 0, tzinfo=timezone.utc)
        mock_pool.fetchrow.return_value = _mock_row({
            "id":                  "88888888-8888-8888-8888-888888888888",
            "status":              "notified_subject",
            "subject_notified_at": notified_at,
        })

        result = await _dispatch(
            "notify_data_subject",
            {"incident_id": "88888888-8888-8888-8888-888888888888",
             "subject_ref": "subject-42",
             "channel":     "email",
             "template_id": "tpl-2026-art34-v1"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["status"] == "notified_subject"
        assert payload["subject_ref"] == "subject-42"
        # The SQL should reference jsonb ledger append
        sql = mock_pool.fetchrow.call_args[0][0]
        assert "subject_notifications" in sql
