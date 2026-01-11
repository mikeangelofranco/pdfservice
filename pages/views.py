import base64
from io import BytesIO
import tempfile
import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.contrib.auth import get_user_model
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from django.urls import reverse
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from decimal import Decimal
import json

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont
import re
import zipfile
import os

from .forms import SignupForm
from .models import ServiceUsage, Purchase

logger = logging.getLogger(__name__)

def home(request):
    services = [
        "PDF Merge",
        "PDF Split",
        "PDF Unlock",
        "PDF to Image",
        "Image to PDF",
        "Remove pages",
        "Redact text",
    ]
    return render(request, "home.html", {"services": services})


def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("dashboard")
    else:
        form = SignupForm()

    return render(request, "registration/signup.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("home")


def _available_credits(user):
    total = user.purchases.filter(status="completed").aggregate(
        total=Coalesce(Sum("credits"), 0)
    )["total"]
    used = ServiceUsage.objects.filter(user=user).count()
    remaining = (total or 0) - used
    return max(remaining, 0)


ADVANCED_TOOL_SLUG = "pdf-form-creator"
FORM_PAGE_WIDTH = 612
FORM_PAGE_HEIGHT = 792
FORM_MARGIN_X = 48
FORM_MARGIN_TOP = 72
FORM_MARGIN_BOTTOM = 56
FORM_SECTION_TITLE_HEIGHT = 20
FORM_LABEL_HEIGHT = 14
FORM_FIELD_HEIGHT = 26
FORM_FIELD_GAP = 16
FORM_SECTION_GAP = 18
FORM_COLUMN_GAP = 18


def _can_use_advanced_tools(user):
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(getattr(profile, "can_use_advanced_tools", False))


def _normalize_form_template(raw_template):
    if not isinstance(raw_template, dict):
        raise ValueError("Template data is invalid.")

    title = (raw_template.get("title") or "Untitled form").strip()
    layout = (raw_template.get("layout") or "single").strip().lower()
    if layout not in ("single", "two-column"):
        layout = "single"

    sections = []
    for section in raw_template.get("sections") or []:
        if not isinstance(section, dict):
            continue
        sec_title = (section.get("title") or "").strip()
        fields = []
        for field in section.get("fields") or []:
            if not isinstance(field, dict):
                continue
            label = (field.get("label") or "").strip()
            ftype = (field.get("type") or "text").strip().lower()
            if ftype not in ("text", "checkbox", "date", "signature"):
                ftype = "text"
            fields.append(
                {
                    "label": label or "Untitled field",
                    "type": ftype,
                }
            )
        if sec_title or fields:
            if not fields:
                fields.append({"label": "Untitled field", "type": "text"})
            sections.append({"title": sec_title, "fields": fields})

    if not sections:
        raise ValueError("Add at least one section with fields.")

    return {"title": title, "layout": layout, "sections": sections}


def _layout_form(template, page_width, page_height):
    placements = []
    y = FORM_MARGIN_TOP
    if template["layout"] == "two-column":
        col_width = (page_width - (FORM_MARGIN_X * 2) - FORM_COLUMN_GAP) / 2
    else:
        col_width = page_width - (FORM_MARGIN_X * 2)

    for section in template["sections"]:
        sec_title = section.get("title")
        if sec_title:
            placements.append(
                {
                    "kind": "section",
                    "title": sec_title,
                    "x": FORM_MARGIN_X,
                    "y": y,
                }
            )
            y += FORM_SECTION_TITLE_HEIGHT

        row_y = y
        col = 0
        for field in section["fields"]:
            if template["layout"] == "two-column":
                x = FORM_MARGIN_X + col * (col_width + FORM_COLUMN_GAP)
                placements.append(
                    {
                        "kind": "field",
                        "field": field,
                        "x": x,
                        "y": row_y,
                        "width": col_width,
                    }
                )
                col += 1
                if col == 2:
                    col = 0
                    row_y += FORM_LABEL_HEIGHT + FORM_FIELD_HEIGHT + FORM_FIELD_GAP
            else:
                x = FORM_MARGIN_X
                placements.append(
                    {
                        "kind": "field",
                        "field": field,
                        "x": x,
                        "y": row_y,
                        "width": col_width,
                    }
                )
                row_y += FORM_LABEL_HEIGHT + FORM_FIELD_HEIGHT + FORM_FIELD_GAP

        if template["layout"] == "two-column" and col != 0:
            row_y += FORM_LABEL_HEIGHT + FORM_FIELD_HEIGHT + FORM_FIELD_GAP

        y = row_y + FORM_SECTION_GAP

        if y > page_height - FORM_MARGIN_BOTTOM:
            raise ValueError("Template is too long for a single page.")

    return placements


def _render_form_background(template):
    img = Image.new("RGB", (FORM_PAGE_WIDTH, FORM_PAGE_HEIGHT), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    title = template.get("title") or "Untitled form"
    draw.text((FORM_MARGIN_X, 28), title, fill="black", font=font)

    placements = _layout_form(template, FORM_PAGE_WIDTH, FORM_PAGE_HEIGHT)
    field_specs = []
    for item in placements:
        if item["kind"] == "section":
            draw.text((item["x"], item["y"]), item["title"], fill="black", font=font)
            continue

        field = item["field"]
        label = field.get("label") or "Untitled field"
        field_type = field.get("type") or "text"
        draw.text((item["x"], item["y"]), label, fill="black", font=font)

        input_y = item["y"] + FORM_LABEL_HEIGHT + 4
        input_x = item["x"]
        input_w = item["width"]
        input_h = FORM_FIELD_HEIGHT

        if field_type == "checkbox":
            input_w = 16
            input_h = 16
            draw.rectangle(
                (input_x, input_y, input_x + input_w, input_y + input_h),
                outline="#222222",
                width=1,
            )
        elif field_type == "signature":
            line_y = input_y + input_h - 4
            draw.line((input_x, line_y, input_x + input_w, line_y), fill="#222222", width=1)
        else:
            draw.rectangle(
                (input_x, input_y, input_x + input_w, input_y + input_h),
                outline="#222222",
                width=1,
            )

        rect = [
            float(input_x),
            float(FORM_PAGE_HEIGHT - (input_y + input_h)),
            float(input_x + input_w),
            float(FORM_PAGE_HEIGHT - input_y),
        ]
        field_specs.append(
            {
                "name": label,
                "type": field_type,
                "rect": rect,
            }
        )

    return img, field_specs


def _add_form_fields(writer, page, fields):
    annotations = page.get("/Annots")
    if annotations is None:
        annotations = ArrayObject()
    elif not isinstance(annotations, ArrayObject):
        annotations = ArrayObject(annotations)

    field_array = ArrayObject()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    for idx, field in enumerate(fields, start=1):
        safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", field["name"]).strip("_")
        field_name = safe_label or f"field_{idx}"
        rect = ArrayObject([FloatObject(val) for val in field["rect"]])

        if field["type"] == "checkbox":
            annot = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Btn"),
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/T"): TextStringObject(field_name),
                    NameObject("/Rect"): rect,
                    NameObject("/V"): NameObject("/Off"),
                    NameObject("/AS"): NameObject("/Off"),
                    NameObject("/Ff"): NumberObject(0),
                    NameObject("/F"): NumberObject(4),
                    NameObject("/MK"): DictionaryObject({NameObject("/CA"): TextStringObject("X")}),
                    NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g"),
                }
            )
        else:
            annot = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/T"): TextStringObject(field_name),
                    NameObject("/Rect"): rect,
                    NameObject("/Ff"): NumberObject(0),
                    NameObject("/V"): TextStringObject(""),
                    NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g"),
                }
            )

        annotations.append(annot)
        field_array.append(annot)

    page[NameObject("/Annots")] = annotations
    form = DictionaryObject(
        {
            NameObject("/Fields"): field_array,
            NameObject("/NeedAppearances"): BooleanObject(True),
            NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g"),
            NameObject("/DR"): DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject({NameObject("/Helv"): font_ref}),
                }
            ),
        }
    )
    writer._root_object.update({NameObject("/AcroForm"): form})


def _build_form_pdf(template, export_mode):
    img, field_specs = _render_form_background(template)
    base_buffer = BytesIO()
    img.save(base_buffer, format="PDF")
    base_buffer.seek(0)

    if export_mode == "fillable":
        reader = PdfReader(base_buffer)
        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        if writer.pages and field_specs:
            _add_form_fields(writer, writer.pages[0], field_specs)
        output = BytesIO()
        writer.write(output)
        output.seek(0)
        return output.read()

    return base_buffer.read()


@login_required
def dashboard(request):
    tools = [
        {"name": "PDF Lock", "description": "Encrypt PDFs and add access passwords for secure sharing.", "slug": "pdf-lock", "icon": "lock"},
        {"name": "PDF Unlock", "description": "Remove passwords from PDFs you have access to.", "slug": "pdf-unlock", "icon": "unlock"},
        {"name": "PDF Merge", "description": "Combine multiple PDFs into a single, clean file.", "slug": "pdf-merge", "icon": "merge"},
        {"name": "PDF Split", "description": "Split PDFs into selected page ranges or individual files.", "slug": "pdf-split", "icon": "split"},
        {"name": "PDF to Image", "description": "Export PDF pages to high-quality images.", "slug": "pdf-to-image", "icon": "pdf-to-image"},
        {"name": "Image to PDF", "description": "Convert images into a single ordered PDF.", "slug": "image-to-pdf", "icon": "image-to-pdf"},
        {"name": "Remove pages", "description": "Delete unwanted pages before sharing.", "slug": "remove-pages", "icon": "remove"},
        {"name": "Redact text", "description": "Permanently remove sensitive text and data.", "slug": "redact-text", "icon": "redact"},
    ]
    balance = _available_credits(request.user)
    return render(
        request,
        "dashboard.html",
        {
            "tools": tools,
            "credit_balance": balance,
        },
    )


@login_required
def form_creator(request):
    if not _can_use_advanced_tools(request.user):
        reason = (
            "PDF Form Creator is an advanced tool. Ask your admin to enable advanced access "
            "for your account."
        )
        return render(
            request,
            "advanced_tool_denied.html",
            {"reason": reason, "credit_balance": _available_credits(request.user)},
        )

    balance = _available_credits(request.user)
    if not request.user.is_superuser and balance <= 0:
        messages.error(
            request,
            "Not enough credits to launch PDF Form Creator. Please top up.",
        )
        return redirect("dashboard")

    if not request.user.is_superuser:
        ServiceUsage.objects.create(user=request.user, tool_slug=ADVANCED_TOOL_SLUG)
        balance = _available_credits(request.user)

    return render(
        request,
        "form_creator.html",
        {"credit_balance": balance},
    )


@login_required
def form_creator_export(request):
    if request.method != "POST":
        return redirect("form_creator")

    if not _can_use_advanced_tools(request.user):
        reason = (
            "PDF Form Creator is an advanced tool. Ask your admin to enable advanced access "
            "for your account."
        )
        return render(
            request,
            "advanced_tool_denied.html",
            {"reason": reason, "credit_balance": _available_credits(request.user)},
        )

    template_json = request.POST.get("template_json") or ""
    export_mode = (request.POST.get("export_mode") or "flattened").strip().lower()
    if export_mode not in ("flattened", "fillable"):
        export_mode = "flattened"

    try:
        raw_template = json.loads(template_json or "{}")
        template = _normalize_form_template(raw_template)
        pdf_bytes = _build_form_pdf(template, export_mode)
    except json.JSONDecodeError:
        error = "Template data is invalid. Please rebuild the form and try again."
    except ValueError as exc:
        error = str(exc)
    except Exception:
        logger.exception("form_creator_export failed")
        error = "Unable to generate PDF. Please review the template and try again."
    else:
        safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", template["title"]).strip("_") or "pdf_form"
        filename = f"{safe_title}.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    return render(
        request,
        "form_creator.html",
        {
            "credit_balance": _available_credits(request.user),
            "form_error": error,
            "template_json": template_json,
            "export_mode": export_mode,
        },
    )


@login_required
def payment(request):
    plans = [
        {"amount": 99, "credits": 6},
        {"amount": 200, "credits": 13},
        {"amount": 400, "credits": 29},
        {"amount": 800, "credits": 64},
        {"amount": 1600, "credits": 140},
    ]
    return render(
        request,
        "payment.html",
        {
            "plans": plans,
            "credit_balance": _available_credits(request.user),
        },
    )


@login_required
def user_list(request):
    if not request.user.is_superuser:
        return redirect("dashboard")

    query = (request.GET.get("q") or "").strip()
    users = User.objects.all().order_by("email").prefetch_related("purchases")
    if query:
        users = users.filter(
            Q(email__icontains=query)
            | Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(purchases__description__icontains=query)
        ).distinct()

    paginator = Paginator(users, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    rows = []
    for u in page_obj:
        available = _available_credits(u)
        last_payment_obj = u.purchases.filter(status="completed").order_by("-created_at").first()
        last_payment = last_payment_obj.created_at if last_payment_obj else None
        payments_meta = [
            {"date": p.created_at, "amount": p.amount, "status": p.status}
            for p in u.purchases.all().order_by("-created_at")
        ]
        rows.append(
            {
                "user": u,
                "available": available,
                "last_payment": last_payment,
                "payments": payments_meta,
            }
        )

    return render(
        request,
        "users.html",
        {
            "page_obj": page_obj,
            "rows": rows,
            "query": query,
            "credit_balance": _available_credits(request.user),
        },
    )


@login_required
def admin_reset_link(request):
    if not request.user.is_superuser:
        return redirect("dashboard")

    link = None
    error = None
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        try:
            target = User.objects.get(email=email)
            token = signer.sign(target.email)
            link = request.build_absolute_uri(f"{reverse('reset_via_link')}?token={token}")
        except User.DoesNotExist:
            error = "User not found."

    return render(
        request,
        "admin_reset_link.html",
        {"link": link, "error": error, "credit_balance": _available_credits(request.user)},
    )


def reset_via_link(request):
    token = request.GET.get("token") or request.POST.get("token")
    email = None
    error = None
    if token:
        try:
            email = signer.unsign(token, max_age=60 * 60 * 24)
        except SignatureExpired:
            error = "Reset link has expired."
        except BadSignature:
            error = "Invalid reset link."
    else:
        error = "Missing reset link."

    if request.method == "POST" and not error and email:
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            error = "User not found."
        password = request.POST.get("password") or ""
        confirm = request.POST.get("confirm") or ""
        if not error:
            if not password:
                error = "Password is required."
            elif password != confirm:
                error = "Passwords do not match."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            else:
                user.set_password(password)
                user.save()
                messages.success(request, "Password updated. You can now log in.")
                return redirect("login")

    return render(
        request,
        "reset_via_link.html",
        {"token": token, "email": email, "error": error},
    )


def forgot_password(request):
    message = None
    error = None
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        dob = (request.POST.get("dob") or "").strip()
        try:
            User.objects.get(email=email)
            message = (
                "We found your account. Please email paymentsupport@plughub-ims.com "
                "with your email and birthdate for verification. Include the birthdate you entered: "
                f"{dob if dob else 'not provided'}."
            )
        except User.DoesNotExist:
            error = "Invalid email address. Please consider signing up instead."

    return render(
        request,
        "forgot_password.html",
        {"message": message, "error": error},
    )


@login_required
def use_tool(request, slug):
    if request.method != "POST":
        return redirect("dashboard")

    balance = _available_credits(request.user)
    display_name = slug.replace("-", " ").title()

    if balance <= 0:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)
        messages.error(request, "No credits remaining. Add credits to continue.")
        return redirect("dashboard")

    ServiceUsage.objects.create(user=request.user, tool_slug=slug)
    remaining = max(balance - 1, 0)
    success_message = f"{display_name} queued. 1 credit deducted. Remaining: {remaining}."

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"status": "ok", "message": success_message, "remaining": remaining})

    messages.success(request, success_message)
    return redirect("dashboard")


@login_required
def pdf_lock(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    password = request.POST.get("password") or "locked"

    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    # Deduct credit before processing, per requested flow.
    ServiceUsage.objects.create(user=request.user, tool_slug="pdf-lock")
    remaining = _available_credits(request.user)

    try:
        reader = PdfReader(uploaded)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(password)

        output = BytesIO()
        writer.write(output)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to lock the PDF. Please check the file and try again."}, status=400)

    return JsonResponse(
        {
            "status": "ok",
            "message": "PDF locked successfully. 1 credit deducted.",
            "remaining": remaining,
            "file_name": f"locked_{uploaded.name}",
            "file_data": encoded,
        }
    )


@login_required
def pdf_unlock(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    password = request.POST.get("password") or ""

    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)
    if not password:
        return JsonResponse({"status": "error", "message": "Password is required to unlock the PDF."}, status=400)

    try:
        reader = PdfReader(uploaded)
        if reader.is_encrypted:
            # decrypt returns 0/False on failure
            success = False
            for pwd in (password, password.encode("utf-8"), b""):
                try:
                    res = reader.decrypt(pwd)
                except Exception:
                    res = 0
                if res not in (0, False, None):
                    success = True
                    break
            if not success:
                return JsonResponse({"status": "error", "message": "Incorrect password. Please try again."}, status=400)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to unlock the PDF. Please check the file and password."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="pdf-unlock")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "PDF unlocked successfully. 1 credit deducted.",
            "remaining": remaining,
            "file_name": f"unlocked_{uploaded.name}",
            "file_data": encoded,
        }
    )


@login_required
def pdf_merge(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    files = request.FILES.getlist("pdfs")
    if not files:
        return JsonResponse({"status": "error", "message": "Upload at least two PDFs to merge."}, status=400)
    if len(files) < 2:
        return JsonResponse({"status": "error", "message": "Add two or more PDFs to merge."}, status=400)

    try:
        writer = PdfWriter()
        for f in files:
            reader = PdfReader(f)
            if reader.is_encrypted:
                # Encrypted PDFs are not merged here; user must unlock first.
                return JsonResponse({"status": "error", "message": f"{f.name} is encrypted. Unlock it first."}, status=400)
            for page in reader.pages:
                writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to merge PDFs. Please check the files and try again."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="pdf-merge")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "PDFs merged successfully. 1 credit deducted.",
            "remaining": remaining,
            "file_name": "merged.pdf",
            "file_data": encoded,
        }
    )


@login_required
def pdf_split(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    try:
        reader = PdfReader(uploaded)
        num_pages = len(reader.pages)
        if num_pages <= 1:
            return JsonResponse({"status": "error", "message": "Only one page detected. Operation cancelled."}, status=400)

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
            for idx, page in enumerate(reader.pages, start=1):
                writer = PdfWriter()
                writer.add_page(page)
                page_bytes = BytesIO()
                writer.write(page_bytes)
                page_bytes.seek(0)
                zipf.writestr(f"page_{idx}.pdf", page_bytes.read())

        zip_buffer.seek(0)
        encoded = base64.b64encode(zip_buffer.read()).decode("ascii")
    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to split PDF. Please check the file and try again."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="pdf-split")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "PDF split successfully. 1 credit deducted.",
            "remaining": remaining,
            "file_name": "split_pages.zip",
            "file_data": encoded,
        }
    )


@login_required
def pdf_to_image(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    try:
        uploaded.seek(0)
        data = uploaded.read()
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as tmp:
                tmp.write(data)
                tmp.flush()

            doc = pdfium.PdfDocument(tmp_path)
            try:
                encrypted = doc.needs_password()
            except AttributeError:
                encrypted = False
            if encrypted:
                return JsonResponse({"status": "error", "message": "PDF is encrypted. Unlock it first."}, status=400)

            page_indices = list(range(len(doc)))
            if not page_indices:
                return JsonResponse({"status": "error", "message": "No pages found to convert."}, status=400)

            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                for idx in page_indices:
                    page = doc[idx]
                    bitmap = page.render(scale=2)
                    image = bitmap.to_pil()
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    img_bytes = BytesIO()
                    image.save(img_bytes, format="PNG")
                    img_bytes.seek(0)
                    zipf.writestr(f"page_{idx}.png", img_bytes.read())

            zip_buffer.seek(0)
            encoded = base64.b64encode(zip_buffer.read()).decode("ascii")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        logging.exception("pdf_to_image failed")
        return JsonResponse({"status": "error", "message": "Unable to convert PDF to images. Please check the file and try again."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="pdf-to-image")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "PDF converted to images. 1 credit deducted.",
            "remaining": remaining,
            "file_name": "pdf_images.zip",
            "file_data": encoded,
        }
    )


@login_required
def image_to_pdf(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    images = request.FILES.getlist("images")
    if not images:
        return JsonResponse({"status": "error", "message": "Upload at least one image to convert."}, status=400)

    try:
        pil_images = []
        for f in images:
            img = Image.open(f).convert("RGB")
            pil_images.append(img)
        if not pil_images:
            return JsonResponse({"status": "error", "message": "No images found to convert."}, status=400)

        output = BytesIO()
        pil_images[0].save(output, format="PDF", save_all=True, append_images=pil_images[1:])
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to convert images to PDF. Please check the files and try again."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="image-to-pdf")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "Images converted to PDF. 1 credit deducted.",
            "remaining": remaining,
            "file_name": "images.pdf",
            "file_data": encoded,
        }
    )


@login_required
def remove_pages(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    page_spec = (request.POST.get("pages") or "").strip()
    if not page_spec:
        return JsonResponse({"status": "error", "message": "Specify at least one page to remove."}, status=400)

    def parse_pages(spec, total):
        pages = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    start_i = int(start)
                    end_i = int(end)
                    if start_i > end_i:
                        start_i, end_i = end_i, start_i
                    for i in range(start_i, end_i + 1):
                        if 1 <= i <= total:
                            pages.add(i - 1)  # zero-based
                except ValueError:
                    continue
            else:
                try:
                    num = int(part)
                    if 1 <= num <= total:
                        pages.add(num - 1)
                except ValueError:
                    continue
        return sorted(pages)

    try:
        reader = PdfReader(uploaded)
        num_pages = len(reader.pages)
        if num_pages <= 1:
            return JsonResponse({"status": "error", "message": "Only one page detected. Nothing to remove."}, status=400)

        to_remove = parse_pages(page_spec, num_pages)
        if not to_remove:
            return JsonResponse({"status": "error", "message": "No valid pages to remove."}, status=400)
        if len(to_remove) >= num_pages:
            return JsonResponse({"status": "error", "message": "Cannot remove all pages."}, status=400)

        writer = PdfWriter()
        for idx, page in enumerate(reader.pages):
            if idx not in to_remove:
                writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to remove pages. Please check the file and try again."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="remove-pages")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "Page removed successfully. 1 credit deducted.",
            "remaining": remaining,
            "file_name": "trimmed.pdf",
            "file_data": encoded,
        }
    )


@login_required
def remove_pages_inspect(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    uploaded = request.FILES.get("pdf")
    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    try:
        uploaded.seek(0)
        data = uploaded.read()
        reader = PdfReader(BytesIO(data))
        num_pages = len(reader.pages)
        if num_pages <= 0:
            return JsonResponse({"status": "error", "message": "No pages detected."}, status=400)
        if num_pages <= 1:
            return JsonResponse({"status": "error", "message": "Only one page detected. Nothing to remove."}, status=400)

        previews = []
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            with open(tmp_path, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
            doc = pdfium.PdfDocument(tmp_path)
            max_preview = min(num_pages, 50)
            for idx in range(max_preview):
                page = doc[idx]
                bmp = page.render(scale=0.6)
                img = bmp.to_pil()
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                previews.append(base64.b64encode(buf.read()).decode("ascii"))
        except Exception:
            previews = []
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    except Exception:
        return JsonResponse({"status": "error", "message": "Unable to read PDF. Please check the file."}, status=400)

    return JsonResponse({"status": "ok", "pages": num_pages, "previews": previews})


@login_required
def redact_text(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    terms_raw = (request.POST.get("terms") or "").strip()

    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)
    if not terms_raw:
        return JsonResponse({"status": "error", "message": "Specify at least one term to redact."}, status=400)

    terms = [t.strip() for t in terms_raw.split(",") if t.strip()]
    if not terms:
        return JsonResponse({"status": "error", "message": "Specify at least one term to redact."}, status=400)

    try:
        reader = PdfReader(uploaded)
        writer = PdfWriter()
        replaced_any = False

        for page in reader.pages:
            # attempt naive content replacement on stream
            page_obj = page
            content = page_obj.get_contents()
            if content is None:
                writer.add_page(page_obj)
                continue
            if not isinstance(content, list):
                streams = [content]
            else:
                streams = content

            new_streams = []
            for stream in streams:
                try:
                    data = stream.get_data()
                except Exception as exc:
                    logger.warning("redact_text: unable to read stream data", exc_info=exc)
                    new_streams.append(stream)
                    continue
                original = data
                for term in terms:
                    try:
                        data = data.replace(term.encode("utf-8"), b"[REDACTED]")
                    except Exception:
                        logger.debug("redact_text: term replacement failed", exc_info=True)
                if data != original:
                    replaced_any = True
                new_stream = DecodedStreamObject()
                new_stream.set_data(data)
                new_streams.append(new_stream)

            # rebuild page content
            if len(new_streams) == 1:
                page_obj[NameObject("/Contents")] = new_streams[0]
            else:
                page_obj[NameObject("/Contents")] = ArrayObject(new_streams)

            writer.add_page(page_obj)

        if not replaced_any:
            return JsonResponse({"status": "error", "message": "No matches found to redact."}, status=400)

        output = BytesIO()
        writer.write(output)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception as exc:
        logger.exception("redact_text failed", extra={"user": request.user.id, "terms": terms})
        return JsonResponse({"status": "error", "message": "Unable to redact text. Please check the file and try again."}, status=400)

    ServiceUsage.objects.create(user=request.user, tool_slug="redact-text")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": "Text redacted. 1 credit deducted.",
            "remaining": remaining,
            "file_name": "redacted.pdf",
            "file_data": encoded,
        }
    )


@login_required
def redact_text_inspect(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    uploaded = request.FILES.get("pdf")
    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    try:
        reader = PdfReader(uploaded)
        num_pages = len(reader.pages)
        if num_pages <= 0:
            return JsonResponse({"status": "error", "message": "No pages detected."}, status=400)
        text_content = []
        for page in reader.pages[:10]:  # limit for performance
            txt = page.extract_text() or ""
            text_content.append(txt)
        combined = " ".join(text_content)
        tokens = set(re.findall(r"\b\w{3,}\b", combined))
        suggestions = sorted(list(tokens))[:200]
    except Exception as exc:
        logger.exception("redact_text_inspect failed", extra={"user": getattr(request.user, 'id', None)})
        return JsonResponse({"status": "error", "message": "Unable to read PDF text."}, status=400)

    return JsonResponse({"status": "ok", "terms": suggestions})


@csrf_exempt
def increase_credits(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    api_key = request.headers.get("xapikey") or request.headers.get("X-Api-Key")
    expected_key = getattr(settings, "PDFSERVICE_API_KEY", None)
    if not expected_key or api_key != expected_key:
        return JsonResponse({"status": "error", "message": "Forbidden"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON body."}, status=400)

    data = payload.get("data") or {}
    email = (data.get("email") or "").strip().lower()
    amount_value = data.get("amount", data.get("aount"))

    if not email:
        return JsonResponse({"status": "error", "message": "Email is required."}, status=400)
    if amount_value is None:
        return JsonResponse({"status": "error", "message": "Amount is required."}, status=400)

    try:
        amount_dec = Decimal(str(amount_value))
    except Exception:
        return JsonResponse({"status": "error", "message": "Amount must be numeric."}, status=400)

    brackets = {
        Decimal("99"): 6,
        Decimal("200"): 13,
        Decimal("400"): 29,
        Decimal("800"): 64,
        Decimal("1600"): 140,
    }

    credits = brackets.get(amount_dec)
    if credits is None:
        return JsonResponse(
            {
                "status": "error",
                "message": "Invalid amount. Allowed amounts: 99, 200, 400, 800, 1600.",
            },
            status=400,
        )

    user_model = settings.AUTH_USER_MODEL
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        return JsonResponse({"status": "error", "message": "User not found."}, status=404)

    Purchase.objects.create(
        user=user,
        description=f"API credit top-up ({amount_dec} PHP)",
        credits=credits,
        amount=amount_dec,
        status="completed",
    )

    remaining = _available_credits(user)
    return JsonResponse(
        {
            "status": "ok",
            "message": f"Added {credits} credits.",
            "credits_added": credits,
            "balance": remaining,
        }
    )
User = get_user_model()
signer = TimestampSigner()
