import base64
from io import BytesIO
import tempfile
import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from decimal import Decimal
import json

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import DecodedStreamObject, NameObject, ArrayObject
import pypdfium2 as pdfium
from PIL import Image
import re
from PIL import Image
import zipfile
import os

from .forms import SignupForm
from .models import ServiceUsage, Purchase

logger = logging.getLogger(__name__)

def home(request):
    services = [
        "PDF Merge",
        "PDF Split",
        "PDF Compress",
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
