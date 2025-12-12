# adb_manager/urls.py

from django.urls import path
from . import views

app_name = "adb_manager"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.AddDeviceView.as_view(), name="add_device"),
    path("edit/<int:device_id>/", views.EditDeviceView.as_view(), name="edit_device"),
    path("delete/<int:device_id>/", views.DeleteDeviceView.as_view(), name="delete_device"),
    path("connect/", views.ADBDeviceConnectView.as_view(), name="connect"),
    path("disconnect/", views.ADBDeviceDisconnectView.as_view(), name="disconnect"),
    path("refresh-all/", views.RefreshAllDevicesView.as_view(), name="refresh_all"),
    path("connect-all/", views.ConnectAllDevicesView.as_view(), name="connect_all"),
    path("disconnect-all/", views.DisconnectAllDevicesView.as_view(), name="disconnect_all"),
    path("status/", views.ADBDeviceStatusView.as_view(), name="device_status"),
    path("csrf-token/", views.CSRFTokenView.as_view(), name="csrf_token"),
    path("list-devices/", views.ADBDevicesListView.as_view(), name="list_devices"),
    path("detail-device/", views.ADBDeviceDetailView.as_view(), name="detail_device"),
]