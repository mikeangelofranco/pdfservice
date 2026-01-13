from django.urls import path

from . import views


urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('payment/', views.payment, name='payment'),
    path('users/', views.user_list, name='user_list'),
    path('reset-link/', views.admin_reset_link, name='admin_reset_link'),
    path('reset/', views.reset_via_link, name='reset_via_link'),
    path('forgot-password/', views.forgot_password, name='forgot_password'),
    path('signup/', views.signup, name='signup'),
    path('tool/<slug:slug>/', views.use_tool, name='use_tool'),
    path('tool/pdf-lock/process/', views.pdf_lock, name='pdf_lock'),
    path('tool/pdf-unlock/process/', views.pdf_unlock, name='pdf_unlock'),
    path('tool/pdf-merge/process/', views.pdf_merge, name='pdf_merge'),
    path('tool/pdf-split/process/', views.pdf_split, name='pdf_split'),
    path('tool/pdf-to-image/process/', views.pdf_to_image, name='pdf_to_image'),
    path('tool/fillable-form-converter/process/', views.fillable_form_convert, name='fillable_form_convert'),
    path('tool/image-to-pdf/process/', views.image_to_pdf, name='image_to_pdf'),
    path('tool/remove-pages/process/', views.remove_pages, name='remove_pages'),
    path('tool/remove-pages/inspect/', views.remove_pages_inspect, name='remove_pages_inspect'),
    path('tool/redact-text/process/', views.redact_text, name='redact_text'),
    path('tool/redact-text/inspect/', views.redact_text_inspect, name='redact_text_inspect'),
    path('api/pdfservice/increasecredits', views.increase_credits, name='increase_credits'),
]
