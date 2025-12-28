from django.urls import path
from . import views

app_name = 'task_orchestration'

urlpatterns = [
    path('list/', views.OrchestrationListView.as_view(), name='list'),
    path('create/', views.OrchestrationCreateView.as_view(), name='create'),
    path('edit_steps/<int:task_id>/', views.StepEditView.as_view(), name='edit_steps'),
    path('execute_api/<int:task_id>/', views.ExecuteOrchestrationAPIView.as_view(), name='execute_api'),
    path('execute/', views.OrchestrationExecuteView.as_view(), name='execute_orchestration'),
    path('stop/<int:log_id>/', views.StopOrchestrationView.as_view(), name='stop_orchestration'),
    path('step/delete/<int:step_id>/', views.StepDeleteView.as_view(), name='delete_step'),
    path('log/<int:log_id>/', views.OrchestrationLogDetailView.as_view(), name='log_detail'),
    path('log/status/<int:log_id>/', views.OrchestrationLogStatusView.as_view(), name='log_status'),
    path('clone/<int:task_id>/', views.OrchestrationCloneView.as_view(), name='clone'),

    path('',views.send_sms,name='send_sms_index'),
    path('send/', views.send_sms_view, name='send_sms'),  # 普通视图
    path('task-result/', views.check_task_result, name='check_task'),  # 查询任务结果
    path('delete/<int:task_id>/', views.OrchestrationDeleteView.as_view(), name='delete'),  # 删除任务
    path('management_logs/global/', views.OrchestrationGlobalManagementLogView.as_view(),
         name='global_management_logs'),  # 全局日志
]