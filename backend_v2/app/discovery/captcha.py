"""Recognition of the portal's document-download captcha.

A captcha page is not a separate window: it *is* the response to the file
request. The page carries the answer input and the picture of the challenge.
A page that merely mentions a captcha is not one, and treating it as one is
worse than not asking at all: a person cannot answer a picture that is not
shown.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

_CAPTCHA_INPUT_RE = re.compile(
    r"<input\b[^>]*\bname=[\"']captchaText[\"'][^>]*>", re.IGNORECASE
)
_CAPTCHA_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bid=[\"']captchaImage[\"'][^>]*>", re.IGNORECASE
)
_FORM_TAG_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
_HIDDEN_INPUT_RE = re.compile(
    r"<input\b[^>]*\btype=[\"']hidden[\"'][^>]*>", re.IGNORECASE
)
_ATTR_RE = re.compile(
    r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL
)


@dataclass(slots=True)
class CaptchaForm:
    """The form the portal handed us instead of the file.

    The fields are the portal's own: the seed, the token, the page it came
    from and the session. They never travel to the browser UI and are never
    stored in Postgres; only the picture is shown to the human.
    """

    action: str
    fields: dict[str, str]
    answer_field: str = "captchaText"
    image: bytes | None = None
    page: str = ""


def _attributes(tag: str) -> dict[str, str]:
    return {match.group(1).lower(): match.group(3) for match in _ATTR_RE.finditer(tag)}


def _decode_data_image(source: str) -> bytes | None:
    if not source or not source.startswith("data:"):
        return None
    try:
        header, payload = source.split(",", 1)
    except ValueError:
        return None
    if "base64" not in header.lower():
        return None
    try:
        return base64.b64decode(payload)
    except Exception:  # noqa: BLE001 - a broken picture is no picture
        return None


def parse_captcha_form(html: str) -> CaptchaForm | None:
    """Read the portal's captcha form out of a page.

    Both the answer input and the picture must be present. The portal's chrome
    mentions captcha on every page, so a mention alone is never enough.
    """

    if not html:
        return None
    input_tag = _CAPTCHA_INPUT_RE.search(html)
    image_tag = _CAPTCHA_IMAGE_RE.search(html)
    if not input_tag or not image_tag:
        return None

    form_start = html.rfind("<form", 0, input_tag.start())
    if form_start < 0:
        return None
    form_tag_match = _FORM_TAG_RE.search(html, form_start)
    if not form_tag_match:
        return None
    form_end = html.find("</form>", form_tag_match.end())
    form_html = html[form_tag_match.end() : form_end if form_end > 0 else len(html)]

    action = _attributes(form_tag_match.group(0)).get("action", "")
    fields: dict[str, str] = {}
    for tag in _HIDDEN_INPUT_RE.findall(form_html):
        attributes = _attributes(tag)
        name = attributes.get("name")
        if name:
            fields[name] = attributes.get("value", "")
    for tag in re.findall(r"<input\b[^>]*>", form_html, re.IGNORECASE):
        attributes = _attributes(tag)
        name = attributes.get("name")
        if name and name not in fields and attributes.get("type", "").lower() == "hidden":
            fields[name] = attributes.get("value", "")

    image_attributes = _attributes(image_tag.group(0))
    image = _decode_data_image(image_attributes.get("src", ""))
    if not image:
        return None

    return CaptchaForm(
        action=action,
        fields=fields,
        answer_field="captchaText",
        image=image,
        page=fields.get("page", ""),
    )
