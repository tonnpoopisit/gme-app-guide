#!/usr/bin/env python3
"""
customer_guide_server.py

Public customer-facing "how to use the app" guide - a corridor switcher
(Laos / Thailand, each independently manageable), each corridor holding its
own topic list (Registration, 4-digit code, Autodebit + 3-digit code, Add
receiver, Send money, Limitations, etc.), each topic holding an
admin-managed, ordered list of images/videos/text blocks.

Two audiences, two access models:
  - Customers: every /api/content read and every /media/* file is open, no
    login at all - anyone with the link can view.
  - Admin: everything under /admin (page + /admin/api/*) requires a single
    PIN (no team/owner split needed here - there's only one privileged
    role). Auth skeleton (hash/salt/lockout/session) is the same shape as
    dashboard_server.py / sim_registration_server.py, just single-tier.

Content lives in customer_guide_content.json:
    {"corridors": [{"id", "label", "lang", "sections": [{"slug", "title",
     "blocks": [...]}]}]}
Array order *is* display order at every level (corridors, sections, blocks)
- no separate "order" field to keep in sync. Uploaded media lives under
customer_guide_uploads/<corridor-id>/<section-slug>/<uuid>.<ext> and is
served through /media/<corridor-id>/<slug>/<filename> rather than raw
static hosting, so uploads stay extension/size-validated on the way in
(same spirit as sim_registration_server.py's document-photo validation).

Each corridor has a `lang` (currently "th" or "lo") used by the admin
translate-on-demand feature (see /admin/api/translate): admin content is
typically typed in English and translated to that corridor's language via
Google Cloud Translation API, client-side script-detection skips the round
trip when the admin already typed Thai/Lao directly.

Run locally:
    py customer_guide_server.py
Then open http://127.0.0.1:5153 (customer view) - the admin PIN is printed
to the console on first run, same as the other tools. For deployment, PORT/
ADMIN_PIN/SECRET_KEY/DATA_DIR/GOOGLE_TRANSLATE_API_KEY can be supplied via
environment variables (see the Auth and __main__ sections below) instead of
the local auto-generated files.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, abort, redirect, session

PROJECT_ROOT = Path(__file__).parent
STATIC_DIR = PROJECT_ROOT / "customer_guide_static"

# DATA_DIR is where the actual content (corridors/topics/blocks JSON +
# uploaded media) lives. Locally this defaults to the project folder
# itself. On Cloud Run - where the container filesystem is wiped on every
# cold start - this gets pointed at a Cloud Storage FUSE volume mount (e.g.
# DATA_DIR=/data) so uploads and edits actually persist between deploys and
# scale-to-zero cycles. AUTH_PATH/SECRET_KEY_PATH deliberately stay off
# DATA_DIR: on Cloud Run those are set via ADMIN_PIN/SECRET_KEY env vars
# instead (see _load_or_create_auth/_load_or_create_secret_key below), so
# they never need bucket-backed storage.
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT))
CONTENT_PATH = DATA_DIR / "customer_guide_content.json"
UPLOADS_DIR = DATA_DIR / "customer_guide_uploads"
AUTH_PATH = PROJECT_ROOT / "customer_guide_auth.json"
SECRET_KEY_PATH = PROJECT_ROOT / "customer_guide_secret_key.txt"

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_VIDEO_EXT = {".mp4", ".webm", ".mov"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB - generous for a phone-shot how-to video

# Text-block styling is block-level (whole block, not per-character rich
# text) and validated against small whitelists rather than accepting raw
# HTML/CSS - the public page applies these as plain CSS properties via safe
# DOM APIs, so there's no markup ever coming from admin input to sanitize.
ALLOWED_FONT_SIZES = {"small", "normal", "large", "xlarge"}
ALLOWED_ALIGN = {"left", "center", "right"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_STYLE = {"bold": False, "italic": False, "color": None, "fontSize": "normal", "align": "left"}

ALLOWED_LANGS = {"th": "Thai", "lo": "Lao"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024  # small headroom for multipart overhead

_content_lock = threading.Lock()

# --------------------------------------------------------------------------
# Auth - single-tier PIN gate (only one privileged role exists here, unlike
# the two-tier team/owner split in dashboard_server.py / sim_registration_
# server.py). ADMIN_PIN env var lets a deploy set/rotate the PIN from the
# hosting platform's dashboard without shelling in to read a generated one
# out of console logs; otherwise falls back to the same auto-generate-and-
# print-once-to-console pattern the other internal tools use.
# --------------------------------------------------------------------------


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256((salt + pin).encode()).hexdigest()


def _generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _load_or_create_auth() -> dict:
    env_pin = os.environ.get("ADMIN_PIN")
    if env_pin:
        salt = secrets.token_hex(16)
        return {"pin_hash": _hash_pin(env_pin.strip(), salt), "pin_salt": salt}

    if AUTH_PATH.exists():
        return json.loads(AUTH_PATH.read_text())

    pin = _generate_pin()
    salt = secrets.token_hex(16)
    data = {"pin_hash": _hash_pin(pin, salt), "pin_salt": salt}
    AUTH_PATH.write_text(json.dumps(data, indent=2))
    print("=" * 64)
    print("Generated new Customer Guide admin PIN - shown only this once.")
    print(f"  Admin PIN: {pin}")
    print(f"Stored (hashed, salted) in {AUTH_PATH.name}.")
    print("To regenerate, delete that file and restart the server (or set")
    print("the ADMIN_PIN environment variable to pin it explicitly).")
    print("=" * 64)
    return data


def _load_or_create_secret_key() -> str:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key)
    return key


AUTH = _load_or_create_auth()
app.secret_key = _load_or_create_secret_key()

GOOGLE_TRANSLATE_API_KEY = os.environ.get("GOOGLE_TRANSLATE_API_KEY")

_login_attempts = {}
_login_attempts_lock = threading.Lock()
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300

# Public, unauthenticated routes: the whole customer-facing surface.
OPEN_PATH_PREFIXES = ("/media/",)
OPEN_PATHS = {"/", "/api/content", "/admin/login"}


def _is_locked_out(ip: str) -> bool:
    with _login_attempts_lock:
        cutoff = time.time() - LOGIN_WINDOW_SECONDS
        attempts = [t for t in _login_attempts.get(ip, []) if t > cutoff]
        _login_attempts[ip] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(ip: str):
    with _login_attempts_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


@app.before_request
def _require_auth():
    path = request.path
    if path in OPEN_PATHS or path.startswith(OPEN_PATH_PREFIXES):
        return None
    if not path.startswith("/admin"):
        # Any other top-level static asset the customer page needs (none
        # currently - index.html is fully self-contained - but keep the
        # public surface open by default rather than admin-only by default).
        return None
    if session.get("authenticated"):
        return None
    if path.startswith("/admin/api/"):
        return jsonify({"error": "Not authenticated"}), 401
    return redirect("/admin/login")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return send_file(str(STATIC_DIR / "admin_login.html"))

    ip = request.remote_addr
    if _is_locked_out(ip):
        return jsonify({"error": "Too many failed attempts - locked out for 5 minutes"}), 429

    data = request.get_json(force=True, silent=True) or {}
    pin = str(data.get("pin", "")).strip()

    if pin and _hash_pin(pin, AUTH["pin_salt"]) == AUTH["pin_hash"]:
        session["authenticated"] = True
        return jsonify({"ok": True})

    _record_failed_attempt(ip)
    return jsonify({"error": "Incorrect PIN"}), 401


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/admin/api/whoami")
def admin_whoami():
    return jsonify({"authenticated": bool(session.get("authenticated"))})


# --------------------------------------------------------------------------
# Content storage
# --------------------------------------------------------------------------


def _load_content() -> dict:
    if not CONTENT_PATH.exists():
        return {"corridors": []}
    return json.loads(CONTENT_PATH.read_text(encoding="utf-8"))


def _save_content(content: dict):
    CONTENT_PATH.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "item"


def _unique_slug(base_slug: str, existing_slugs: set) -> str:
    if base_slug not in existing_slugs:
        return base_slug
    n = 2
    while f"{base_slug}-{n}" in existing_slugs:
        n += 1
    return f"{base_slug}-{n}"


def _find_corridor(content: dict, corridor_id: str) -> dict | None:
    return next((c for c in content["corridors"] if c["id"] == corridor_id), None)


def _find_section(corridor: dict, slug: str) -> dict | None:
    return next((s for s in corridor["sections"] if s["slug"] == slug), None)


def _find_block(content: dict, block_id: str):
    """Returns (corridor, section, block) for the corridor/section owning
    block_id, or (None, None, None). Block ids are globally unique (uuid4),
    so this is the only lookup that needs to search every corridor."""
    for corridor in content["corridors"]:
        for section in corridor["sections"]:
            for block in section["blocks"]:
                if block["id"] == block_id:
                    return corridor, section, block
    return None, None, None


def _validate_style(raw: dict) -> dict:
    raw = raw or {}
    color = raw.get("color")
    return {
        "bold": bool(raw.get("bold", False)),
        "italic": bool(raw.get("italic", False)),
        "color": color if isinstance(color, str) and COLOR_RE.match(color) else None,
        "fontSize": raw.get("fontSize") if raw.get("fontSize") in ALLOWED_FONT_SIZES else "normal",
        "align": raw.get("align") if raw.get("align") in ALLOWED_ALIGN else "left",
    }


def _validate_link(raw_link, corridor: dict) -> str | None:
    """A link only ever points at another topic within the SAME corridor
    (picked from a dropdown, never typed in) - cross-corridor links don't
    make sense since a customer views one corridor at a time."""
    if not raw_link:
        return None
    valid_slugs = {s["slug"] for s in corridor["sections"]}
    return raw_link if raw_link in valid_slugs else None


# Only Facebook (Reels/videos) is supported today - restricting to this
# domain pattern rather than accepting arbitrary embed HTML is what makes
# _block_public_view safe to trust: the embed iframe's src is built
# server-side from a URL that's already been confirmed to point at
# facebook.com/fb.watch, never from admin-supplied markup.
EMBED_URL_RE = re.compile(r"^https://([a-z0-9-]+\.)?(facebook\.com|fb\.watch)/", re.IGNORECASE)


def _validate_embed_url(raw_url) -> str | None:
    url = str(raw_url or "").strip()
    return url if EMBED_URL_RE.match(url) else None


def _block_public_view(corridor_id: str, section_slug: str, block: dict) -> dict:
    view = {
        "id": block["id"],
        "type": block["type"],
        "caption": block.get("caption", ""),
        "link": block.get("link"),
    }
    if block["type"] == "text":
        view["text"] = block.get("text", "")
        view["style"] = block.get("style") or DEFAULT_STYLE
    elif block["type"] == "embed":
        view["embedUrl"] = block.get("embedUrl", "")
    else:
        view["url"] = f"/media/{corridor_id}/{section_slug}/{block['filename']}"
    return view


# --------------------------------------------------------------------------
# Public API - customer-facing, no auth
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return send_file(str(STATIC_DIR / "index.html"))


@app.route("/api/content")
def api_content():
    return jsonify(_content_public_view(include_hidden=False))


def _section_view(section: dict, corridor_id: str, include_hidden: bool) -> dict:
    view = {
        "slug": section["slug"],
        "title": section["title"],
        "blocks": [_block_public_view(corridor_id, section["slug"], b) for b in section["blocks"]],
    }
    if include_hidden:
        view["hidden"] = section.get("hidden", False)
    return view


def _content_public_view(include_hidden: bool = False) -> dict:
    """include_hidden=False (customer-facing /api/content) drops any
    section marked hidden entirely - not just visually, it's absent from
    the JSON so there's no link/hash a customer could use to reach it
    anyway. include_hidden=True (/admin/api/content) keeps every section,
    with a `hidden` flag, so the admin can still find and unhide them."""
    content = _load_content()
    corridors = [
        {
            "id": c["id"],
            "label": c["label"],
            "lang": c["lang"],
            "sections": [
                _section_view(s, c["id"], include_hidden)
                for s in c["sections"]
                if include_hidden or not s.get("hidden", False)
            ],
        }
        for c in content["corridors"]
    ]
    return {"corridors": corridors}


@app.route("/media/<corridor_id>/<slug>/<filename>")
def media(corridor_id, slug, filename):
    # filename is always a server-generated uuid+ext (see _save_upload), so
    # no user-controlled path segments ever reach the filesystem lookup.
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(404)
    file_path = UPLOADS_DIR / corridor_id / slug / filename
    if not file_path.is_file():
        abort(404)
    return send_file(str(file_path))


# --------------------------------------------------------------------------
# Admin UI + API
# --------------------------------------------------------------------------


@app.route("/admin")
def admin_page():
    return send_file(str(STATIC_DIR / "admin.html"))


@app.route("/admin/api/content")
def admin_api_content():
    return jsonify(_content_public_view(include_hidden=True))


@app.route("/admin/api/translate", methods=["POST"])
def admin_translate():
    """Thin proxy to Google Cloud Translation API v2, kept server-side so
    the API key never reaches the browser. Deliberately dumb: the client
    (admin.html) does its own script-detection to decide whether a
    translation is even needed (typing directly in Thai/Lao skips this
    entirely) - this endpoint just translates whatever text it's given."""
    if not GOOGLE_TRANSLATE_API_KEY:
        return jsonify({"error": "Translation isn't configured - set the GOOGLE_TRANSLATE_API_KEY environment variable"}), 501

    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", "")).strip()
    target = data.get("target")
    if not text or target not in ALLOWED_LANGS:
        return jsonify({"error": "text and a valid target ('th' or 'lo') are required"}), 400

    payload = json.dumps({"q": text, "target": target, "format": "text"}).encode("utf-8")
    req = urllib.request.Request(
        f"https://translation.googleapis.com/language/translate/v2?key={GOOGLE_TRANSLATE_API_KEY}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        translated = result["data"]["translations"][0]["translatedText"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return jsonify({"error": f"Translation API error ({e.code}): {detail[:300]}"}), 502
    except Exception as e:  # noqa: BLE001 - surfaced to the admin as-is, nothing sensitive in it
        return jsonify({"error": f"Translation request failed: {e}"}), 502

    return jsonify({"translatedText": translated})


@app.route("/admin/api/corridors", methods=["POST"])
def admin_create_corridor():
    data = request.get_json(force=True, silent=True) or {}
    label = str(data.get("label", "")).strip()
    lang = data.get("lang")
    if not label:
        return jsonify({"error": "Label is required"}), 400
    if lang not in ALLOWED_LANGS:
        return jsonify({"error": "lang must be 'th' (Thai) or 'lo' (Lao)"}), 400

    with _content_lock:
        content = _load_content()
        existing_ids = {c["id"] for c in content["corridors"]}
        cid = _unique_slug(_slugify(label), existing_ids)
        corridor = {"id": cid, "label": label, "lang": lang, "sections": []}
        content["corridors"].append(corridor)
        _save_content(content)
    return jsonify(corridor)


@app.route("/admin/api/corridors/<corridor_id>", methods=["PUT"])
def admin_rename_corridor(corridor_id):
    data = request.get_json(force=True, silent=True) or {}
    new_label = str(data.get("label", "")).strip()
    if not new_label:
        return jsonify({"error": "Label is required"}), 400

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404

        other_ids = {c["id"] for c in content["corridors"] if c["id"] != corridor_id}
        new_id = _unique_slug(_slugify(new_label), other_ids)

        if new_id != corridor_id:
            old_dir = UPLOADS_DIR / corridor_id
            new_dir = UPLOADS_DIR / new_id
            if old_dir.exists():
                old_dir.rename(new_dir)
            corridor["id"] = new_id
        corridor["label"] = new_label
        if data.get("lang") in ALLOWED_LANGS:
            corridor["lang"] = data["lang"]
        _save_content(content)
    return jsonify(corridor)


@app.route("/admin/api/corridors/<corridor_id>", methods=["DELETE"])
def admin_delete_corridor(corridor_id):
    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        if len(content["corridors"]) <= 1:
            return jsonify({"error": "Can't delete the last remaining corridor"}), 400
        content["corridors"] = [c for c in content["corridors"] if c["id"] != corridor_id]
        _save_content(content)
        shutil.rmtree(UPLOADS_DIR / corridor_id, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/admin/api/corridors/reorder", methods=["POST"])
def admin_reorder_corridors():
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order", [])

    with _content_lock:
        content = _load_content()
        current_ids = {c["id"] for c in content["corridors"]}
        if set(order) != current_ids or len(order) != len(content["corridors"]):
            return jsonify({"error": "Order must be a permutation of existing corridor ids"}), 400
        by_id = {c["id"]: c for c in content["corridors"]}
        content["corridors"] = [by_id[cid] for cid in order]
        _save_content(content)
    return jsonify({"ok": True})


@app.route("/admin/api/corridors/<corridor_id>/sections", methods=["POST"])
def admin_create_section(corridor_id):
    data = request.get_json(force=True, silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        existing_slugs = {s["slug"] for s in corridor["sections"]}
        slug = _unique_slug(_slugify(title), existing_slugs)
        section = {"slug": slug, "title": title, "blocks": []}
        corridor["sections"].append(section)
        _save_content(content)
    return jsonify(section)


@app.route("/admin/api/corridors/<corridor_id>/sections/<slug>", methods=["PUT"])
def admin_rename_section(corridor_id, slug):
    """Doubles as the visibility-toggle endpoint: `title` renames (and may
    change the slug/uploads folder, as before), `hidden` independently
    flips whether the section is dropped from the customer-facing
    /api/content. Either field alone is a valid request - the admin's
    "hide from customers" button doesn't need to resend the title."""
    data = request.get_json(force=True, silent=True) or {}
    if "title" not in data and "hidden" not in data:
        return jsonify({"error": "Nothing to update"}), 400

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        section = _find_section(corridor, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404

        if "title" in data:
            new_title = str(data["title"]).strip()
            if not new_title:
                return jsonify({"error": "Title is required"}), 400
            other_slugs = {s["slug"] for s in corridor["sections"] if s["slug"] != slug}
            new_slug = _unique_slug(_slugify(new_title), other_slugs)
            if new_slug != slug:
                old_dir = UPLOADS_DIR / corridor_id / slug
                new_dir = UPLOADS_DIR / corridor_id / new_slug
                if old_dir.exists():
                    old_dir.rename(new_dir)
                section["slug"] = new_slug
            section["title"] = new_title

        if "hidden" in data:
            section["hidden"] = bool(data["hidden"])

        _save_content(content)
    return jsonify(section)


@app.route("/admin/api/corridors/<corridor_id>/sections/<slug>", methods=["DELETE"])
def admin_delete_section(corridor_id, slug):
    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        section = _find_section(corridor, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        corridor["sections"] = [s for s in corridor["sections"] if s["slug"] != slug]
        _save_content(content)
        shutil.rmtree(UPLOADS_DIR / corridor_id / slug, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/admin/api/corridors/<corridor_id>/sections/reorder", methods=["POST"])
def admin_reorder_sections(corridor_id):
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order", [])

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        current_slugs = {s["slug"] for s in corridor["sections"]}
        if set(order) != current_slugs or len(order) != len(corridor["sections"]):
            return jsonify({"error": "Order must be a permutation of existing section slugs"}), 400
        by_slug = {s["slug"]: s for s in corridor["sections"]}
        corridor["sections"] = [by_slug[slug] for slug in order]
        _save_content(content)
    return jsonify({"ok": True})


def _ext_and_type(filename: str) -> tuple[str, str] | tuple[None, None]:
    ext = Path(filename).suffix.lower()
    if ext in ALLOWED_IMAGE_EXT:
        return ext, "image"
    if ext in ALLOWED_VIDEO_EXT:
        return ext, "video"
    return None, None


@app.route("/admin/api/corridors/<corridor_id>/sections/<slug>/media", methods=["POST"])
def admin_add_media_block(corridor_id, slug):
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file uploaded"}), 400

    ext, block_type = _ext_and_type(upload.filename)
    if block_type is None:
        return jsonify({"error": "Unsupported file type"}), 400

    caption = str(request.form.get("caption", "")).strip()

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        section = _find_section(corridor, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        link = _validate_link(request.form.get("link"), corridor)

        dest_dir = UPLOADS_DIR / corridor_id / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        upload.save(str(dest_dir / stored_name))

        block = {"id": uuid.uuid4().hex, "type": block_type, "filename": stored_name, "caption": caption, "link": link}
        section["blocks"].append(block)
        _save_content(content)
    return jsonify(_block_public_view(corridor_id, slug, block))


@app.route("/admin/api/corridors/<corridor_id>/sections/<slug>/text", methods=["POST"])
def admin_add_text_block(corridor_id, slug):
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "Text is required"}), 400
    style = _validate_style(data.get("style"))

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        section = _find_section(corridor, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        link = _validate_link(data.get("link"), corridor)
        block = {"id": uuid.uuid4().hex, "type": "text", "text": text, "caption": "", "style": style, "link": link}
        section["blocks"].append(block)
        _save_content(content)
    return jsonify(_block_public_view(corridor_id, slug, block))


@app.route("/admin/api/corridors/<corridor_id>/sections/<slug>/embed", methods=["POST"])
def admin_add_embed_block(corridor_id, slug):
    embed_url = _validate_embed_url((request.get_json(force=True, silent=True) or {}).get("url"))
    if not embed_url:
        return jsonify({"error": "Enter a valid facebook.com or fb.watch video/reel link"}), 400
    data = request.get_json(force=True, silent=True) or {}
    caption = str(data.get("caption", "")).strip()

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        section = _find_section(corridor, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        link = _validate_link(data.get("link"), corridor)
        block = {"id": uuid.uuid4().hex, "type": "embed", "embedUrl": embed_url, "caption": caption, "link": link}
        section["blocks"].append(block)
        _save_content(content)
    return jsonify(_block_public_view(corridor_id, slug, block))


@app.route("/admin/api/blocks/<block_id>", methods=["PUT"])
def admin_edit_block(block_id):
    data = request.get_json(force=True, silent=True) or {}

    with _content_lock:
        content = _load_content()
        corridor, section, block = _find_block(content, block_id)
        if block is None:
            return jsonify({"error": "Block not found"}), 404
        if block["type"] == "text" and "text" in data:
            block["text"] = str(data["text"]).strip()
        if block["type"] == "text" and "style" in data:
            block["style"] = _validate_style(data["style"])
        if block["type"] == "embed" and "embedUrl" in data:
            validated = _validate_embed_url(data["embedUrl"])
            if not validated:
                return jsonify({"error": "Enter a valid facebook.com or fb.watch video/reel link"}), 400
            block["embedUrl"] = validated
        if "caption" in data:
            block["caption"] = str(data["caption"]).strip()
        if "link" in data:
            block["link"] = _validate_link(data["link"], corridor)
        _save_content(content)
    return jsonify(_block_public_view(corridor["id"], section["slug"], block))


@app.route("/admin/api/blocks/<block_id>", methods=["DELETE"])
def admin_delete_block(block_id):
    with _content_lock:
        content = _load_content()
        corridor, section, block = _find_block(content, block_id)
        if block is None:
            return jsonify({"error": "Block not found"}), 404
        section["blocks"] = [b for b in section["blocks"] if b["id"] != block_id]
        _save_content(content)
        if block["type"] in ("image", "video"):
            (UPLOADS_DIR / corridor["id"] / section["slug"] / block["filename"]).unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/admin/api/corridors/<corridor_id>/sections/<slug>/blocks/reorder", methods=["POST"])
def admin_reorder_blocks(corridor_id, slug):
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order", [])

    with _content_lock:
        content = _load_content()
        corridor = _find_corridor(content, corridor_id)
        if corridor is None:
            return jsonify({"error": "Corridor not found"}), 404
        section = _find_section(corridor, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        current_ids = {b["id"] for b in section["blocks"]}
        if set(order) != current_ids or len(order) != len(section["blocks"]):
            return jsonify({"error": "Order must be a permutation of existing block ids"}), 400
        by_id = {b["id"]: b for b in section["blocks"]}
        section["blocks"] = [by_id[bid] for bid in order]
        _save_content(content)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5153))
    app.run(host="0.0.0.0", port=port)
