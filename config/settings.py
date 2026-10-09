import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env manually if available
env_path = BASE_DIR / '.env'
if env_path.exists():
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

from django.core.exceptions import ImproperlyConfigured

DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-dev-key-spotter-fuel-planner-change-in-prod' if DEBUG else '')

if not DEBUG and (not SECRET_KEY or 'django-insecure' in SECRET_KEY):
    raise ImproperlyConfigured("Production settings error: A secure SECRET_KEY environment variable is mandatory when DEBUG=False.")

raw_allowed_hosts = os.environ.get('ALLOWED_HOSTS', '127.0.0.1,localhost')
ALLOWED_HOSTS = [h.strip() for h in raw_allowed_hosts.split(',') if h.strip()]
if 'testserver' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('testserver')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'fuel_planner',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
    'EXCEPTION_HANDLER': 'fuel_planner.views.custom_exception_handler',
}

# Application Fuel Constraints & HeiGIT/ORS Settings
HEIGIT_API_KEY = os.environ.get('HEIGIT_API_KEY', '')
TANK_CAPACITY_GALLONS = float(os.environ.get('TANK_CAPACITY_GALLONS', '50.0'))
FUEL_ECONOMY_MPG = float(os.environ.get('FUEL_ECONOMY_MPG', '10.0'))
DEFAULT_STARTING_FUEL_GALLONS = float(os.environ.get('DEFAULT_STARTING_FUEL_GALLONS', '50.0'))
MAX_OFF_ROUTE_DISTANCE_MILES = float(os.environ.get('MAX_OFF_ROUTE_DISTANCE_MILES', '5.0'))
