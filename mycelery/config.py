
from django.conf import settings

broker_url = getattr(settings, 'CELERY_BROKER_URL')
result_backend = getattr(settings, 'CELERY_RESULT_BACKEND')
