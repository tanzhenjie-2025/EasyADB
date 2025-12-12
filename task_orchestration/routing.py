from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/orchestration_log/(?P<log_id>\d+)/$', consumers.OrchestrationLogConsumer.as_asgi()),
]