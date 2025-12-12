# EasyADB/asgi.py
"""
ASGI config for EasyADB project.
"""
import os

# 第一步：先设置Django环境变量（必须最先执行）
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'EasyADB.settings')

# 第二步：加载Django ASGI应用（初始化settings）
from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()  # 先初始化Django，再导入Channels相关

# 第三步：导入Channels路由（此时settings已加载，models可正常导入）
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import script_center.routing
# 注意：如果task_orchestration.routing不存在/未使用，注释掉这行！
import task_orchestration.routing

# 第四步：构建ASGI应用
application = ProtocolTypeRouter({
    "http": django_asgi_app,  # 已初始化的Django HTTP应用
    "websocket": AuthMiddlewareStack(
        URLRouter(
            script_center.routing.websocket_urlpatterns
            # 若task_orchestration无websocket路由，删除下面的拼接
            + task_orchestration.routing.websocket_urlpatterns
        )
    ),
})