from django.urls import include, path
from django.views.generic import RedirectView

from apps.plans import api_views

urlpatterns = [
    path("movil/planes/<uuid:plan_id>/recomendaciones/", api_views.recommendations,
         name="mobile_recommendations"),
    path("notificaciones/", include("apps.notifications.urls")),
    path("grupos/", include("apps.groups.urls")),
    path("planes/", include("apps.plans.urls")),
    path("", RedirectView.as_view(pattern_name="users:dashboard", permanent=False)),
    path("", include("apps.users.urls")),
]
