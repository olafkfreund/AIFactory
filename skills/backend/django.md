# django

> Source: curated best practices | 2026

---

# Django - Batteries-included web + REST with DRF

This skill equips the coder to build production Django 5.x services (Python 3.11+), typically with Django REST Framework for JSON APIs. It enforces a settings split by environment, fat models / thin views with a service layer, `select_related`/`prefetch_related` to kill N+1 queries, DRF serializers for validation, token/session auth with permission classes, migrations for every schema change, and `APITestCase`/`pytest-django` tests against a real test database.

## When to Activate

Use when building with Django:
- Building Django apps, admin sites, or DRF JSON APIs
- Files importing `django`, `rest_framework`, `models.Model`, or `serializers.ModelSerializer`
- Adding models, migrations, views/viewsets, URLs, or auth/permissions
- ORM query optimization, signals, or management commands

## Patterns and Best Practices

Project layout with a settings package:

```
config/
  settings/base.py prod.py dev.py
  urls.py wsgi.py asgi.py
apps/
  users/
    models.py serializers.py views.py urls.py services.py
    migrations/
    tests.py
```

Environment-driven settings (no secrets in source):

```python
# config/settings/base.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.environ["DB_NAME"],
    "USER": os.environ["DB_USER"],
    "PASSWORD": os.environ["DB_PASSWORD"],
    "HOST": os.environ.get("DB_HOST", "localhost"),
    "CONN_MAX_AGE": 60,
}}
```

Models with constraints and indexes at the DB level:

```python
# apps/users/models.py
from django.conf import settings
from django.db import models

class Article(models.Model):
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name="articles")
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    body = models.TextField()
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["author", "published_at"])]
        constraints = [
            models.CheckConstraint(check=models.Q(title__gt=""), name="title_not_empty"),
        ]

    def __str__(self) -> str:
        return self.title
```

Service layer keeps business logic out of views:

```python
# apps/users/services.py
from django.db import transaction
from django.utils.text import slugify
from .models import Article

@transaction.atomic
def create_article(*, author, title: str, body: str) -> Article:
    return Article.objects.create(author=author, title=title, slug=slugify(title), body=body)
```

DRF serializer for validation and shaping output:

```python
# apps/users/serializers.py
from rest_framework import serializers
from .models import Article

class ArticleSerializer(serializers.ModelSerializer):
    author = serializers.ReadOnlyField(source="author.username")

    class Meta:
        model = Article
        fields = ["id", "author", "title", "slug", "body", "published_at"]
        read_only_fields = ["slug", "published_at"]

    def validate_title(self, value: str) -> str:
        if len(value) < 3:
            raise serializers.ValidationError("title too short")
        return value
```

ViewSet with permissions and query optimization:

```python
# apps/users/views.py
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from .models import Article
from .serializers import ArticleSerializer
from .services import create_article

class ArticleViewSet(viewsets.ModelViewSet):
    serializer_class = ArticleSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        # select_related avoids N+1 on author lookups
        return Article.objects.select_related("author").all()

    def perform_create(self, serializer):
        article = create_article(author=self.request.user, **serializer.validated_data)
        serializer.instance = article
```

URL wiring with a router:

```python
# apps/users/urls.py
from rest_framework.routers import DefaultRouter
from .views import ArticleViewSet

router = DefaultRouter()
router.register("articles", ArticleViewSet, basename="article")
urlpatterns = router.urls
```

Tests against the DRF test client:

```python
# apps/users/tests.py
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

class ArticleTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", password="pw12345678")

    def test_create_requires_auth(self):
        resp = self.client.post("/api/articles/", {"title": "Hello", "body": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_create_ok(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/articles/", {"title": "Hello", "body": "x"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["author"], "bob")
```

Always generate migrations for schema changes: `python manage.py makemigrations && python manage.py migrate`.

## Anti-patterns

- N+1 queries: iterating a queryset and touching related objects without `select_related`/`prefetch_related`.
- Business logic in views or serializers instead of a service layer / model methods.
- Editing generated migrations by hand or committing schema changes without a migration.
- `objects.all()` then filtering in Python — filter in the ORM so the database does the work.
- Hardcoding `SECRET_KEY`, `DEBUG=True`, or credentials in `settings.py`.
- Fat `settings.py` with no environment split; running the same settings in dev and prod.
- Overusing signals for logic that belongs in an explicit service call (hidden control flow).
