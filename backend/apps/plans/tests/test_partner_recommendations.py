import json
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.plans.services import partner_recommendations
from apps.plans.services.place_types import RecommendationError
from apps.plans.tests.test_services import PLAN_ID


@override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test",
                   PARTNER_BURGER_URL="https://burger.example/", PARTNER_STREAMLY_URL="https://movies.example/")
class PartnerRecommendationsTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch("apps.plans.services.partner_recommendations.httpx.post")
    def test_gemini_results_are_validated_and_cached(self, post):
        post.return_value = Mock(json=lambda: {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": json.dumps({"apps": ["burger", "movies"]})}]}}]})
        proposal = {"tittle": "Peli y hamburguesas", "description": ""}
        self.assertEqual(partner_recommendations.classify(proposal), ["burger", "movies"])
        self.assertEqual(partner_recommendations.classify(proposal), ["burger", "movies"])
        post.assert_called_once()

    @patch("apps.plans.services.partner_recommendations.httpx.post")
    def test_gemini_cannot_inject_other_apps(self, post):
        post.return_value = Mock(json=lambda: {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": '{"apps":["unexpected"]}'}]}}]})
        with self.assertRaises(RecommendationError):
            partner_recommendations.classify({"tittle": "Otra actividad"})

    @patch("apps.plans.services.partner_recommendations.httpx.post")
    def test_classification_includes_plan_context(self, post):
        post.return_value = Mock(json=lambda: {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": '{"apps":["movies"]}'}]}}]})
        partner_recommendations.classify({"tittle": "En casa"}, {"name": "Noche de películas"})
        payload = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        activity = json.loads(payload)
        self.assertEqual(activity["plan"]["title"], "Noche de películas")
        self.assertEqual(activity["propuesta"]["title"], "En casa")

    @patch("apps.users.services.session_service.get_access_token", return_value="token")
    @patch("apps.users.services.session_service.current_user", return_value={"id": "user", "username": "Ana"})
    @patch("apps.plans.partner_views.plan_recommendation_service._proposal", return_value={"tittle": "Pelis"})
    @patch("apps.plans.partner_views.plan_recommendation_service._authorized_plan", return_value={"status": True, "selected_proposal_id": 7, "confirmed_at": "2026-01-01"})
    @patch("apps.plans.partner_views.partner_recommendations.classify", return_value=["movies"])
    def test_confirmed_plan_only_returns_matching_link(self, classify, plan, proposal, current, token):
        response = self.client.post(reverse("plans:partner_apps", args=[PLAN_ID]))
        self.assertEqual(response.json(), {"apps": [{"kind": "movies", "url": "https://movies.example/"}]})

    @patch("apps.users.services.session_service.get_access_token", return_value="token")
    @patch("apps.users.services.session_service.current_user", return_value={"id": "user", "username": "Ana"})
    @patch("apps.plans.partner_views.plan_recommendation_service._authorized_plan", return_value={"status": True, "selected_proposal_id": None})
    @patch("apps.plans.partner_views.partner_recommendations.classify")
    def test_unconfirmed_plan_is_not_classified(self, classify, plan, current, token):
        response = self.client.post(reverse("plans:partner_apps", args=[PLAN_ID]))
        self.assertEqual(response.json(), {"apps": []})
        classify.assert_not_called()
