import base64
import json
import logging
import re
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.conf import settings
from django.utils.text import slugify

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
from PIL import Image, ImageDraw, ImageFont

from pages.models import ServiceUsage

from .models import FormField, FormTemplate

logger = logging.getLogger(__name__)

ADVANCED_TOOL_SLUG = "pdf-form-creator"
SUPPORTED_FILLABLE = {"text", "text_line", "multiline", "number", "checkbox", "radio", "date"}


def _available_credits(user):
    total = user.purchases.filter(status="completed").aggregate(
        total=Coalesce(Sum("credits"), 0)
    )["total"]
    used = ServiceUsage.objects.filter(user=user).count()
    remaining = (total or 0) - used
    return max(remaining, 0)


def _can_use_advanced_tools(user):
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(getattr(profile, "can_use_advanced_tools", False))


def _page_size_points(page_size):
    if page_size == "LETTER":
        return (612, 792)
    return (595, 842)


def _generate_key(label, used_keys):
    base = slugify(label).replace("-", "_").strip("_") or "field"
    candidate = base
    suffix = 2
    while candidate in used_keys:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_keys.add(candidate)
    return candidate


def _normalize_table_options(raw_options):
    options = raw_options or {}

    def _int_value(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    columns = _int_value(options.get("columns", 3), 3)
    rows = _int_value(options.get("rows", 2), 2)
    columns = max(1, min(columns, 12))
    rows = max(1, min(rows, 50))

    include_header = bool(options.get("include_header", True))
    cell_padding = _int_value(options.get("cell_padding", 2), 2)
    cell_padding = max(0, cell_padding)
    font_size = _int_value(options.get("font_size", 10), 10)
    font_size = max(6, font_size)
    border_width = _int_value(options.get("border_width", 1), 1)
    border_width = max(1, border_width)
    header_background = bool(options.get("header_background", False))
    header_bold = bool(options.get("header_bold", True))

    column_headers = options.get("column_headers") or []
    column_headers = [str(header).strip() for header in column_headers]
    if len(column_headers) != columns:
        column_headers = [f"Column {idx + 1}" for idx in range(columns)]

    column_widths = options.get("column_widths") or []
    widths = []
    if isinstance(column_widths, list) and len(column_widths) == columns:
        for raw_width in column_widths:
            try:
                widths.append(float(raw_width))
            except (TypeError, ValueError):
                widths.append(0)
    if len(widths) != columns or any(width <= 0 for width in widths):
        widths = [1 / columns for _ in range(columns)]
    else:
        total = sum(widths)
        if total <= 0:
            widths = [1 / columns for _ in range(columns)]
        else:
            widths = [width / total for width in widths]

    return {
        "columns": columns,
        "rows": rows,
        "include_header": include_header,
        "column_headers": column_headers,
        "column_widths": widths,
        "cell_padding": cell_padding,
        "font_size": font_size,
        "border_width": border_width,
        "header_background": header_background,
        "header_bold": header_bold,
    }


def _serialize_template(template):
    fields = []
    for field in template.fields.all().order_by("order", "id"):
        options = field.options_json or {}
        validation = field.validation_json or {}
        table_options = None
        if field.type == "table":
            table_options = _normalize_table_options(options)
        fields.append(
            {
                "id": field.id,
                "type": field.type,
                "label": field.label,
                "key": field.key,
                "required": field.required,
                "x": field.x,
                "y": field.y,
                "w": field.w,
                "h": field.h,
                "order": field.order,
                "options": options.get("options", []),
                "placeholder": options.get("placeholder", ""),
                "divider": options.get("divider", False),
                "font_size": None if field.type == "table" else options.get("font_size"),
                "validation": validation,
                "default_value": field.default_value,
                "table": table_options,
            }
        )
    return {
        "id": template.id,
        "name": template.name,
        "page_size": template.page_size,
        "default_output_mode": template.default_output_mode,
        "margins": {
            "top": template.margin_top,
            "right": template.margin_right,
            "bottom": template.margin_bottom,
            "left": template.margin_left,
        },
        "fields": fields,
    }


def _normalize_output_mode(raw_value, fallback):
    value = (raw_value or fallback or "FLATTENED").strip().upper()
    if value in ("FLATTENED", "FILLABLE"):
        return value
    if value.lower() in ("flattened", "fillable"):
        return value.upper()
    return "FLATTENED"


def _build_acroform(writer, page, fields):
    annotations = page.get("/Annots")
    if annotations is None:
        annotations = ArrayObject()
    elif not isinstance(annotations, ArrayObject):
        annotations = ArrayObject(annotations)

    field_array = ArrayObject()
    page_ref = getattr(page, "indirect_reference", None)
    default_da = "/Helv 12 Tf 0 g"
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    def make_empty_appearance(width, height):
        stream = DecodedStreamObject()
        stream.set_data(b"")
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

    def make_checkbox_appearance(width, height):
        padding = max(1.0, min(width, height) * 0.2)
        x1 = padding
        y1 = padding
        x2 = width - padding
        y2 = height - padding
        stream = DecodedStreamObject()
        commands = f"0 0 0 RG 1 w {x1} {y1} m {x2} {y2} l {x1} {y2} m {x2} {y1} l S"
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

    def normalize_export_value(value, used):
        raw = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_")
        base = raw or "Option"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    for idx, field in enumerate(fields, start=1):
        raw_name = field.get("key") or f"field_{idx}"
        safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_") or f"field_{idx}"
        required = bool(field.get("required"))

        if field["type"] == "radio" and field.get("options"):
            options = field.get("options") or []
            if not options:
                continue
            ff = 2 if required else 0
            ff |= 32768
            parent = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Btn"),
                    NameObject("/T"): TextStringObject(safe_name),
                    NameObject("/Ff"): NumberObject(ff),
                    NameObject("/Kids"): ArrayObject(),
                }
            )
            parent_ref = writer._add_object(parent)
            used_values = set()
            selected_value = None
            for option in options:
                if not isinstance(option, dict):
                    continue
                rect_values = option.get("rect")
                if not rect_values or len(rect_values) != 4:
                    continue
                export_value = normalize_export_value(option.get("value"), used_values)
                if selected_value is None and field.get("default_value") == option.get("value"):
                    selected_value = export_value
                option_rect = ArrayObject([FloatObject(val) for val in rect_values])
                option_width = float(rect_values[2] - rect_values[0])
                option_height = float(rect_values[3] - rect_values[1])
                on_ref = make_radio_appearance(option_width, option_height)
                off_ref = make_empty_appearance(option_width, option_height)
                annot = DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Widget"),
                        NameObject("/Parent"): parent_ref,
                        NameObject("/Rect"): option_rect,
                        NameObject("/AS"): NameObject("/Off"),
                        NameObject("/F"): NumberObject(4),
                        NameObject("/DA"): TextStringObject(default_da),
                        NameObject("/AP"): DictionaryObject(
                            {
                                NameObject("/N"): DictionaryObject(
                                    {
                                        NameObject(f"/{export_value}"): on_ref,
                                        NameObject("/Off"): off_ref,
                                    }
                                )
                            }
                        ),
                    }
                )
                if page_ref is not None:
                    annot[NameObject("/P")] = page_ref
                annot_ref = writer._add_object(annot)
                annotations.append(annot_ref)
                parent[NameObject("/Kids")].append(annot_ref)
                if selected_value == export_value:
                    annot[NameObject("/AS")] = NameObject(f"/{export_value}")
            if selected_value:
                parent[NameObject("/V")] = NameObject(f"/{selected_value}")
            field_array.append(parent_ref)
            continue

        rect = ArrayObject([FloatObject(val) for val in field["rect"]])
        rect_width = float(field["rect"][2] - field["rect"][0])
        rect_height = float(field["rect"][3] - field["rect"][1])
        font_size_override = field.get("font_size")
        if font_size_override not in (None, ""):
            try:
                text_font_size = float(font_size_override)
            except (TypeError, ValueError):
                text_font_size = None
            if text_font_size is not None:
                text_font_size = max(6, min(48, text_font_size))
            else:
                height_ratio = 0.5 if field.get("multiline") else 0.65
                text_font_size = max(9, min(12, rect_height * height_ratio))
        else:
            height_ratio = 0.5 if field.get("multiline") else 0.65
            text_font_size = max(9, min(12, rect_height * height_ratio))
        field_da = f"/Helv {text_font_size:.1f} Tf 0 g"

        if field["type"] in ("checkbox", "radio"):
            checked = bool(field.get("default_value"))
            ff = 2 if required else 0
            if field["type"] == "radio":
                ff |= 32768
            on_ref = (
                make_radio_appearance(rect_width, rect_height)
                if field["type"] == "radio"
                else make_checkbox_appearance(rect_width, rect_height)
            )
            off_ref = make_empty_appearance(rect_width, rect_height)
            annot = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Btn"),
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/T"): TextStringObject(safe_name),
                    NameObject("/Rect"): rect,
                    NameObject("/V"): NameObject("/Yes" if checked else "/Off"),
                    NameObject("/AS"): NameObject("/Yes" if checked else "/Off"),
                    NameObject("/Ff"): NumberObject(ff),
                    NameObject("/F"): NumberObject(4),
                    NameObject("/DA"): TextStringObject(default_da),
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
            if field["type"] == "checkbox":
                annot[NameObject("/MK")] = DictionaryObject(
                    {NameObject("/CA"): TextStringObject("X")}
                )
        else:
            ff = 0
            if field.get("multiline"):
                ff |= 4096
            if required:
                ff |= 2
            default_value = field.get("default_value") or ""
            annot = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/T"): TextStringObject(safe_name),
                    NameObject("/Rect"): rect,
                    NameObject("/Ff"): NumberObject(ff),
                    NameObject("/V"): TextStringObject(default_value),
                    NameObject("/DV"): TextStringObject(default_value),
                    NameObject("/DA"): TextStringObject(field_da),
                }
            )
            annot[NameObject("/F")] = NumberObject(4)

        if page_ref is not None:
            annot[NameObject("/P")] = page_ref
        annot_ref = writer._add_object(annot)
        annotations.append(annot_ref)
        field_array.append(annot_ref)

    page[NameObject("/Annots")] = annotations
    form = DictionaryObject(
        {
            NameObject("/Fields"): field_array,
            NameObject("/NeedAppearances"): BooleanObject(True),
            NameObject("/DA"): TextStringObject(default_da),
            NameObject("/DR"): DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject({NameObject("/Helv"): font_ref}),
                }
            ),
        }
    )
    writer._root_object.update({NameObject("/AcroForm"): form})


def _load_pdf_font(size, bold=False):
    font_paths = [
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeuib.ttf" if bold else "C:\\Windows\\Fonts\\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _build_pdf(template, fields, output_mode):
    page_width, page_height = _page_size_points(template.page_size)
    render_scale = 2.0
    render_dpi = int(72 * render_scale)
    img_width = int(page_width * render_scale)
    img_height = int(page_height * render_scale)
    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)
    base_font_size = max(10, int(12 * render_scale))
    font = _load_pdf_font(base_font_size)

    label_inset = 4 * render_scale
    label_gap = 4 * render_scale
    radio_label_gap = 8 * render_scale
    field_specs = []
    line_width = max(1, int(1 * render_scale))
    def calc_line_height(font_ref):
        try:
            bbox = font_ref.getbbox("Hg")
            return (bbox[3] - bbox[1]) + int(4 * render_scale)
        except Exception:
            return int(14 * render_scale)

    line_height = calc_line_height(font)

    def text_width(text, font_ref=None):
        font_ref = font_ref or font
        try:
            return draw.textlength(text, font=font_ref)
        except Exception:
            return len(text) * (6 * render_scale)

    def split_long_word(word, max_width, font_ref=None):
        if text_width(word, font_ref) <= max_width:
            return [word]
        parts = []
        current = ""
        for ch in word:
            test = f"{current}{ch}"
            if text_width(test, font_ref) <= max_width or not current:
                current = test
            else:
                parts.append(current)
                current = ch
        if current:
            parts.append(current)
        return parts

    def wrap_text(text, max_width, font_ref=None):
        raw_text = str(text or "")
        if not raw_text:
            return []
        lines = []
        for raw_line in raw_text.splitlines():
            words = raw_line.split()
            if not words:
                lines.append("")
                continue
            current = ""
            for word in words:
                if text_width(word, font_ref) > max_width:
                    if current:
                        lines.append(current)
                        current = ""
                    lines.extend(split_long_word(word, max_width, font_ref))
                    continue
                test = f"{current} {word}".strip()
                if text_width(test, font_ref) <= max_width or not current:
                    current = test
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        return lines

    def parse_font_size(value, fallback):
        try:
            size = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(6, min(size, 48))

    for field in fields:
        x = max(0.0, min(1.0, field.x))
        y = max(0.0, min(1.0, field.y))
        w = max(0.0, min(1.0, field.w))
        h = max(0.0, min(1.0, field.h))

        x_pt = x * page_width
        y_pt = y * page_height
        w_pt = w * page_width
        h_pt = h * page_height
        x_px = x_pt * render_scale
        y_px = y_pt * render_scale
        w_px = w_pt * render_scale
        h_px = h_pt * render_scale
        field_options = field.options_json or {}
        font_size_override = field_options.get("font_size")
        field_font_pt = parse_font_size(font_size_override, 12)
        field_font = _load_pdf_font(int(field_font_pt * render_scale))
        field_line_height = calc_line_height(field_font)
        line_rect = None

        if field.type in ("section", "heading", "paragraph"):
            title = field.label or "Section"
            if field.type == "paragraph":
                content_width = max(1, w_px - (label_inset * 2))
                lines = wrap_text(title, content_width, field_font)
                try:
                    ascent, descent = field_font.getmetrics()
                    base_height = ascent + descent
                except Exception:
                    base_height = field_line_height
                paragraph_line_height = max(base_height + int(1 * render_scale), int(9 * render_scale))
                for idx, line in enumerate(lines):
                    line_y = y_px + (idx * paragraph_line_height)
                    if line_y + base_height > y_px + h_px:
                        break
                    try:
                        draw.text((x_px + label_inset, line_y), line, fill="black", font=field_font, anchor="lt")
                    except TypeError:
                        draw.text((x_px + label_inset, line_y), line, fill="black", font=field_font)
            else:
                draw.text((x_px, y_px), title, fill="black", font=field_font)
                divider = (field.options_json or {}).get("divider")
                if divider or field.type == "heading":
                    line_y = y_px + field_line_height + (4 * render_scale)
                    draw.line(
                        (x_px, line_y, x_px + w_px, line_y),
                        fill="#222222",
                        width=line_width,
                    )
            continue

        label = field.label or "Untitled"

        if field.type == "checkbox":
            size = min(w_px, h_px, 14 * render_scale)
            label_x = x_px + size + (8 * render_scale)
            label_y = y_px + (2 * render_scale)
            draw.text((label_x, label_y), label, fill="black", font=field_font)
            draw.rectangle(
                (x_px, y_px, x_px + size, y_px + size),
                outline="#222222",
                width=line_width,
            )
        elif field.type == "text_line":
            label_text = label if label.rstrip().endswith(":") else f"{label}:"
            label_y = y_px + max(0, (h_px - field_line_height) / 2)
            try:
                label_width = draw.textlength(label_text, font=field_font)
            except Exception:
                label_width = len(label_text) * (6 * render_scale)
            line_start = x_px + label_width + label_gap
            min_line_width = 12 * render_scale
            max_start = x_px + w_px - min_line_width
            if line_start > max_start:
                line_start = max(x_px + label_inset, max_start)
            line_y = label_y + field_line_height - (2 * render_scale)
            draw.text((x_px, label_y), label_text, fill="black", font=field_font)
            draw.line(
                (line_start, line_y, x_px + w_px, line_y),
                fill="#222222",
                width=line_width,
            )
            line_start_pt = line_start / render_scale
            line_y_pt = line_y / render_scale
            text_height_pt = max(12.0, field_line_height / render_scale)
            rect_bottom = max(0.0, page_height - line_y_pt)
            rect_top = min(page_height, page_height - (line_y_pt - text_height_pt))
            if rect_top <= rect_bottom:
                rect_top = min(page_height, rect_bottom + text_height_pt)
            line_rect = [
                float(line_start_pt),
                float(rect_bottom),
                float(x_pt + w_pt),
                float(rect_top),
            ]
        elif field.type == "signature":
            draw.text(
                (x_px + label_inset, y_px + label_inset),
                label,
                fill="black",
                font=field_font,
            )
            line_y = y_px + max(h_px - (4 * render_scale), (2 * render_scale))
            draw.line(
                (x_px, line_y, x_px + w_px, line_y),
                fill="#222222",
                width=line_width,
            )
        elif field.type == "radio":
            group_label = label or "Radio group"
            draw.text(
                (x_px + label_inset, y_px + label_inset),
                group_label,
                fill="black",
                font=field_font,
            )
            options = (field.options_json or {}).get("options") or ["Option 1", "Option 2"]
            box_size = 12 * render_scale
            row_height = max(box_size + (4 * render_scale), field_line_height + (4 * render_scale))
            options_top = y_px + field_line_height + label_gap + radio_label_gap
            option_gap = 12 * render_scale
            label_pad = 6 * render_scale
            cursor_x = x_px
            cursor_y = options_top
            max_x = x_px + w_px
            option_specs = []
            for option in options:
                label_text = str(option)
                try:
                    label_width = draw.textlength(label_text, font=field_font)
                except Exception:
                    label_width = len(label_text) * (6 * render_scale)
                option_width = box_size + label_pad + label_width + option_gap
                if cursor_x + option_width > max_x and cursor_x > x_px:
                    cursor_x = x_px
                    cursor_y += row_height
                if cursor_y + box_size > y_px + h_px:
                    break
                draw.ellipse(
                    (cursor_x, cursor_y, cursor_x + box_size, cursor_y + box_size),
                    outline="#222222",
                    width=line_width,
                )
                draw.text(
                    (cursor_x + box_size + label_pad, cursor_y + (1 * render_scale)),
                    label_text,
                    fill="black",
                    font=field_font,
                )
                if field.type in SUPPORTED_FILLABLE:
                    cursor_x_pt = cursor_x / render_scale
                    cursor_y_pt = cursor_y / render_scale
                    box_size_pt = box_size / render_scale
                    rect = [
                        float(cursor_x_pt),
                        float(page_height - (cursor_y_pt + box_size_pt)),
                        float(cursor_x_pt + box_size_pt),
                        float(page_height - cursor_y_pt),
                    ]
                    option_specs.append({"value": option, "rect": rect})
                cursor_x += option_width
            if field.type in SUPPORTED_FILLABLE and option_specs:
                field_specs.append(
                    {
                        "key": field.key,
                        "type": "radio",
                        "required": field.required,
                        "options": option_specs,
                        "default_value": field.default_value,
                    }
                )
        elif field.type == "table":
            table_options = _normalize_table_options(field.options_json or {})
            columns = table_options["columns"]
            rows = table_options["rows"]
            include_header = table_options["include_header"]
            column_headers = table_options["column_headers"]
            column_widths = table_options["column_widths"]
            cell_padding = table_options["cell_padding"] * render_scale
            font_size = table_options["font_size"]
            border_width = max(1, int(table_options["border_width"] * render_scale))
            header_background = table_options["header_background"]
            header_bold = table_options["header_bold"]

            total_rows = rows + (1 if include_header else 0)
            if columns <= 0 or total_rows <= 0:
                continue

            row_height = h_px / total_rows
            col_widths_px = [w_px * ratio for ratio in column_widths]
            row_height_pt = h_pt / total_rows
            col_widths_pt = [w_pt * ratio for ratio in column_widths]

            if include_header and header_background:
                draw.rectangle(
                    (x_px, y_px, x_px + w_px, y_px + row_height),
                    fill="#f0f0f0",
                )

            draw.rectangle(
                (x_px, y_px, x_px + w_px, y_px + h_px),
                outline="#222222",
                width=border_width,
            )
            cursor_x = x_px
            for width in col_widths_px[:-1]:
                cursor_x += width
                draw.line(
                    (cursor_x, y_px, cursor_x, y_px + h_px),
                    fill="#222222",
                    width=border_width,
                )
            for row_idx in range(1, total_rows):
                row_y = y_px + row_idx * row_height
                draw.line(
                    (x_px, row_y, x_px + w_px, row_y),
                    fill="#222222",
                    width=border_width,
                )

            if include_header:
                header_font = _load_pdf_font(int(font_size * render_scale), bold=header_bold)
                cursor_x = x_px
                for col_idx in range(columns):
                    header_text = (
                        column_headers[col_idx]
                        if col_idx < len(column_headers)
                        else f"Column {col_idx + 1}"
                    )
                    try:
                        bbox = header_font.getbbox(header_text)
                        text_height = bbox[3] - bbox[1]
                    except Exception:
                        text_height = int(10 * render_scale)
                    text_x = cursor_x + cell_padding
                    text_y = y_px + (row_height - text_height) / 2
                    draw.text(
                        (text_x, text_y),
                        header_text,
                        fill="black",
                        font=header_font,
                    )
                    cursor_x += col_widths_px[col_idx]
            if output_mode == "FILLABLE":
                base_key = field.key or f"table_{field.id}"
                start_row = 1 if include_header else 0
                col_offsets = []
                running = 0.0
                for width in col_widths_pt:
                    col_offsets.append(running)
                    running += width
                for row_idx in range(rows):
                    cell_top = y_pt + ((start_row + row_idx) * row_height_pt)
                    cell_bottom = cell_top + row_height_pt
                    for col_idx in range(columns):
                        cell_left = x_pt + col_offsets[col_idx]
                        cell_width = col_widths_pt[col_idx]
                        pad = min(
                            float(table_options["cell_padding"]),
                            cell_width * 0.3,
                            row_height_pt * 0.3,
                        )
                        if cell_width - (2 * pad) < 6 or row_height_pt - (2 * pad) < 6:
                            pad = 0
                        inner_left = cell_left + pad
                        inner_right = cell_left + cell_width - pad
                        inner_top = cell_top + pad
                        inner_bottom = cell_bottom - pad
                        rect = [
                            float(inner_left),
                            float(page_height - inner_bottom),
                            float(inner_right),
                            float(page_height - inner_top),
                        ]
                        field_specs.append(
                            {
                                "key": f"{base_key}_r{row_idx + 1}_c{col_idx + 1}",
                                "type": "text",
                                "rect": rect,
                                "required": False,
                                "multiline": True,
                                "default_value": "",
                                "font_size": table_options["font_size"],
                            }
                        )
        else:
            draw.text((x_px + label_inset, y_px + label_inset), label, fill="black", font=field_font)
            draw.rectangle(
                (x_px, y_px, x_px + w_px, y_px + h_px),
                outline="#222222",
                width=line_width,
            )
            if field.type == "dropdown":
                caret_x = x_px + w_px - (14 * render_scale)
                caret_y = y_px + (h_px / 2) - (3 * render_scale)
                draw.polygon(
                    [
                        (caret_x, caret_y),
                        (caret_x + (8 * render_scale), caret_y),
                        (caret_x + (4 * render_scale), caret_y + (6 * render_scale)),
                    ],
                    fill="#222222",
                )

        if field.type in SUPPORTED_FILLABLE and field.type != "radio":
            if field.type == "checkbox":
                box_size = min(w_pt, h_pt, 14)
                rect = [
                    float(x_pt),
                    float(page_height - (y_pt + box_size)),
                    float(x_pt + box_size),
                    float(page_height - y_pt),
                ]
            elif field.type == "radio":
                box_size = min(w_pt, h_pt, 12)
                rect = [
                    float(x_pt),
                    float(page_height - (y_pt + box_size)),
                    float(x_pt + box_size),
                    float(page_height - y_pt),
                ]
            elif field.type == "text_line" and line_rect:
                rect = line_rect
            else:
                label_pad_pt = (field_line_height + label_gap) / render_scale
                min_input_height = 12
                max_label_pad = max(0, h_pt - min_input_height)
                label_pad_pt = min(label_pad_pt, h_pt * 0.4, max_label_pad)
                label_pad_pt = max(0, min(label_pad_pt, h_pt - 4))
                rect = [
                    float(x_pt),
                    float(page_height - (y_pt + h_pt)),
                    float(x_pt + w_pt),
                    float(page_height - (y_pt + label_pad_pt)),
                ]
            spec = {
                "key": field.key,
                "type": field.type,
                "rect": rect,
                "required": field.required,
                "multiline": field.type in {"text", "multiline", "number", "date"},
                "default_value": field.default_value,
            }
            if font_size_override not in (None, ""):
                spec["font_size"] = parse_font_size(font_size_override, 12)
            field_specs.append(spec)

    base_buffer = BytesIO()
    img.save(base_buffer, format="PDF", resolution=render_dpi)
    base_buffer.seek(0)

    warning = None
    if output_mode == "FILLABLE":
        warning_parts = []
        unsupported = [
            f
            for f in fields
            if f.type not in SUPPORTED_FILLABLE and f.type not in {"section", "table"}
        ]
        if unsupported:
            warning_parts.append(
                "Fillable export supports text, multiline, number, date, checkbox, and radio fields. "
                "Other fields are flattened."
            )
        warning = " ".join(warning_parts) if warning_parts else None
        if field_specs:
            reader = PdfReader(base_buffer)
            writer = PdfWriter()
            writer.append_pages_from_reader(reader)
            try:
                writer.set_need_appearances_writer()
            except AttributeError:
                pass
            _build_acroform(writer, writer.pages[0], field_specs)
            output = BytesIO()
            writer.write(output)
            output.seek(0)
            return output.read(), warning

    return base_buffer.read(), warning


def _extract_payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return None
    raw = request.POST.get("payload") or ""
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _save_template_payload(template, payload):
    errors = []
    field_errors = []
    allowed_types = {choice[0] for choice in FormField.FIELD_TYPES}

    name = (payload.get("name") or template.name or "Untitled template").strip()
    page_size = (payload.get("page_size") or template.page_size or "A4").strip().upper()
    if page_size not in {"A4", "LETTER"}:
        page_size = "A4"
    default_output_mode = _normalize_output_mode(
        payload.get("default_output_mode"), template.default_output_mode
    )

    margins = payload.get("margins") or {}
    def _margin(val, fallback):
        try:
            value = int(val)
            return value if value >= 0 else fallback
        except (TypeError, ValueError):
            return fallback

    template.name = name
    template.page_size = page_size
    template.default_output_mode = default_output_mode
    template.margin_top = _margin(margins.get("top"), template.margin_top)
    template.margin_right = _margin(margins.get("right"), template.margin_right)
    template.margin_bottom = _margin(margins.get("bottom"), template.margin_bottom)
    template.margin_left = _margin(margins.get("left"), template.margin_left)

    fields_payload = payload.get("fields") or []
    used_keys = set()
    new_fields = []

    for idx, raw in enumerate(fields_payload):
        field_id = raw.get("id")
        field_type = (raw.get("type") or "text").strip().lower()
        if field_type not in allowed_types:
            field_errors.append({"id": field_id, "message": "Unsupported field type."})
            continue
        label = (raw.get("label") or "").strip() or "Untitled field"
        key = (raw.get("key") or "").strip()
        if key:
            key = re.sub(r"\s+", "_", key)
            if key in used_keys:
                field_errors.append({"id": field_id, "message": "Field key must be unique."})
                continue
            used_keys.add(key)
        else:
            key = _generate_key(label, used_keys)

        try:
            x = float(raw.get("x", 0))
            y = float(raw.get("y", 0))
            w = float(raw.get("w", 0.2))
            h = float(raw.get("h", 0.05))
        except (TypeError, ValueError):
            field_errors.append({"id": field_id, "message": "Field coordinates are invalid."})
            continue

        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1):
            field_errors.append({"id": field_id, "message": "Field placement is out of bounds."})
            continue
        if x + w > 1 or y + h > 1:
            field_errors.append({"id": field_id, "message": "Field placement is out of bounds."})
            continue

        options = raw.get("options") or []
        placeholder = (raw.get("placeholder") or "").strip()
        divider = bool(raw.get("divider"))
        validation = raw.get("validation") or {}
        default_value = (raw.get("default_value") or "").strip()

        options_json = {}
        if field_type in {"dropdown", "radio"}:
            options_json["options"] = [str(opt).strip() for opt in options if str(opt).strip()]
        if placeholder:
            options_json["placeholder"] = placeholder
        if field_type == "section":
            options_json["divider"] = divider
        if field_type != "table":
            font_size_raw = raw.get("font_size")
            if font_size_raw not in (None, ""):
                try:
                    font_size = int(font_size_raw)
                except (TypeError, ValueError):
                    field_errors.append(
                        {"id": field_id, "message": "Font size must be a number."}
                    )
                    continue
                font_size = max(6, min(font_size, 48))
                options_json["font_size"] = font_size

        if field_type == "table":
            table_raw = raw.get("table") or {}
            try:
                columns_value = int(table_raw.get("columns", 3))
            except (TypeError, ValueError):
                field_errors.append({"id": field_id, "message": "Table columns must be a number."})
                continue
            try:
                rows_value = int(table_raw.get("rows", 2))
            except (TypeError, ValueError):
                field_errors.append({"id": field_id, "message": "Table rows must be a number."})
                continue
            if not (1 <= columns_value <= 12):
                field_errors.append(
                    {"id": field_id, "message": "Table columns must be between 1 and 12."}
                )
                continue
            if not (1 <= rows_value <= 50):
                field_errors.append(
                    {"id": field_id, "message": "Table rows must be between 1 and 50."}
                )
                continue
            table_raw = {**table_raw, "columns": columns_value, "rows": rows_value}
            options_json = _normalize_table_options(table_raw)

        validation_json = {}
        for key_name in ("min", "max", "min_length", "max_length"):
            if key_name in validation and validation[key_name] not in (None, ""):
                validation_json[key_name] = validation[key_name]

        new_fields.append(
            FormField(
                template=template,
                type=field_type,
                label=label,
                key=key,
                required=bool(raw.get("required")),
                x=x,
                y=y,
                w=w,
                h=h,
                order=idx,
                options_json=options_json,
                validation_json=validation_json,
                default_value=default_value,
            )
        )

    if field_errors:
        return errors, field_errors

    with transaction.atomic():
        template.save()
        template.fields.all().delete()
        if new_fields:
            FormField.objects.bulk_create(new_fields)

    return errors, field_errors


@login_required
def template_list(request):
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

    templates = FormTemplate.objects.filter(is_deleted=False)
    if not request.user.is_superuser:
        templates = templates.filter(owner=request.user)
    templates = templates.annotate(field_count=Count("fields"))

    return render(
        request,
        "form_creator/template_list.html",
        {"templates": templates, "credit_balance": balance},
    )


@login_required
def template_new(request):
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

    if request.method == "POST":
        name = (request.POST.get("name") or "Untitled template").strip()
        page_size = (request.POST.get("page_size") or "A4").strip().upper()
        if page_size not in {"A4", "LETTER"}:
            page_size = "A4"
        output_mode = _normalize_output_mode(
            request.POST.get("default_output_mode"), "FLATTENED"
        )
        template = FormTemplate.objects.create(
            owner=request.user,
            name=name or "Untitled template",
            page_size=page_size,
            default_output_mode=output_mode,
        )
        return redirect("form_creator_edit", template_id=template.id)

    return render(
        request,
        "form_creator/template_create.html",
        {"credit_balance": _available_credits(request.user)},
    )


@login_required
def template_edit(request, template_id):
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

    template = FormTemplate.objects.filter(id=template_id, is_deleted=False).first()
    if not template:
        messages.error(request, "That template was not found.")
        return redirect("form_creator")
    if not request.user.is_superuser and template.owner_id != request.user.id:
        messages.error(request, "You do not have access to that template.")
        return redirect("form_creator")

    template_data = _serialize_template(template)
    return render(
        request,
        "form_creator/template_builder.html",
        {
            "template": template,
            "template_data": template_data,
            "credit_balance": _available_credits(request.user),
        },
    )


@login_required
def template_save(request, template_id):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    template = get_object_or_404(FormTemplate, id=template_id, is_deleted=False)
    if not request.user.is_superuser and template.owner_id != request.user.id:
        return JsonResponse({"status": "error", "message": "Forbidden."}, status=403)

    payload = _extract_payload(request)
    if payload is None:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON payload."},
            status=400,
        )

    errors, field_errors = _save_template_payload(template, payload)
    if errors or field_errors:
        return JsonResponse(
            {"status": "error", "errors": errors, "field_errors": field_errors},
            status=400,
        )

    return JsonResponse(
        {"status": "ok", "saved_at": timezone.now().isoformat()},
    )


@login_required
def template_duplicate(request, template_id):
    if request.method != "POST":
        return redirect("form_creator")

    template = get_object_or_404(FormTemplate, id=template_id, is_deleted=False)
    if not request.user.is_superuser and template.owner_id != request.user.id:
        messages.error(request, "You do not have access to that template.")
        return redirect("form_creator")

    copy_template = FormTemplate.objects.create(
        owner=request.user,
        name=f"{template.name} Copy",
        page_size=template.page_size,
        margin_top=template.margin_top,
        margin_right=template.margin_right,
        margin_bottom=template.margin_bottom,
        margin_left=template.margin_left,
        default_output_mode=template.default_output_mode,
    )
    new_fields = [
        FormField(
            template=copy_template,
            type=field.type,
            label=field.label,
            key=field.key,
            required=field.required,
            x=field.x,
            y=field.y,
            w=field.w,
            h=field.h,
            order=field.order,
            options_json=field.options_json,
            validation_json=field.validation_json,
            default_value=field.default_value,
        )
        for field in template.fields.all()
    ]
    if new_fields:
        FormField.objects.bulk_create(new_fields)

    messages.success(request, "Template duplicated.")
    return redirect("form_creator_edit", template_id=copy_template.id)


@login_required
def template_delete(request, template_id):
    if request.method != "POST":
        return redirect("form_creator")

    template = get_object_or_404(FormTemplate, id=template_id, is_deleted=False)
    if not request.user.is_superuser and template.owner_id != request.user.id:
        messages.error(request, "You do not have access to that template.")
        return redirect("form_creator")

    template.is_deleted = True
    template.save(update_fields=["is_deleted"])
    messages.success(request, "Template deleted.")
    return redirect("form_creator")


@login_required
def template_export(request, template_id):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    template = get_object_or_404(FormTemplate, id=template_id, is_deleted=False)
    if not request.user.is_superuser and template.owner_id != request.user.id:
        return JsonResponse({"status": "error", "message": "Forbidden."}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        output_mode = payload.get("output_mode") or template.default_output_mode
    else:
        output_mode = request.POST.get("output_mode") or template.default_output_mode
    output_mode = _normalize_output_mode(output_mode, template.default_output_mode)

    fields = list(template.fields.all())
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        pdf_bytes, warning = _build_pdf(template, fields, output_mode)
    except Exception as exc:
        logger.exception("template_export failed", extra={"template": template.id})
        if is_ajax:
            if settings.DEBUG:
                return JsonResponse(
                    {"status": "error", "message": f"Export error: {exc}"},
                    status=400,
                )
            return JsonResponse(
                {"status": "error", "message": "Unable to generate PDF. Please try again."},
                status=400,
            )
        messages.error(request, "Unable to generate PDF. Please try again.")
        return redirect("form_creator")

    safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", template.name).strip("_") or "form_template"
    filename = f"{safe_title}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    if warning:
        response["X-Form-Creator-Warning"] = warning
    return response


@login_required
def template_api(request, template_id):
    template = get_object_or_404(FormTemplate, id=template_id, is_deleted=False)
    if not request.user.is_superuser and template.owner_id != request.user.id:
        return JsonResponse({"status": "error", "message": "Forbidden."}, status=403)

    if request.method == "GET":
        return JsonResponse({"status": "ok", "template": _serialize_template(template)})

    if request.method == "POST":
        payload = _extract_payload(request)
        if payload is None:
            return JsonResponse(
                {"status": "error", "message": "Invalid JSON payload."},
                status=400,
            )
        errors, field_errors = _save_template_payload(template, payload)
        if errors or field_errors:
            return JsonResponse(
                {"status": "error", "errors": errors, "field_errors": field_errors},
                status=400,
            )
        return JsonResponse({"status": "ok", "saved_at": timezone.now().isoformat()})

    return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)


@login_required
def template_export_api(request, template_id):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    template = get_object_or_404(FormTemplate, id=template_id, is_deleted=False)
    if not request.user.is_superuser and template.owner_id != request.user.id:
        return JsonResponse({"status": "error", "message": "Forbidden."}, status=403)

    payload = _extract_payload(request) or {}
    output_mode = _normalize_output_mode(payload.get("output_mode"), template.default_output_mode)

    fields = list(template.fields.all())
    try:
        pdf_bytes, warning = _build_pdf(template, fields, output_mode)
    except Exception as exc:
        logger.exception("template_export_api failed", extra={"template": template.id})
        if settings.DEBUG:
            return JsonResponse(
                {"status": "error", "message": f"Export error: {exc}"},
                status=400,
            )
        return JsonResponse(
            {"status": "error", "message": "Unable to generate PDF. Please try again."},
            status=400,
        )

    safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", template.name).strip("_") or "form_template"
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    return JsonResponse(
        {
            "status": "ok",
            "file_name": f"{safe_title}.pdf",
            "file_data": encoded,
            "warning": warning,
        }
    )
