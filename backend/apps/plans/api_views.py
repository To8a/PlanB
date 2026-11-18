"""API JSON para la app móvil.

Sólo expone operaciones que ya existen como servicio: no duplica prompts,
ranking ni reglas de negocio. La app envía su JWT de Supabase y el servicio
lo usa para leer con RLS, igual que la web.
"""
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.groups.services.exceptions import GroupNotFound, GroupUnavailable, InvalidGroup
from apps.users.services.exceptions import SessionExpired
from .services import plan_recommendation_service
from .services.exceptions import PlanNotFound, PlanUnavailable
from .services.place_types import RecommendationError


def _bearer(request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _wants_replace(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("replace") is True


@csrf_exempt
@require_POST
def recommendations(request, plan_id):
    """Genera (o reutiliza) las recomendaciones del plan confirmado.

    Sin ``replace`` sólo reserva trabajo si el snapshot está vencido o ausente,
    así que abrir la tarjeta en la app no repite llamadas a Gemini/Geoapify.
    """
    token = _bearer(request)
    if not token:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        result = plan_recommendation_service.generate_recommendations(
            token, str(plan_id), replace=_wants_replace(request),
        )
    except (PlanNotFound, GroupNotFound):
        return JsonResponse({"error": "not_found"}, status=404)
    except SessionExpired:
        return JsonResponse({"error": "unauthorized"}, status=401)
    except (PlanUnavailable, GroupUnavailable, InvalidGroup, RecommendationError):
        return JsonResponse(
            {"status": "unavailable", "error_code": "storage_unavailable", "items": []},
            status=503,
        )
    return JsonResponse(result)
