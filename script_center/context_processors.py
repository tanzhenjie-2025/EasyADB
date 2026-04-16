from django.conf import settings

def global_settings(request):
    """
    把 settings 中的特定变量暴露给所有模板
    """
    return {
        'SHOW_BUILTIN_SCRIPTS': getattr(settings, 'SHOW_BUILTIN_SCRIPTS', True),
    }