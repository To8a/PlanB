"""Delimita los recursos HTTP de Data API al procesamiento de cada request."""
from .services.data_api_transport import request_connections


class DataAPIConnectionsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Django renderiza TemplateResponse dentro de get_response. En respuestas
        # streaming, el contenido posterior queda fuera del scope y no reutiliza.
        with request_connections():
            return self.get_response(request)
