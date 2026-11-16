"""Consulta del voto propio: vista y SDK reales sobre HTTP simulado, sin red ni DB."""
from uuid import UUID
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase

from apps.plans.services import election_service
from apps.plans.tests.test_services import GROUP_ID, PLAN_ID, USER_ID, plan_row, proposal_row
from apps.users.services import clients, session_service


class PlanVoteQueryTests(SimpleTestCase):
    def setUp(self):
        self.requests, self.transports = [], []
        self.vote_id = 47
        self.vote_unavailable = False
        self.plan = plan_row(
            election_method="votes", selected_proposal_id=None, confirmed_at=None,
        )
        self.proposals = [
            proposal_row(votes=4, date_pick="2026-10-31T18:00:00Z"),
            proposal_row(id=48, tittle="Otra alternativa", votes=2,
                         date_pick="2026-10-31T19:00:00Z"),
        ]

        def factory():
            transport = httpx.Client(transport=httpx.MockTransport(self.handle))
            self.transports.append(transport)
            return transport

        for patcher in (
            patch.object(clients, "http_client", side_effect=factory),
            patch.object(session_service, "_active_token", return_value="synthetic-user-jwt"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        voted = patch.object(election_service, "voted_proposal", wraps=election_service.voted_proposal)
        self.voted = voted.start()
        self.addCleanup(voted.stop)

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path == "/auth/v1/user":
            data = {"id": USER_ID, "aud": "authenticated", "role": "authenticated",
                    "email": "audit@example.invalid", "app_metadata": {}, "user_metadata": {},
                    "created_at": "2026-09-30T12:00:00Z"}
        elif path == "/rest/v1/profiles":
            data = [{"id": USER_ID, "username": "Ana", "created_at": "2026-09-30T12:00:00Z",
                     "updated_at": "2026-09-30T12:00:00Z"}]
        elif path == "/rest/v1/plans":
            data = [self.plan]
        elif path == "/rest/v1/groups":
            data = [{"id": GROUP_ID, "name": "Amigos", "description": "Grupo de prueba",
                     "created_by": USER_ID, "created_at": "2026-09-30T12:00:00Z",
                     "updated_at": "2026-09-30T12:00:00Z"}]
        elif path == "/rest/v1/group_members":
            data = [{"role": "owner"}]
        elif path == "/rest/v1/proposals":
            data = self.proposals
        elif path == "/rest/v1/rpc/proposal_vote_for_user":
            if self.vote_unavailable:
                return httpx.Response(503, json={"code": "synthetic_unavailable", "message": "Unavailable",
                                                 "details": None, "hint": None})
            data = self.vote_id
        elif path == "/rest/v1/notifications":
            return httpx.Response(200, headers={"Content-Range": "*/0"}, content=b"")
        else:
            raise AssertionError("Operación HTTP inesperada")
        return httpx.Response(200, json=data)

    def confirm(self):
        self.plan.update(selected_proposal_id=47, confirmed_at="2026-10-22T18:00:00Z",
                         scheduled_at="2026-10-31T18:00:00Z")

    def get_detail(self):
        return self.client.get(f"/planes/{PLAN_ID}/")

    def assert_http_count(self, total, vote_calls):
        self.assertEqual(len(self.requests), total)
        self.assertEqual(self.requests.count(("POST", "/rest/v1/rpc/proposal_vote_for_user")), vote_calls)
        self.assertEqual(self.requests.count(("GET", "/auth/v1/user")), 1)
        self.assertEqual(self.requests.count(("GET", "/rest/v1/group_members")), 1)
        self.assertEqual(self.requests.count(("HEAD", "/rest/v1/notifications")), 1)
        self.assertTrue(all(transport.is_closed for transport in self.transports))

    def assert_standings(self, response):
        self.assertEqual(response.context["election_method"], "votes")
        self.assertEqual(response.context["champion_id"], 47)
        self.assertEqual([(p["id"], p["votes"]) for p in response.context["leaderboard"]],
                         [(47, 4), (48, 2)])

    def test_confirmed_plan_skips_vote_query_and_preserves_result_and_history(self):
        self.confirm()
        response = self.get_detail()
        self.assertEqual(response.status_code, 200)
        self.voted.assert_not_called()
        self.assertIsNone(response.context["voted_proposal_id"])
        self.assertEqual(response.context["confirmed_proposal"]["id"], 47)
        self.assertEqual(len(response.context["proposals"]), 2)
        self.assert_standings(response)
        self.assertContains(response, "Cómo llegamos a esta decisión")
        self.assertContains(response, "Otra alternativa")
        self.assertNotContains(response, "Quitar voto")
        self.assert_http_count(total=8, vote_calls=0)

    def test_open_plan_keeps_vote_query_and_actions_regardless_of_active_status(self):
        for status in (True, False):
            with self.subTest(status=status):
                self.plan["status"] = status
                self.requests.clear()
                self.voted.reset_mock()
                response = self.get_detail()
                self.assertEqual(response.status_code, 200)
                self.voted.assert_called_once_with("synthetic-user-jwt", plan_id=UUID(PLAN_ID))
                self.assertEqual(response.context["voted_proposal_id"], 47)
                self.assertContains(response, "Tu voto")
                self.assertContains(response, "Quitar voto")
                self.assertContains(response, f'action="/planes/{PLAN_ID}/propuestas/48/votar/"')
                self.assert_standings(response)
                self.assert_http_count(total=9, vote_calls=1)

    def test_open_plan_without_previous_vote_keeps_lookup_and_vote_actions(self):
        self.vote_id = None
        response = self.get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["voted_proposal_id"])
        self.assertContains(response, f'action="/planes/{PLAN_ID}/propuestas/47/votar/"')
        self.assertNotContains(response, "Quitar voto")
        self.assert_http_count(total=9, vote_calls=1)

    def test_selected_proposal_skips_lookup_even_when_plan_is_inactive(self):
        self.confirm()
        self.plan["status"] = False
        response = self.get_detail()
        self.assertEqual(response.status_code, 200)
        self.voted.assert_not_called()
        self.assert_standings(response)
        self.assert_http_count(total=8, vote_calls=0)

    def test_reopened_plan_resumes_vote_query(self):
        self.confirm()
        self.assertEqual(self.get_detail().status_code, 200)
        self.voted.assert_not_called()
        self.requests.clear()
        self.plan.update(selected_proposal_id=None, confirmed_at=None, scheduled_at=None)
        response = self.get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["voted_proposal_id"], 47)
        self.assertContains(response, "Quitar voto")
        self.assert_http_count(total=9, vote_calls=1)

    def test_vote_service_failure_still_reports_unavailable_for_open_plan(self):
        self.vote_unavailable = True
        response = self.get_detail()
        self.assertEqual(response.status_code, 503)
        self.voted.assert_called_once_with("synthetic-user-jwt", plan_id=UUID(PLAN_ID))
        self.assert_http_count(total=9, vote_calls=1)
