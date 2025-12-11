"""
URL configuration for EasyADB project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

# easy_adb/urls.py
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    # 挂载ADB管理应用的URL，指定命名空间（关键）
    path('adb/', include(('adb_manager.urls', 'adb_manager'), namespace='adb_manager')),
    # 新增脚本中心路由
    path('script/', include('script_center.urls')),

    path('task_orchestration/', include('task_orchestration.urls')),
path('scheduler/', include('task_scheduler.urls')),
]
