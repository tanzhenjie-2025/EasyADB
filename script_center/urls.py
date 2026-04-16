# script_center/urls.py
from django.urls import path
from . import views

# 命名空间必须和模板/视图中一致（script_center）
app_name = "script_center"

urlpatterns = [
    # 任务列表页（根路径）
    path("", views.TaskListView.as_view(), name="task_list"),
    # 新增任务
    path("add/", views.TaskAddView.as_view(), name="task_add"),
    # 编辑任务
    path("edit/<int:task_id>/", views.TaskEditView.as_view(), name="task_edit"),
    # 删除任务
    path("delete/<int:task_id>/", views.TaskDeleteView.as_view(), name="task_delete"),
    # 执行任务
    path("execute/", views.ExecuteTaskView.as_view(), name="execute_task"),
    # 停止任务
    path("stop/<int:log_id>/", views.StopTaskView.as_view(), name="stop_task"),

    # ====================== 修正：Airtest 相关路由（顺序很重要！） ======================
    # 1. 最具体的 clear 路由放最前面
    path('log/<int:log_id>/airtest-images/clear/', views.ClearAirtestLogImagesView.as_view(),
         name='clear_airtest_images'),
    # 2. 然后是获取图片列表
    path('log/<int:log_id>/airtest-images/', views.AirtestLogImagesView.as_view(), name='airtest_images'),
    # 3. 最后才是带变量的图片文件名路由
    path('log/<int:log_id>/airtest-images/<str:image_name>/', views.ServeAirtestLogImageView.as_view(),
         name='serve_airtest_image'),

    # ====================== 其他日志路由放在后面 ======================
    # 日志详情
    path("log/<int:log_id>/", views.LogDetailView.as_view(), name="log_detail"),
    # 日志状态（AJAX）
    path("log/status/<int:log_id>/", views.LogStatusView.as_view(), name="log_status"),
    # 管理日志
    path("management_log/", views.TaskManagementLogView.as_view(), name="management_log"),
    # === 新增：内置脚本库路由 ===
    path("builtin/", views.BuiltinScriptListView.as_view(), name="builtin_list"),
    path("builtin/<int:script_id>/", views.BuiltinScriptDetailView.as_view(), name="builtin_detail"),
]