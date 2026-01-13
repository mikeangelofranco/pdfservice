import base64
from io import BytesIO
import tempfile
import logging
import math

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
    ContentStream,
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
    page_ref = getattr(page, "indirect_ref", None)

    field_array = ArrayObject()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    zapf_font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/ZapfDingbats"),
        }
    )
    zapf_ref = writer._add_object(zapf_font)
    dr = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/Helv"): font_ref, NameObject("/ZaDb"): zapf_ref}
            ),
        }
    )

    def make_empty_appearance(width, height, font_resource=None):
        stream = DecodedStreamObject()
        stream.set_data(b"")
        resources = DictionaryObject()
        if font_resource is not None:
            resources[NameObject("/Font")] = font_resource
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
                ),
                NameObject("/Resources"): resources,
            }
        )
        return writer._add_object(stream)

    def make_checkbox_appearance(width, height):
        size = max(6.0, min(width, height) * 0.8)
        tx = max(0.5, (width - size) / 2)
        ty = max(0.5, (height - size) / 2)
        stream = DecodedStreamObject()
        commands = (
            f"BT /ZaDb {size:.2f} Tf 0 0 0 rg "
            f"1 0 0 1 {tx:.2f} {ty:.2f} Tm (4) Tj ET"
        )
        stream.set_data(commands.encode("ascii"))
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
                ),
                NameObject("/Resources"): DictionaryObject(
                    {
                        NameObject("/Font"): DictionaryObject(
                            {NameObject("/ZaDb"): zapf_ref}
                        )
                    }
                ),
            }
        )
        return writer._add_object(stream)

    for idx, field in enumerate(fields, start=1):
        safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", field["name"]).strip("_")
        field_name = safe_label or f"field_{idx}"
        rect = ArrayObject([FloatObject(val) for val in field["rect"]])

        if field["type"] == "checkbox":
            rect_w = rect[2] - rect[0]
            rect_h = rect[3] - rect[1]
            on_ref = make_checkbox_appearance(rect_w, rect_h)
            off_ref = make_empty_appearance(
                rect_w, rect_h, DictionaryObject({NameObject("/ZaDb"): zapf_ref})
            )
            widget = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/Rect"): rect,
                    NameObject("/AS"): NameObject("/Off"),
                    NameObject("/F"): NumberObject(4),
                    NameObject("/MK"): DictionaryObject({NameObject("/CA"): TextStringObject("4")}),
                    NameObject("/DA"): TextStringObject("/ZaDb 12 Tf 0 g"),
                    NameObject("/DR"): dr,
                    NameObject("/AP"): DictionaryObject(
                        {
                            NameObject("/N"): DictionaryObject(
                                {
                                    NameObject("/Yes"): on_ref,
                                    NameObject("/Off"): off_ref,
                                }
                            )
                        }
                    ),
                }
            )
            if page_ref is not None:
                widget[NameObject("/P")] = page_ref
            widget_ref = writer._add_object(widget)
            annot = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Btn"),
                    NameObject("/T"): TextStringObject(field_name),
                    NameObject("/V"): NameObject("/Off"),
                    NameObject("/Ff"): NumberObject(0),
                    NameObject("/DA"): TextStringObject("/ZaDb 12 Tf 0 g"),
                    NameObject("/Kids"): ArrayObject([widget_ref]),
                }
            )
        else:
            widget = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/Rect"): rect,
                    NameObject("/F"): NumberObject(4),
                    NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                    NameObject("/DR"): dr,
                }
            )
            if page_ref is not None:
                widget[NameObject("/P")] = page_ref
            widget_ref = writer._add_object(widget)
            annot = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/T"): TextStringObject(field_name),
                    NameObject("/V"): TextStringObject(""),
                    NameObject("/Ff"): NumberObject(0),
                    NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                    NameObject("/Kids"): ArrayObject([widget_ref]),
                }
            )

        annot_ref = writer._add_object(annot)
        widget[NameObject("/Parent")] = annot_ref
        annotations.append(widget_ref)
        field_array.append(annot_ref)

    page[NameObject("/Annots")] = annotations
    form = DictionaryObject(
        {
            NameObject("/Fields"): field_array,
            NameObject("/NeedAppearances"): BooleanObject(True),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
            NameObject("/DR"): DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/Helv"): font_ref, NameObject("/ZaDb"): zapf_ref}
                    ),
                }
            ),
        }
    )
    writer._root_object.update({NameObject("/AcroForm"): form})


def _resolve_annotations(page):
    annotations = page.get("/Annots")
    if annotations is None:
        return None
    if hasattr(annotations, "get_object"):
        annotations = annotations.get_object()
    if not isinstance(annotations, ArrayObject):
        annotations = ArrayObject(annotations)
    page[NameObject("/Annots")] = annotations
    return annotations


def _collect_widget_fields(pages):
    widgets = []
    name_index = 1
    for page in pages:
        annotations = _resolve_annotations(page)
        if not annotations:
            continue
        for annot in annotations:
            try:
                annot_obj = annot.get_object()
            except Exception:
                annot_obj = annot
            if annot_obj.get("/Subtype") != NameObject("/Widget"):
                continue
            if annot_obj.get("/Rect") is None:
                continue
            if annot_obj.get("/T") is None:
                annot_obj[NameObject("/T")] = TextStringObject(f"field_{name_index}")
                name_index += 1
            if annot_obj.get("/FT") is None:
                annot_obj[NameObject("/FT")] = NameObject("/Tx")
            widgets.append(annot)
    return widgets


def _build_acroform(writer, fields):
    field_array = ArrayObject(fields)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    zapf_font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/ZapfDingbats"),
        }
    )
    zapf_ref = writer._add_object(zapf_font)
    form = DictionaryObject(
        {
            NameObject("/Fields"): field_array,
            NameObject("/NeedAppearances"): BooleanObject(True),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
            NameObject("/DR"): DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/Helv"): font_ref, NameObject("/ZaDb"): zapf_ref}
                    ),
                }
            ),
        }
    )
    writer._root_object.update({NameObject("/AcroForm"): form})


def _add_full_page_text_fields(writer):
    fields = []
    for idx, page in enumerate(writer.pages, start=1):
        annotations = page.get("/Annots")
        if annotations is None:
            annotations = ArrayObject()
        elif hasattr(annotations, "get_object"):
            annotations = annotations.get_object()
        if not isinstance(annotations, ArrayObject):
            annotations = ArrayObject(annotations)
        page[NameObject("/Annots")] = annotations

        box = page.mediabox
        left = float(box.left) + 36
        bottom = float(box.bottom) + 36
        right = float(box.right) - 36
        top = float(box.top) - 36
        if right <= left or top <= bottom:
            left = float(box.left)
            bottom = float(box.bottom)
            right = float(box.right)
            top = float(box.top)

        rect = ArrayObject(
            [
                FloatObject(left),
                FloatObject(bottom),
                FloatObject(right),
                FloatObject(top),
            ]
        )
        annot = DictionaryObject(
            {
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/T"): TextStringObject(f"page_{idx}_content"),
                NameObject("/Rect"): rect,
                NameObject("/Ff"): NumberObject(4096),
                NameObject("/Q"): NumberObject(0),
                NameObject("/Border"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)]),
                NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
            }
        )
        annotations.append(annot)
        fields.append(annot)
    _build_acroform(writer, fields)


def _matrix_multiply(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    ]


def _transform_point(ctm, x, y):
    a, b, c, d, e, f = ctm
    return (a * x + c * y + e, b * x + d * y + f)


def _bbox_from_points(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _page_bounds(page):
    box = page.cropbox if page.cropbox is not None else page.mediabox
    return (
        float(box.left),
        float(box.bottom),
        float(box.right),
        float(box.top),
    )


def _clamp_rect(rect, page):
    left, bottom, right, top = _page_bounds(page)
    x1, y1, x2, y2 = rect
    x1 = max(left, min(x1, right))
    x2 = max(left, min(x2, right))
    y1 = max(bottom, min(y1, top))
    y2 = max(bottom, min(y2, top))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return [x1, y1, x2, y2]


def _dedupe_field_specs(fields):
    seen = set()
    unique = []
    for field in fields:
        rect = field["rect"]
        key = (
            field["type"],
            bool(field.get("multiline")),
            round(rect[0], 1),
            round(rect[1], 1),
            round(rect[2], 1),
            round(rect[3], 1),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(field)
    return unique


def _rects_intersect(rect_a, rect_b):
    return not (
        rect_a[2] <= rect_b[0]
        or rect_a[0] >= rect_b[2]
        or rect_a[3] <= rect_b[1]
        or rect_a[1] >= rect_b[3]
    )


def _remove_text_overlaps(fields):
    boxes = [
        field["rect"]
        for field in fields
        if field.get("type") in ("checkbox", "radio")
    ]
    if not boxes:
        return fields
    filtered = []
    for field in fields:
        if field.get("type") != "text":
            filtered.append(field)
            continue
        if any(_rects_intersect(field["rect"], box) for box in boxes):
            continue
        filtered.append(field)
    return filtered


def _matrix_scale(ctm):
    return math.sqrt(abs((ctm[0] * ctm[3]) - (ctm[1] * ctm[2]))) or 1.0


def _extract_drawn_fields(reader, page):
    contents = page.get_contents()
    if not contents:
        return []
    try:
        stream = ContentStream(contents, reader)
    except Exception:
        return []

    left, bottom, right, top = _page_bounds(page)
    page_width = right - left
    min_line_len = max(10.0, page_width * 0.02)
    max_line_thickness = 4.0
    min_box = 8.0
    max_box = 26.0
    text_height = 14.0
    max_text_box_height = 32.0

    fields = []
    line_segments = []
    vertical_segments = []

    def add_text_field_from_line(bbox, stroke_width):
        x1, y1, x2, y2 = bbox
        line_y = max(y1, y2)
        height = max(12.0, min(16.0, text_height))
        rect = [x1, line_y, x2, line_y + height]
        rect = _clamp_rect(rect, page)
        if rect[2] - rect[0] < 6:
            return
        fields.append({"type": "text", "rect": rect, "multiline": False})

    def add_line_segment(x1, y1, x2, y2, stroke_width):
        if abs(y2 - y1) > max_line_thickness:
            return
        length = math.hypot(x2 - x1, y2 - y1)
        if length < min_line_len:
            return
        y_mid = (y1 + y2) / 2.0
        if y_mid <= bottom + 2 or y_mid >= top - 2:
            return
        line_segments.append(
            {
                "x1": min(x1, x2),
                "x2": max(x1, x2),
                "y": y_mid,
                "stroke": stroke_width,
                "length": length,
            }
        )

    def add_vertical_segment(x1, y1, x2, y2, stroke_width):
        if abs(x2 - x1) > max_line_thickness:
            return
        length = math.hypot(x2 - x1, y2 - y1)
        if length < min_box:
            return
        x_mid = (x1 + x2) / 2.0
        if x_mid <= left + 2 or x_mid >= right - 2:
            return
        vertical_segments.append(
            {
                "x": x_mid,
                "y1": min(y1, y2),
                "y2": max(y1, y2),
                "stroke": stroke_width,
                "length": length,
            }
        )

    def add_text_field_from_box(bbox, stroke_width):
        x1, y1, x2, y2 = bbox
        inset = max(1.5, stroke_width * 1.2)
        rect = [x1 + inset, y1 + inset, x2 - inset, y2 - inset]
        rect = _clamp_rect(rect, page)
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        if width < 10 or height < 8:
            return
        fields.append({"type": "text", "rect": rect, "multiline": height >= 24})

    def add_checkbox(bbox):
        rect = _clamp_rect(bbox, page)
        fields.append({"type": "checkbox", "rect": rect})

    def add_radio(bbox):
        rect = _clamp_rect(bbox, page)
        fields.append({"type": "radio", "rect": rect})

    def classify_path(path_segments):
        if not path_segments:
            return
        stroke_width = max(seg.get("line_width", 1.0) for seg in path_segments)
        has_curve = any(seg["kind"] == "curve" for seg in path_segments)
        has_rect_line = any(seg["kind"] in ("rect", "line") for seg in path_segments)
        if has_curve and not has_rect_line:
            points = []
            for seg in path_segments:
                points.extend(seg["points"])
            bbox = _bbox_from_points(points)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            ratio = width / height if height else 0
            if (
                min_box <= width <= max_box
                and min_box <= height <= max_box
                and 0.8 <= ratio <= 1.25
            ):
                add_radio(bbox)
            return

        if path_segments and all(seg["kind"] == "line" for seg in path_segments):
            points = []
            axis_aligned = True
            for seg in path_segments:
                x1, y1, x2, y2 = seg["points"]
                points.extend([(x1, y1), (x2, y2)])
                if abs(x2 - x1) > max_line_thickness and abs(y2 - y1) > max_line_thickness:
                    axis_aligned = False
            bbox = _bbox_from_points(points)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            ratio = width / height if height else 0
            if (
                min_box <= width <= max_box
                and min_box <= height <= max_box
                and 0.8 <= ratio <= 1.25
                and len(path_segments) >= 3
            ):
                add_checkbox(bbox)
                return

        for seg in path_segments:
            if seg["kind"] == "rect":
                bbox = seg["bbox"]
                width = bbox[2] - bbox[0]
                height = bbox[3] - bbox[1]
                if width <= 0 or height <= 0:
                    continue
                if height <= max_line_thickness and width >= min_line_len:
                    add_line_segment(bbox[0], bbox[1], bbox[2], bbox[3], stroke_width)
                    continue
                if (
                    min_box <= width <= max_box
                    and min_box <= height <= max_box
                    and 0.8 <= (width / height) <= 1.25
                ):
                    add_checkbox(bbox)
                    continue
                add_line_segment(bbox[0], bbox[1], bbox[2], bbox[1], stroke_width)
                add_line_segment(bbox[0], bbox[3], bbox[2], bbox[3], stroke_width)
                add_vertical_segment(bbox[0], bbox[1], bbox[0], bbox[3], stroke_width)
                add_vertical_segment(bbox[2], bbox[1], bbox[2], bbox[3], stroke_width)
            elif seg["kind"] == "line":
                x1, y1, x2, y2 = seg["points"]
                if abs(y2 - y1) > max_line_thickness:
                    if abs(x2 - x1) <= max_line_thickness:
                        add_vertical_segment(x1, y1, x2, y2, stroke_width)
                    continue
                add_line_segment(x1, y1, x2, y2, stroke_width)

    paint_ops = {b"S", b"s", b"B", b"B*", b"b", b"b*"}
    reset_ops = {b"n"}

    def parse_stream(content_stream, resources, start_ctm=None, start_line_width=1.0, depth=0):
        if depth > 8:
            return
        ctm = start_ctm[:] if start_ctm else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        state_stack = []
        line_width = start_line_width
        current_point = None
        subpaths = []
        current_path = []

        def record_line_width():
            return line_width * _matrix_scale(ctm)

        for operands, operator in content_stream.operations:
            if operator == b"q":
                state_stack.append((ctm[:], line_width, current_point, current_path[:], list(subpaths)))
                current_point = None
                current_path = []
                subpaths = []
                continue
            if operator == b"Q":
                if state_stack:
                    ctm, line_width, current_point, current_path, subpaths = state_stack.pop()
                else:
                    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
                    line_width = 1.0
                    current_point = None
                    current_path = []
                    subpaths = []
                continue
            if operator == b"cm":
                try:
                    m = [float(val) for val in operands]
                    if len(m) == 6:
                        ctm = _matrix_multiply(ctm, m)
                except Exception:
                    pass
                continue
            if operator == b"w":
                try:
                    line_width = float(operands[0])
                except Exception:
                    line_width = 1.0
                continue
            if operator == b"Do":
                if not resources:
                    continue
                xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
                if not xobjects:
                    continue
                if hasattr(xobjects, "get_object"):
                    xobjects = xobjects.get_object()
                name = operands[0]
                xobj = xobjects.get(name) if hasattr(xobjects, "get") else None
                if not xobj:
                    continue
                try:
                    xobj = xobj.get_object()
                except Exception:
                    pass
                if not isinstance(xobj, DictionaryObject):
                    continue
                if xobj.get("/Subtype") != NameObject("/Form"):
                    continue
                try:
                    x_stream = ContentStream(xobj, reader)
                except Exception:
                    continue
                x_resources = xobj.get("/Resources") or resources
                if hasattr(x_resources, "get_object"):
                    x_resources = x_resources.get_object()
                x_ctm = ctm
                matrix = xobj.get("/Matrix")
                if matrix and len(matrix) == 6:
                    try:
                        m = [float(val) for val in matrix]
                        x_ctm = _matrix_multiply(ctm, m)
                    except Exception:
                        x_ctm = ctm
                parse_stream(x_stream, x_resources, x_ctm, line_width, depth + 1)
                continue
            if operator == b"m":
                if current_path:
                    subpaths.append(current_path)
                    current_path = []
                try:
                    x, y = float(operands[0]), float(operands[1])
                    current_point = _transform_point(ctm, x, y)
                except Exception:
                    current_point = None
                continue
            if operator == b"l":
                if current_point is None:
                    continue
                try:
                    x, y = float(operands[0]), float(operands[1])
                    end_point = _transform_point(ctm, x, y)
                except Exception:
                    continue
                current_path.append(
                    {"kind": "line", "points": [*current_point, *end_point], "line_width": record_line_width()}
                )
                current_point = end_point
                continue
            if operator == b"re":
                try:
                    x, y, w, h = (float(val) for val in operands[:4])
                except Exception:
                    continue
                corners = [
                    _transform_point(ctm, x, y),
                    _transform_point(ctm, x + w, y),
                    _transform_point(ctm, x + w, y + h),
                    _transform_point(ctm, x, y + h),
                ]
                bbox = _bbox_from_points(corners)
                if current_path:
                    subpaths.append(current_path)
                    current_path = []
                subpaths.append([{"kind": "rect", "bbox": bbox, "line_width": record_line_width()}])
                continue
            if operator in (b"c", b"v", b"y"):
                points = []
                try:
                    if operator == b"c":
                        coords = [float(val) for val in operands[:6]]
                        if current_point:
                            points.append(current_point)
                        points.extend(
                            [
                                _transform_point(ctm, coords[0], coords[1]),
                                _transform_point(ctm, coords[2], coords[3]),
                                _transform_point(ctm, coords[4], coords[5]),
                            ]
                        )
                        current_point = points[-1]
                    elif operator == b"v":
                        coords = [float(val) for val in operands[:4]]
                        if current_point:
                            points.append(current_point)
                        points.extend(
                            [
                                _transform_point(ctm, coords[0], coords[1]),
                                _transform_point(ctm, coords[2], coords[3]),
                            ]
                        )
                        current_point = points[-1] if points else current_point
                    elif operator == b"y":
                        coords = [float(val) for val in operands[:4]]
                        if current_point:
                            points.append(current_point)
                        points.extend(
                            [
                                _transform_point(ctm, coords[0], coords[1]),
                                _transform_point(ctm, coords[2], coords[3]),
                            ]
                        )
                        current_point = points[-1]
                except Exception:
                    points = []
                if points:
                    current_path.append({"kind": "curve", "points": points, "line_width": record_line_width()})
                continue
            if operator in paint_ops:
                if current_path:
                    subpaths.append(current_path)
                    current_path = []
                for sp in subpaths:
                    classify_path(sp)
                subpaths = []
                continue
            if operator in reset_ops:
                current_path = []
                subpaths = []
                continue

    resources = page.get("/Resources")
    if hasattr(resources, "get_object"):
        resources = resources.get_object()
    parse_stream(stream, resources, start_ctm=[1.0, 0.0, 0.0, 1.0, 0.0, 0.0], start_line_width=1.0)

    if line_segments:
        underline_max_stroke = 2.5
        rect_max_stroke = 2.5
        rect_min_height = 12.0
        rect_max_height = max_text_box_height

        horizontal_lines = [
            seg for seg in line_segments
            if seg["length"] >= min_line_len
            and bottom + 2 < seg["y"] < top - 2
        ]
        vertical_lines = [
            seg for seg in vertical_segments
            if seg["length"] >= min_box
        ]

        used_lines = set()

        def has_vertical_at(x, y_low, y_high):
            for v in vertical_lines:
                if abs(v["x"] - x) > 2.5:
                    continue
                if v["length"] < rect_min_height:
                    continue
                if v["y1"] <= y_low + 2.5 and v["y2"] >= y_high - 2.5:
                    return True
            return False

        def has_internal_line(x1, x2, y_low, y_high):
            for line in horizontal_lines:
                if line["y"] <= y_low + 3 or line["y"] >= y_high - 3:
                    continue
                overlap = min(x2, line["x2"]) - max(x1, line["x1"])
                if overlap <= 0:
                    continue
                max_width = max(x2 - x1, line["x2"] - line["x1"])
                if max_width <= 0:
                    continue
                if (overlap / max_width) >= 0.8:
                    return True
            return False

        rect_candidates = [
            (idx, seg) for idx, seg in enumerate(horizontal_lines)
            if seg["stroke"] <= rect_max_stroke
        ]
        rect_candidates.sort(key=lambda item: item[1]["y"])

        for i, (idx, top_seg) in enumerate(rect_candidates):
            for j in range(i + 1, len(rect_candidates)):
                idx2, bottom_seg = rect_candidates[j]
                y_diff = bottom_seg["y"] - top_seg["y"]
                if y_diff < rect_min_height:
                    continue
                if y_diff > rect_max_height:
                    break
                overlap = min(top_seg["x2"], bottom_seg["x2"]) - max(top_seg["x1"], bottom_seg["x1"])
                if overlap <= 0:
                    continue
                max_width = max(top_seg["x2"] - top_seg["x1"], bottom_seg["x2"] - bottom_seg["x1"])
                if max_width <= 0:
                    continue
                if (overlap / max_width) < 0.8:
                    continue
                x1 = max(top_seg["x1"], bottom_seg["x1"])
                x2 = min(top_seg["x2"], bottom_seg["x2"])
                if x2 - x1 < 20:
                    continue
                y_low = top_seg["y"]
                y_high = bottom_seg["y"]
                if y_high <= y_low:
                    y_low, y_high = y_high, y_low
                if not (has_vertical_at(x1, y_low, y_high) and has_vertical_at(x2, y_low, y_high)):
                    continue
                if has_internal_line(x1, x2, y_low, y_high):
                    continue
                used_lines.add(idx)
                used_lines.add(idx2)
                rect = [x1 + 1.5, y_low + 1.5, x2 - 1.5, y_high - 1.5]
                rect = _clamp_rect(rect, page)
                height = rect[3] - rect[1]
                if height < rect_min_height:
                    continue
                fields.append({"type": "text", "rect": rect, "multiline": height >= 24})
                break

        for idx, seg in enumerate(horizontal_lines):
            if seg["stroke"] > underline_max_stroke:
                continue
            if idx in used_lines:
                continue
            bbox = [seg["x1"], seg["y"], seg["x2"], seg["y"]]
            add_text_field_from_line(bbox, seg["stroke"])

    return _dedupe_field_specs(fields)


def _apply_detected_fields(writer, fields_by_page):
    field_array = ArrayObject()
    default_da = "/Helv 12 Tf 0 g"
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    zapf_font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/ZapfDingbats"),
        }
    )
    zapf_ref = writer._add_object(zapf_font)
    dr = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/Helv"): font_ref, NameObject("/ZaDb"): zapf_ref}
            )
        }
    )
    border = ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)])

    def make_empty_appearance(width, height, font_resource=None):
        stream = DecodedStreamObject()
        stream.set_data(b"")
        resources = DictionaryObject()
        if font_resource is not None:
            resources[NameObject("/Font")] = font_resource
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
                ),
                NameObject("/Resources"): resources,
            }
        )
        return writer._add_object(stream)

    def make_checkbox_appearance(width, height):
        size = max(6.0, min(width, height) * 0.8)
        tx = max(0.5, (width - size) / 2)
        ty = max(0.5, (height - size) / 2)
        stream = DecodedStreamObject()
        commands = (
            f"BT /ZaDb {size:.2f} Tf 0 0 0 rg "
            f"1 0 0 1 {tx:.2f} {ty:.2f} Tm (4) Tj ET"
        )
        stream.set_data(commands.encode("ascii"))
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
                ),
                NameObject("/Resources"): DictionaryObject(
                    {
                        NameObject("/Font"): DictionaryObject(
                            {NameObject("/ZaDb"): zapf_ref}
                        )
                    }
                ),
            }
        )
        return writer._add_object(stream)

    def make_radio_appearance(width, height):
        radius = min(width, height) * 0.28
        cx = width / 2
        cy = height / 2
        k = 0.552284749831
        r = radius
        x0 = cx - r
        y0 = cy
        x1 = cx - r
        y1 = cy + k * r
        x2 = cx - k * r
        y2 = cy + r
        x3 = cx
        y3 = cy + r
        x4 = cx + k * r
        y4 = cy + r
        x5 = cx + r
        y5 = cy + k * r
        x6 = cx + r
        y6 = cy
        x7 = cx + r
        y7 = cy - k * r
        x8 = cx + k * r
        y8 = cy - r
        x9 = cx
        y9 = cy - r
        x10 = cx - k * r
        y10 = cy - r
        x11 = cx - r
        y11 = cy - k * r
        stream = DecodedStreamObject()
        commands = (
            f"0 0 0 rg {x0} {y0} m "
            f"{x1} {y1} {x2} {y2} {x3} {y3} c "
            f"{x4} {y4} {x5} {y5} {x6} {y6} c "
            f"{x7} {y7} {x8} {y8} {x9} {y9} c "
            f"{x10} {y10} {x11} {y11} {x0} {y0} c f"
        )
        stream.set_data(commands.encode("ascii"))
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
                ),
                NameObject("/Resources"): DictionaryObject(),
            }
        )
        return writer._add_object(stream)

    field_index = 1
    for page_index, page_fields in enumerate(fields_by_page):
        if not page_fields:
            continue
        page = writer.pages[page_index]
        annotations = page.get("/Annots")
        if annotations is None:
            annotations = ArrayObject()
        elif hasattr(annotations, "get_object"):
            annotations = annotations.get_object()
        if not isinstance(annotations, ArrayObject):
            annotations = ArrayObject(annotations)
        page[NameObject("/Annots")] = annotations
        page_ref = getattr(page, "indirect_ref", None)

        for field in page_fields:
            rect = field["rect"]
            rect_width = rect[2] - rect[0]
            rect_height = rect[3] - rect[1]
            rect_obj = ArrayObject([FloatObject(val) for val in rect])
            field_name = f"auto_{field_index}"
            field_index += 1

            if field["type"] in ("checkbox", "radio"):
                ff = 0
                check_size = max(8.0, min(18.0, rect_height * 0.9))
                check_da = f"/ZaDb {check_size:.1f} Tf 0 g"
                if field["type"] == "radio":
                    ff |= 32768
                    on_ref = make_radio_appearance(rect_width, rect_height)
                else:
                    on_ref = make_checkbox_appearance(rect_width, rect_height)
                off_ref = make_empty_appearance(rect_width, rect_height)
                widget = DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Widget"),
                        NameObject("/Rect"): rect_obj,
                        NameObject("/AS"): NameObject("/Off"),
                        NameObject("/F"): NumberObject(4),
                        NameObject("/Border"): border,
                        NameObject("/DA"): TextStringObject(check_da),
                        NameObject("/DR"): dr,
                        NameObject("/MK"): DictionaryObject(
                            {NameObject("/CA"): TextStringObject("4")}
                        ),
                        NameObject("/AP"): DictionaryObject(
                            {
                                NameObject("/N"): DictionaryObject(
                                    {
                                        NameObject("/Yes"): on_ref,
                                        NameObject("/On"): on_ref,
                                        NameObject("/Off"): off_ref,
                                    }
                                )
                            }
                        ),
                    }
                )
                if page_ref is not None:
                    widget[NameObject("/P")] = page_ref
                widget_ref = writer._add_object(widget)
                annot = DictionaryObject(
                    {
                        NameObject("/FT"): NameObject("/Btn"),
                        NameObject("/T"): TextStringObject(field_name),
                        NameObject("/V"): NameObject("/Off"),
                        NameObject("/Ff"): NumberObject(ff),
                        NameObject("/DA"): TextStringObject(check_da),
                        NameObject("/DR"): dr,
                        NameObject("/Kids"): ArrayObject([widget_ref]),
                    }
                )
            else:
                ff = 4096 if field.get("multiline") else 0
                font_size = max(9.0, min(12.0, rect_height * 0.8))
                field_da = f"/Helv {font_size:.1f} Tf 0 g"
                widget = DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Widget"),
                        NameObject("/Rect"): rect_obj,
                        NameObject("/F"): NumberObject(4),
                        NameObject("/Border"): border,
                        NameObject("/DA"): TextStringObject(field_da),
                        NameObject("/DR"): dr,
                    }
                )
                if page_ref is not None:
                    widget[NameObject("/P")] = page_ref
                widget_ref = writer._add_object(widget)
                annot = DictionaryObject(
                    {
                        NameObject("/FT"): NameObject("/Tx"),
                        NameObject("/T"): TextStringObject(field_name),
                        NameObject("/V"): TextStringObject(""),
                        NameObject("/Ff"): NumberObject(ff),
                        NameObject("/DA"): TextStringObject(field_da),
                        NameObject("/DR"): dr,
                        NameObject("/Kids"): ArrayObject([widget_ref]),
                    }
                )
            annot_ref = writer._add_object(annot)
            widget[NameObject("/Parent")] = annot_ref
            annotations.append(widget_ref)
            field_array.append(annot_ref)

    if field_array:
        form = DictionaryObject(
            {
                NameObject("/Fields"): field_array,
                NameObject("/NeedAppearances"): BooleanObject(True),
                NameObject("/DA"): TextStringObject(default_da),
                NameObject("/DR"): dr,
            }
        )
        writer._root_object.update({NameObject("/AcroForm"): form})


def _detect_checkbox_fields_from_raster(doc, page_index, page):
    pdf_page = doc[page_index]
    scale = 2.0
    bitmap = pdf_page.render(scale=scale)
    image = bitmap.to_pil().convert("L")
    width, height = image.size
    pixels = image.tobytes()
    visited = bytearray(width * height)
    threshold = 170
    min_px = int(5 * scale)
    max_px = int(30 * scale)
    max_fill_ratio = 0.35
    fields = []

    def to_pdf_rect(x1, y1, x2, y2):
        return [
            x1 / scale,
            (height - y2) / scale,
            x2 / scale,
            (height - y1) / scale,
        ]

    for y in range(height):
        row_idx = y * width
        for x in range(width):
            idx = row_idx + x
            if visited[idx]:
                continue
            if pixels[idx] >= threshold:
                continue
            stack = [idx]
            visited[idx] = 1
            min_x = max_x = x
            min_y = max_y = y
            count = 0
            while stack:
                cur = stack.pop()
                count += 1
                cy, cx = divmod(cur, width)
                if cx < min_x:
                    min_x = cx
                if cx > max_x:
                    max_x = cx
                if cy < min_y:
                    min_y = cy
                if cy > max_y:
                    max_y = cy
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < 0 or nx >= width or ny < 0 or ny >= height:
                        continue
                    n_idx = ny * width + nx
                    if visited[n_idx]:
                        continue
                    if pixels[n_idx] >= threshold:
                        continue
                    visited[n_idx] = 1
                    stack.append(n_idx)
            box_w = max_x - min_x + 1
            box_h = max_y - min_y + 1
            if box_w < min_px or box_h < min_px:
                continue
            if box_w > max_px or box_h > max_px:
                continue
            ratio = box_w / box_h if box_h else 0
            if ratio < 0.7 or ratio > 1.3:
                continue
            area = box_w * box_h
            if area <= 0:
                continue
            fill_ratio = count / area
            if fill_ratio > max_fill_ratio:
                continue
            rect = to_pdf_rect(min_x, min_y, max_x + 1, max_y + 1)
            rect = _clamp_rect(rect, page)
            fields.append({"type": "checkbox", "rect": rect})

    return _dedupe_field_specs(fields)


def _detect_line_fields_from_raster(doc, page_index, page):
    pdf_page = doc[page_index]
    scale = 2.0
    bitmap = pdf_page.render(scale=scale)
    image = bitmap.to_pil().convert("L")
    width, height = image.size
    pixels = image.tobytes()
    threshold = 170
    min_len_px = max(6, int(6 * scale))
    max_thickness_px = max(2, int(2 * scale))
    overlap_ratio = 0.7
    edge_tol_px = max(2, int(2 * scale))
    min_rect_height_px = max(10, int(10 * scale))
    max_rect_height_px = max(min_rect_height_px + 6, int(120 * scale))
    min_rect_width_px = max(30, int(20 * scale))
    fields = []

    horizontal_lines = []
    active = []

    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        segments = []
        x = 0
        while x < width:
            while x < width and row[x] >= threshold:
                x += 1
            if x >= width:
                break
            start = x
            while x < width and row[x] < threshold:
                x += 1
            end = x - 1
            if end - start + 1 >= min_len_px:
                segments.append((start, end))

        matched = [False] * len(segments)
        new_active = []
        for ax1, ax2, ay1, ay2 in active:
            found = False
            for idx, (sx1, sx2) in enumerate(segments):
                if matched[idx]:
                    continue
                overlap = min(ax2, sx2) - max(ax1, sx1)
                if overlap <= 0:
                    continue
                min_width = min(ax2 - ax1, sx2 - sx1)
                if min_width <= 0:
                    continue
                if (overlap / min_width) < overlap_ratio:
                    continue
                new_active.append((min(ax1, sx1), max(ax2, sx2), ay1, y))
                matched[idx] = True
                found = True
                break
            if not found:
                if ay2 - ay1 + 1 <= max_thickness_px:
                    horizontal_lines.append((ax1, ax2, ay1, ay2))
        for idx, (sx1, sx2) in enumerate(segments):
            if not matched[idx]:
                new_active.append((sx1, sx2, y, y))
        active = new_active

    for ax1, ax2, ay1, ay2 in active:
        if ay2 - ay1 + 1 <= max_thickness_px:
            horizontal_lines.append((ax1, ax2, ay1, ay2))

    vertical_lines = []
    active = []

    for x in range(width):
        segments = []
        y = 0
        while y < height:
            while y < height and pixels[(y * width) + x] >= threshold:
                y += 1
            if y >= height:
                break
            start = y
            while y < height and pixels[(y * width) + x] < threshold:
                y += 1
            end = y - 1
            if end - start + 1 >= min_len_px:
                segments.append((start, end))

        matched = [False] * len(segments)
        new_active = []
        for ay1, ay2, ax1, ax2 in active:
            found = False
            for idx, (sy1, sy2) in enumerate(segments):
                if matched[idx]:
                    continue
                overlap = min(ay2, sy2) - max(ay1, sy1)
                if overlap <= 0:
                    continue
                min_len = min(ay2 - ay1, sy2 - sy1)
                if min_len <= 0:
                    continue
                if (overlap / min_len) < overlap_ratio:
                    continue
                new_active.append((min(ay1, sy1), max(ay2, sy2), ax1, x))
                matched[idx] = True
                found = True
                break
            if not found:
                if ax2 - ax1 + 1 <= max_thickness_px:
                    vertical_lines.append((ax1, ax2, ay1, ay2))
        for idx, (sy1, sy2) in enumerate(segments):
            if not matched[idx]:
                new_active.append((sy1, sy2, x, x))
        active = new_active

    for ay1, ay2, ax1, ax2 in active:
        if ax2 - ax1 + 1 <= max_thickness_px:
            vertical_lines.append((ax1, ax2, ay1, ay2))

    def has_vertical_at(x_pos, y_top, y_bottom):
        for vx1, vx2, vy1, vy2 in vertical_lines:
            x_center = (vx1 + vx2) / 2.0
            if abs(x_center - x_pos) > edge_tol_px:
                continue
            if vy1 <= y_top + edge_tol_px and vy2 >= y_bottom - edge_tol_px:
                return True
        return False

    rects = []
    used_lines = set()
    horizontal_sorted = sorted(horizontal_lines, key=lambda seg: (seg[2] + seg[3]) / 2.0)
    for i, top in enumerate(horizontal_sorted):
        top_y = (top[2] + top[3]) / 2.0
        for j in range(i + 1, len(horizontal_sorted)):
            bottom = horizontal_sorted[j]
            bottom_y = (bottom[2] + bottom[3]) / 2.0
            height_px = bottom_y - top_y
            if height_px < min_rect_height_px:
                continue
            if height_px > max_rect_height_px:
                break
            x1 = max(top[0], bottom[0])
            x2 = min(top[1], bottom[1])
            if x2 - x1 < min_rect_width_px:
                continue
            if not (has_vertical_at(x1, top_y, bottom_y) and has_vertical_at(x2, top_y, bottom_y)):
                continue
            rects.append((x1, x2, top_y, bottom_y))
            used_lines.add(i)
            used_lines.add(j)
            break

    inset_px = max(2, int(1.5 * scale))
    for x1, x2, y_top, y_bottom in rects:
        rx1 = x1 + inset_px
        rx2 = x2 - inset_px
        ry1 = y_top + inset_px
        ry2 = y_bottom - inset_px
        if rx2 <= rx1 or ry2 <= ry1:
            continue
        rect = [
            rx1 / scale,
            (height - ry2) / scale,
            rx2 / scale,
            (height - ry1) / scale,
        ]
        rect = _clamp_rect(rect, page)
        if rect[2] - rect[0] < 10 or rect[3] - rect[1] < 8:
            continue
        fields.append({"type": "text", "rect": rect, "multiline": (rect[3] - rect[1]) >= 24})

    for idx, (x1, x2, y1, y2) in enumerate(horizontal_sorted):
        if idx in used_lines:
            continue
        if x2 <= x1:
            continue
        line_y = (y1 + y2) / 2.0
        rect = [x1 / scale, (height - line_y) / scale, x2 / scale, (height - line_y) / scale + 14.0]
        rect = _clamp_rect(rect, page)
        if rect[2] - rect[0] < 10:
            continue
        fields.append({"type": "text", "rect": rect, "multiline": False})

    return _dedupe_field_specs(fields)

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
                "We found your account. Please email support@plughub-ims.com "
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
def fillable_form_convert(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    if not _can_use_advanced_tools(request.user):
        return JsonResponse(
            {
                "status": "error",
                "message": (
                    "Fillable Form Converter is an advanced tool. Ask your admin to enable "
                    "advanced access for your account."
                ),
            },
            status=403,
        )

    balance = _available_credits(request.user)
    if balance <= 0:
        return JsonResponse({"status": "error", "message": "No credits remaining. Add credits to continue."}, status=400)

    uploaded = request.FILES.get("pdf")
    if not uploaded:
        return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)

    try:
        uploaded.seek(0)
        uploaded_bytes = uploaded.read()
        if not uploaded_bytes:
            return JsonResponse({"status": "error", "message": "Upload a PDF to continue."}, status=400)
        reader = PdfReader(BytesIO(uploaded_bytes))
        if reader.is_encrypted:
            try:
                res = reader.decrypt("")
            except Exception:
                res = 0
            if res in (0, False, None):
                return JsonResponse({"status": "error", "message": "PDF is encrypted. Unlock it first."}, status=400)

        writer = PdfWriter()
        writer.clone_document_from_reader(reader)

        manual_fields_by_page = None
        if manual_fields_by_page:
            pass
        else:
            acroform = writer._root_object.get("/AcroForm")
            acroform_obj = acroform.get_object() if hasattr(acroform, "get_object") else acroform
            widgets = _collect_widget_fields(writer.pages)

            has_fields = False
            if acroform_obj:
                fields = acroform_obj.get("/Fields")
                fields_obj = fields.get_object() if hasattr(fields, "get_object") else fields
                if fields_obj and len(fields_obj) > 0:
                    has_fields = True
                    acroform_obj[NameObject("/NeedAppearances")] = BooleanObject(True)
                    if acroform_obj.get("/DA") is None:
                        acroform_obj[NameObject("/DA")] = TextStringObject("/Helv 12 Tf 0 g")
                    font = DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                    font_ref = writer._add_object(font)
                    zapf_font = DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/ZapfDingbats"),
                        }
                    )
                    zapf_ref = writer._add_object(zapf_font)
                    if acroform_obj.get("/DR") is None:
                        acroform_obj[NameObject("/DR")] = DictionaryObject(
                            {
                                NameObject("/Font"): DictionaryObject(
                                    {NameObject("/Helv"): font_ref, NameObject("/ZaDb"): zapf_ref}
                                )
                            }
                        )
                    else:
                        dr_obj = acroform_obj.get("/DR")
                        if hasattr(dr_obj, "get_object"):
                            dr_obj = dr_obj.get_object()
                        font_dict = dr_obj.get("/Font") if hasattr(dr_obj, "get") else None
                        if hasattr(font_dict, "get_object"):
                            font_dict = font_dict.get_object()
                        if not isinstance(font_dict, DictionaryObject):
                            font_dict = DictionaryObject()
                        if font_dict.get("/Helv") is None:
                            font_dict[NameObject("/Helv")] = font_ref
                        if font_dict.get("/ZaDb") is None:
                            font_dict[NameObject("/ZaDb")] = zapf_ref
                        dr_obj[NameObject("/Font")] = font_dict
                        acroform_obj[NameObject("/DR")] = dr_obj
            if widgets and not has_fields:
                has_fields = True

            if has_fields:
                if not acroform_obj:
                    _build_acroform(writer, widgets)
                elif widgets:
                    if not acroform_obj.get("/Fields"):
                        acroform_obj[NameObject("/Fields")] = ArrayObject(widgets)
                    acroform_obj[NameObject("/NeedAppearances")] = BooleanObject(True)
                message = "Fillable fields detected. 1 credit deducted."
            else:
                fields_by_page = [_extract_drawn_fields(reader, p) for p in reader.pages]
                has_checkboxes = any(
                    f["type"] in ("checkbox", "radio")
                    for page_fields in fields_by_page
                    for f in page_fields
                )
                need_raster_lines = True
                need_raster_checkboxes = not has_checkboxes
                if need_raster_lines or need_raster_checkboxes:
                    tmp_path = None
                    try:
                        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
                        os.close(fd)
                        with open(tmp_path, "wb") as tmp:
                            tmp.write(uploaded_bytes)
                            tmp.flush()
                        doc = pdfium.PdfDocument(tmp_path)
                        for page_index, page in enumerate(reader.pages):
                            if need_raster_lines:
                                raster_lines = _detect_line_fields_from_raster(doc, page_index, page)
                                if raster_lines:
                                    fields_by_page[page_index].extend(raster_lines)
                            if need_raster_checkboxes:
                                raster_fields = _detect_checkbox_fields_from_raster(doc, page_index, page)
                                if raster_fields:
                                    fields_by_page[page_index].extend(raster_fields)
                            if fields_by_page[page_index]:
                                fields_by_page[page_index] = _dedupe_field_specs(
                                    fields_by_page[page_index]
                                )
                    finally:
                        if tmp_path:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                fields_by_page = [
                    _remove_text_overlaps(_dedupe_field_specs(page_fields))
                    if page_fields
                    else page_fields
                    for page_fields in fields_by_page
                ]
                total_fields = sum(len(page_fields) for page_fields in fields_by_page)
                if total_fields <= 0:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": (
                                "No lines, checkboxes, or radio buttons were detected. "
                                "Try another PDF or use the PDF Form Creator."
                            ),
                        },
                        status=400,
                    )
                _apply_detected_fields(writer, fields_by_page)
                message = f"Fillable fields added ({total_fields}). 1 credit deducted."

        output = BytesIO()
        writer.write(output)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode("ascii")
    except Exception:
        logger.exception("fillable_form_convert failed")
        return JsonResponse(
            {
                "status": "error",
                "message": "Unable to convert PDF to a fillable form. Please check the file and try again.",
            },
            status=400,
        )

    ServiceUsage.objects.create(user=request.user, tool_slug="fillable-form-converter")
    remaining = _available_credits(request.user)

    return JsonResponse(
        {
            "status": "ok",
            "message": message,
            "remaining": remaining,
            "file_name": f"fillable_{uploaded.name}",
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
