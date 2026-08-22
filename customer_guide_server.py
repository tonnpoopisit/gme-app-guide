#!/usr/bin/env python3
"""
customer_guide_server.py

Public customer-facing "how to use the app" guide - sidebar of topics
(Registration, 4-digit code, Autodebit + 3-digit code, Add receiver, Send
money, Limitations), each holding an admin-managed, ordered list of
images/videos/text blocks.

Two audiences, two access models:
  - Customers: every /api/content read and every /media/* file is open, no
    login at all - anyone with the link can view.
  - Admin: everything under /admin (page + /admin/api/*) requires a single
    PIN (no team/owner split needed here - there's only one privileged
    role). Auth skeleton (hash/salt/lockout/session) is the same shape as
    dashboard_server.py / sim_registration_server.py, just single-tier.

Content lives in customer_guide_content.json (section list, each with an
ordered blocks list - array order *is* display order, no separate "order"
field to keep in sync). Uploaded media lives under
customer_guide_uploads/<section-slug>/<uuid>.<ext> and is served through
/media/<slug>/<filename> rather than raw static hosting, so uploads stay
extension/size-validated on the way in (same spirit as
sim_registration_server.py's document-photo validation).

Run locally:
    py customer_guide_server.py
Then open http://127.0.0.1:5153 (customer view) - the admin PIN is printed
to the console on first run, same as the other tools. For deployment, PORT/
ADMIN_PIN/SECRET_KEY can be supplied via environment variables (see the
Auth and __main__ sections below) instead of the local auto-generated files.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, abort, redirect, session

PROJECT_ROOT = Path(__file__).parent
STATIC_DIR = PROJECT_ROOT / "customer_guide_static"

# DATA_DIR is where the actual content (topics/blocks JSON + uploaded media)
# lives. Locally this defaults to the project folder itself, same as
# before. On Cloud Run - where the container filesystem is wiped on every
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
        return {"sections": []}
    return json.loads(CONTENT_PATH.read_text(encoding="utf-8"))


def _save_content(content: dict):
    CONTENT_PATH.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "section"


def _unique_slug(base_slug: str, existing_slugs: set) -> str:
    if base_slug not in existing_slugs:
        return base_slug
    n = 2
    while f"{base_slug}-{n}" in existing_slugs:
        n += 1
    return f"{base_slug}-{n}"


def _find_section(content: dict, slug: str) -> dict | None:
    return next((s for s in content["sections"] if s["slug"] == slug), None)


def _find_block(content: dict, block_id: str) -> tuple[dict, dict] | tuple[None, None]:
    """Returns (section, block) for the section owning block_id, or (None, None)."""
    for section in content["sections"]:
        for block in section["blocks"]:
            if block["id"] == block_id:
                return section, block
    return None, None


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


def _validate_link(raw_link, content: dict) -> str | None:
    """A link only ever points at one of THIS guide's own topic sections
    (picked from a dropdown, never typed in) - so the only validation
    needed is confirming that section still exists, not general URL
    sanitization."""
    if not raw_link:
        return None
    valid_slugs = {s["slug"] for s in content["sections"]}
    return raw_link if raw_link in valid_slugs else None


def _block_public_view(section_slug: str, block: dict) -> dict:
    view = {
        "id": block["id"],
        "type": block["type"],
        "caption": block.get("caption", ""),
        "link": block.get("link"),
    }
    if block["type"] == "text":
        view["text"] = block.get("text", "")
        view["style"] = block.get("style") or DEFAULT_STYLE
    else:
        view["url"] = f"/media/{section_slug}/{block['filename']}"
    return view


# --------------------------------------------------------------------------
# Public API - customer-facing, no auth
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return send_file(str(STATIC_DIR / "index.html"))


@app.route("/api/content")
def api_content():
    return jsonify(_content_public_view())


def _content_public_view() -> dict:
    content = _load_content()
    sections = [
        {
            "slug": s["slug"],
            "title": s["title"],
            "blocks": [_block_public_view(s["slug"], b) for b in s["blocks"]],
        }
        for s in content["sections"]
    ]
    return {"sections": sections}


@app.route("/media/<slug>/<filename>")
def media(slug, filename):
    # filename is always a server-generated uuid+ext (see _save_upload), so
    # no user-controlled path segments ever reach the filesystem lookup.
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(404)
    file_path = UPLOADS_DIR / slug / filename
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
    return jsonify(_content_public_view())


@app.route("/admin/api/sections", methods=["POST"])
def admin_create_section():
    data = request.get_json(force=True, silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    with _content_lock:
        content = _load_content()
        existing_slugs = {s["slug"] for s in content["sections"]}
        slug = _unique_slug(_slugify(title), existing_slugs)
        section = {"slug": slug, "title": title, "blocks": []}
        content["sections"].append(section)
        _save_content(content)
    return jsonify(section)


@app.route("/admin/api/sections/<slug>", methods=["PUT"])
def admin_rename_section(slug):
    data = request.get_json(force=True, silent=True) or {}
    new_title = str(data.get("title", "")).strip()
    if not new_title:
        return jsonify({"error": "Title is required"}), 400

    with _content_lock:
        content = _load_content()
        section = _find_section(content, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404

        other_slugs = {s["slug"] for s in content["sections"] if s["slug"] != slug}
        new_slug = _unique_slug(_slugify(new_title), other_slugs)

        if new_slug != slug:
            old_dir = UPLOADS_DIR / slug
            new_dir = UPLOADS_DIR / new_slug
            if old_dir.exists():
                old_dir.rename(new_dir)
            section["slug"] = new_slug
        section["title"] = new_title
        _save_content(content)
    return jsonify(section)


@app.route("/admin/api/sections/<slug>", methods=["DELETE"])
def admin_delete_section(slug):
    with _content_lock:
        content = _load_content()
        section = _find_section(content, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        content["sections"] = [s for s in content["sections"] if s["slug"] != slug]
        _save_content(content)
        shutil.rmtree(UPLOADS_DIR / slug, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/admin/api/sections/reorder", methods=["POST"])
def admin_reorder_sections():
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order", [])

    with _content_lock:
        content = _load_content()
        current_slugs = {s["slug"] for s in content["sections"]}
        if set(order) != current_slugs or len(order) != len(content["sections"]):
            return jsonify({"error": "Order must be a permutation of existing section slugs"}), 400
        by_slug = {s["slug"]: s for s in content["sections"]}
        content["sections"] = [by_slug[slug] for slug in order]
        _save_content(content)
    return jsonify({"ok": True})


def _ext_and_type(filename: str) -> tuple[str, str] | tuple[None, None]:
    ext = Path(filename).suffix.lower()
    if ext in ALLOWED_IMAGE_EXT:
        return ext, "image"
    if ext in ALLOWED_VIDEO_EXT:
        return ext, "video"
    return None, None


@app.route("/admin/api/sections/<slug>/media", methods=["POST"])
def admin_add_media_block(slug):
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file uploaded"}), 400

    ext, block_type = _ext_and_type(upload.filename)
    if block_type is None:
        return jsonify({"error": "Unsupported file type"}), 400

    caption = str(request.form.get("caption", "")).strip()

    with _content_lock:
        content = _load_content()
        section = _find_section(content, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        link = _validate_link(request.form.get("link"), content)

        dest_dir = UPLOADS_DIR / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        upload.save(str(dest_dir / stored_name))

        block = {"id": uuid.uuid4().hex, "type": block_type, "filename": stored_name, "caption": caption, "link": link}
        section["blocks"].append(block)
        _save_content(content)
    return jsonify(_block_public_view(slug, block))


@app.route("/admin/api/sections/<slug>/text", methods=["POST"])
def admin_add_text_block(slug):
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "Text is required"}), 400
    style = _validate_style(data.get("style"))

    with _content_lock:
        content = _load_content()
        section = _find_section(content, slug)
        if section is None:
            return jsonify({"error": "Section not found"}), 404
        link = _validate_link(data.get("link"), content)
        block = {"id": uuid.uuid4().hex, "type": "text", "text": text, "caption": "", "style": style, "link": link}
        section["blocks"].append(block)
        _save_content(content)
    return jsonify(_block_public_view(slug, block))


@app.route("/admin/api/blocks/<block_id>", methods=["PUT"])
def admin_edit_block(block_id):
    data = request.get_json(force=True, silent=True) or {}

    with _content_lock:
        content = _load_content()
        section, block = _find_block(content, block_id)
        if block is None:
            return jsonify({"error": "Block not found"}), 404
        if block["type"] == "text" and "text" in data:
            block["text"] = str(data["text"]).strip()
        if block["type"] == "text" and "style" in data:
            block["style"] = _validate_style(data["style"])
        if "caption" in data:
            block["caption"] = str(data["caption"]).strip()
        if "link" in data:
            block["link"] = _validate_link(data["link"], content)
        _save_content(content)
    return jsonify(_block_public_view(section["slug"], block))


@app.route("/admin/api/blocks/<block_id>", methods=["DELETE"])
def admin_delete_block(block_id):
    with _content_lock:
        content = _load_content()
        section, block = _find_block(content, block_id)
        if block is None:
            return jsonify({"error": "Block not found"}), 404
        section["blocks"] = [b for b in section["blocks"] if b["id"] != block_id]
        _save_content(content)
        if block["type"] in ("image", "video"):
            (UPLOADS_DIR / section["slug"] / block["filename"]).unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/admin/api/sections/<slug>/blocks/reorder", methods=["POST"])
def admin_reorder_blocks(slug):
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order", [])

    with _content_lock:
        content = _load_content()
        section = _find_section(content, slug)
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
