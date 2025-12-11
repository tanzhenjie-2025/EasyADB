# task_scheduler/urls.py
from django.urls import path
from . import views

app_name = "task_scheduler"

urlpatterns = [
    path('', views.ScheduleTaskListView.as_view(), name='list'),
    path('create/', views.ScheduleTaskCreateView.as_view(), name='create'),
    path('edit/<int:schedule_id>/', views.ScheduleTaskEditView.as_view(), name='edit'),
    path('detail/<int:schedule_id>/', views.ScheduleTaskDetailView.as_view(), name='detail'),
    path('toggle/<int:schedule_id>/', views.ScheduleTaskToggleView.as_view(), name='toggle'),
    path('execute/<int:schedule_id>/', views.ExecuteScheduledTaskView.as_view(), name='execute'),
]