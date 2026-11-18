"""Recomendaciones externas solo para integrantes y planes confirmados."""
from urllib.parse import urlsplit

from django.conf import settings
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST

from apps.groups.services.exceptions import GroupNotFound, GroupUnavailable, InvalidGroup
from apps.users.decorators import private_page, supabase_login_required
from apps.users.services import session_service
from .services import partner_recommendations, plan_recommendation_service
from .services.exceptions import PlanNotFound, PlanUnavailable
from .services.place_types import RecommendationError


def _url(value):
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None
    except (TypeError, ValueError):
        return None


@private_page
@require_POST
@supabase_login_required
def partner_apps(request, plan_id):
    token = session_service.get_access_token(request)
    try:
        plan = plan_recommendation_service._authorized_plan(token, str(plan_id))
        if not plan_recommendation_service._confirmed(plan):
            return JsonResponse({"apps": []})
        proposal = plan_recommendation_service._proposal(token, plan)
        if proposal is None:
            return JsonResponse({"apps": []})
        matches = partner_recommendations.classify(proposal, plan)
        current = plan_recommendation_service._authorized_plan(token, str(plan_id))
        if not plan_recommendation_service._confirmed(current) or current.get("selected_proposal_id") != plan.get("selected_proposal_id"):
            return JsonResponse({"apps": []})
        urls = {"burger": _url(getattr(settings, "PARTNER_BURGER_URL", "")),
                "movies": _url(getattr(settings, "PARTNER_STREAMLY_URL", ""))}
        return JsonResponse({"apps": [{"kind": kind, "url": urls[kind]} for kind in matches if urls[kind]]})
    except (PlanNotFound, GroupNotFound):
        raise Http404("No encontramos ese plan.") from None
    except (PlanUnavailable, GroupUnavailable, InvalidGroup, RecommendationError):
        return JsonResponse({"apps": []}, status=503)
