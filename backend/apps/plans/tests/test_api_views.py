"""Contrato del endpoint móvil: JWT de Supabase, sin lógica propia."""
import json
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.groups.services.exceptions import GroupNotFound
from apps.plans.services.exceptions import PlanNotFound
from apps.plans.services.place_types import RecommendationError
from apps.plans.tests.test_services import PLAN_ID
from apps.users.services.exceptions import SessionExpired

URL = f"/movil/planes/{PLAN_ID}/recomendaciones/"
PATCH = "apps.plans.api_views.plan_recommendation_service.generate_recommendations"
SNAPSHOT = {"status": "ready", "items": [], "error_code": None}


class MobileRecommendationsApiTests(SimpleTestCase):
    def test_requires_bearer_token(self):
        response = self.client.post(URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "unauthorized"})

    def test_rejects_other_authorization_schemes(self):
        response = self.client.post(URL, HTTP_AUTHORIZATION="Basic private-jwt")
        self.assertEqual(response.status_code, 401)

    @patch(PATCH)
    def test_generates_with_the_user_token(self, generate):
        generate.return_value = SNAPSHOT
        response = self.client.post(URL, HTTP_AUTHORIZATION="Bearer private-jwt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), SNAPSHOT)
        generate.assert_called_once_with("private-jwt", PLAN_ID, replace=False)

    @patch(PATCH)
    def test_replace_only_when_explicitly_requested(self, generate):
        generate.return_value = SNAPSHOT
        response = self.client.post(
            URL,
            data=json.dumps({"replace": True}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer private-jwt",
        )
        self.assertEqual(response.status_code, 200)
        generate.assert_called_once_with("private-jwt", PLAN_ID, replace=True)

    @patch(PATCH)
    def test_invalid_json_body_does_not_replace(self, generate):
        generate.return_value = SNAPSHOT
        response = self.client.post(
            URL, data="not-json", content_type="application/json",
            HTTP_AUTHORIZATION="Bearer private-jwt",
        )
        self.assertEqual(response.status_code, 200)
        generate.assert_called_once_with("private-jwt", PLAN_ID, replace=False)

    @patch(PATCH)
    def test_unknown_plan_is_404(self, generate):
        generate.side_effect = PlanNotFound()
        response = self.client.post(URL, HTTP_AUTHORIZATION="Bearer private-jwt")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "not_found"})

    @patch(PATCH)
    def test_unknown_group_is_404(self, generate):
        generate.side_effect = GroupNotFound()
        response = self.client.post(URL, HTTP_AUTHORIZATION="Bearer private-jwt")
        self.assertEqual(response.status_code, 404)

    @patch(PATCH)
    def test_expired_session_is_401(self, generate):
        generate.side_effect = SessionExpired()
        response = self.client.post(URL, HTTP_AUTHORIZATION="Bearer private-jwt")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "unauthorized"})

    @patch(PATCH)
    def test_provider_failure_is_a_stable_503_without_details(self, generate):
        generate.side_effect = RecommendationError("http_error")
        response = self.client.post(URL, HTTP_AUTHORIZATION="Bearer private-jwt")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "error_code": "storage_unavailable", "items": []},
        )

    def test_only_post_is_allowed(self):
        response = self.client.get(URL, HTTP_AUTHORIZATION="Bearer private-jwt")
        self.assertEqual(response.status_code, 405)
