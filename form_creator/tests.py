import json
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pages.models import UserProfile

from .models import FormField, FormTemplate


class FormCreatorViewsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="creator",
            email="creator@example.com",
            password="test-password-123",
        )
        UserProfile.objects.create(
            user=self.user,
            date_of_birth="1990-01-01",
            can_use_advanced_tools=True,
        )

    def test_advanced_access_required(self):
        User = get_user_model()
        blocked = User.objects.create_user(
            username="blocked",
            email="blocked@example.com",
            password="test-password-123",
        )
        UserProfile.objects.create(
            user=blocked,
            date_of_birth="1990-01-01",
            can_use_advanced_tools=False,
        )
        self.client.login(username="blocked", password="test-password-123")
        response = self.client.get(reverse("form_creator"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "advanced tool", html=False)

    def test_save_rejects_duplicate_keys(self):
        template = FormTemplate.objects.create(
            owner=self.user,
            name="Test template",
            page_size="A4",
            default_output_mode="FLATTENED",
        )
        payload = {
            "name": "Test template",
            "page_size": "A4",
            "default_output_mode": "FLATTENED",
            "margins": {"top": 72, "right": 48, "bottom": 56, "left": 48},
            "fields": [
                {
                    "id": "a",
                    "type": "text",
                    "label": "First",
                    "key": "dup_key",
                    "required": False,
                    "x": 0.1,
                    "y": 0.1,
                    "w": 0.3,
                    "h": 0.05,
                },
                {
                    "id": "b",
                    "type": "text",
                    "label": "Second",
                    "key": "dup_key",
                    "required": False,
                    "x": 0.1,
                    "y": 0.2,
                    "w": 0.3,
                    "h": 0.05,
                },
            ],
        }
        self.client.login(username="creator", password="test-password-123")
        response = self.client.post(
            reverse("form_creator_save", args=[template.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("field_errors", response.json())

    def test_export_returns_pdf(self):
        template = FormTemplate.objects.create(
            owner=self.user,
            name="Export template",
            page_size="LETTER",
            default_output_mode="FLATTENED",
        )
        FormField.objects.create(
            template=template,
            type="text",
            label="Name",
            key="name",
            required=False,
            x=0.1,
            y=0.1,
            w=0.3,
            h=0.05,
            order=0,
        )
        self.client.login(username="creator", password="test-password-123")
        response = self.client.post(
            reverse("form_creator_export", args=[template.id]),
            data={"output_mode": "FLATTENED"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
