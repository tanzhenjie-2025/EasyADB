from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/script_log/(?P<log_id>\d+)/$', consumers.ScriptLogConsumer.as_asgi()),
]