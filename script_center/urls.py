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
    # 执行任务（关键：name统一为execute_task，匹配前端模板）
    path("execute/", views.ExecuteTaskView.as_view(), name="execute_task"),
    # 停止任务
    path("stop/<int:log_id>/", views.StopTaskView.as_view(), name="stop_task"),
    # 日志详情
    path("log/<int:log_id>/", views.LogDetailView.as_view(), name="log_detail"),
    # 日志状态（AJAX）
    path("log/status/<int:log_id>/", views.LogStatusView.as_view(), name="log_status"),
    # 管理日志
    path("management_log/", views.TaskManagementLogView.as_view(), name="management_log"),
]