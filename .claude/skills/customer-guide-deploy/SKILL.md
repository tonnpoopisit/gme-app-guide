---
name: customer-guide-deploy
description: Deploys/maintains the GME customer app guide (customer_guide_server.py, a Flask app teaching customers how to use the GME remittance app - registration, 4-digit code, autodebit+3-digit code, add receiver, send money, limitations - across multiple corridors, currently Laos and Thailand) to Google Cloud Run, backed by a Cloud Storage bucket for persistent data. Covers the full first-deploy runbook (enable APIs, create bucket, migrate content, grant IAM, deploy), the admin's English-to-Thai/Lao translate-on-demand feature (Google Cloud Translation API, needs GOOGLE_TRANSLATE_API_KEY), per-topic hide-from-customers toggling, Facebook video/reel embedding, the custom confirm/prompt modals (native window.confirm/prompt are unreliable here - see below), plus every gotcha hit getting gcloud working on Windows (ESET SSL interception, PowerShell execution policy blocking gcloud.ps1, setx's 1024-char truncation, Cloud Run's 32MB HTTP/1 upload limit). Use this skill whenever the user asks to deploy, redeploy, or troubleshoot this app on Cloud Run, asks about corridors/translation/hidden topics/Facebook embeds, its Cloud Storage-backed data layer, or hits gcloud CLI setup problems on Windows.
---

# Customer Guide - Cloud Run Deployment

## What this app is

A public, no-login customer-facing site (`customer_guide_server.py` + `customer_guide_static/`) with an admin CMS behind a single PIN, teaching GME app customers how to register, use the 4-digit verification code, set up autodebit (3-digit code), add a receiver, send money, and understand limits. Content is organized into **corridors** (currently Laos and Thailand, each with its own language - `lo`/`th`) - customers switch between them with a top-of-page toggle; each corridor has its own independent topic list, and each topic can be individually hidden from customers (still visible/editable in admin, badge-marked) while it's being prepared. Admin can add/rename/reorder both corridors and topics, upload/reorder/caption images and videos, embed Facebook videos/reels by pasting a link (validated against facebook.com/fb.watch, rendered client-side as a responsive 9:16 iframe via Facebook's `/plugins/video.php` embed - no Facebook SDK/app id needed), block-level text styling (bold/italic/size/color/align), cross-topic links (scoped within a corridor), and a **Translate** button on every text field (title/caption/text) that sends admin-typed English through Google Cloud Translation API into that corridor's language - skipped automatically (client-side Unicode script detection) if the admin already typed Thai/Lao directly. See the code comments in `customer_guide_server.py` for the full data model.

## Data model

```json
{"corridors": [
  {"id": "laos", "label": "Laos", "lang": "lo", "sections": [
    {"slug": "registration", "title": "Registration", "hidden": false, "blocks": [
      {"id": "...", "type": "image|video|text|embed", "caption": "...", "link": null,
       "filename": "... (image/video only)", "text": "... (text only)", "style": {"... (text only)"},
       "embedUrl": "https://facebook.com/... (embed only)"}
    ]}
  ]},
  {"id": "thailand", "label": "Thailand", "lang": "th", "sections": [...]}
]}
```

Array order *is* display order at every level (corridors, sections, blocks) - no separate "order" field. Corridor/section renames regenerate the `id`/`slug` and physically move the corresponding `customer_guide_uploads/` subfolder - see `admin_rename_corridor`/`admin_rename_section` in `customer_guide_server.py`. `hidden` sections are dropped entirely from `/api/content` (customer-facing) but kept (with the flag) in `/admin/api/content` - see `_content_public_view(include_hidden=...)`.

## Native browser dialogs are unreliable here - don't reintroduce them

`window.confirm()`/`window.prompt()` looked broken (delete/rename buttons silently did nothing) because Chrome lets a page's dialogs get suppressed after a few fire in a row ("Prevent this page from creating additional dialogs"), and this app's admin panel triggers enough of them in normal use to hit that. Both were replaced with custom in-page modals - `showConfirm(message)` / `showPrompt(message, defaultValue)` in `admin.html`, returning Promises. **Any new destructive action or rename-style input must use these, not the native `confirm`/`prompt`**, or the same silent-failure bug comes back.

## Editing an existing block auto-saves - no Save button

Text/caption/embed-URL/style/link fields on an *existing* block save themselves: typing debounces to a save ~900ms after the last keystroke, and losing focus (blur) flushes immediately so nothing is left pending if the admin clicks away. Each block card shows an inline status (`Saving…` / `Saved` / `Not saved: <error>` on failure) instead of a Save button - see `makeDebouncedSave`/`autoSaveBlock` in `admin.html`. Deliberately does **not** call `loadContent()`/re-render on save (that would steal focus out of the field being typed in) - it PUTs the change and mutates the in-memory `block` object in place instead. The "Add ..." forms (creating a *new* block/topic/corridor) still use an explicit submit button - auto-save only applies to editing something that already exists.

`_save_content()` retries a few times on `OSError` before giving up - seen live as a real 500 on a block reorder while a content.json backup zip was being built concurrently (Windows transiently blocks a write while something else - antivirus, a backup tool - has the file open; usually clears within milliseconds). Keep this in mind if scripting anything that reads `customer_guide_content.json` directly (e.g. the data-zip step) while the server might be live - it's now resilient to a stray collision, but avoid making it a *frequent* one.

The admin header's "View public site →" link deep-links to whatever corridor/topic is currently selected in admin (`#<corridorId>/<slug>`, kept in sync by `updatePublicLink()`) rather than always opening the site root - makes it fast to cross-check what a specific edit actually looks like live.

**Gotcha already hit once**: the style toolbar's preset buttons (Bold/Italic/align/**Default color**) are `<button>`s that mutate `style` on `click`, not `input`/`change` - the auto-save listeners on `toolbar.element` must include `click` too, or clicking those buttons updates what's shown in admin but never actually saves it. Missing this exact listener is what caused a real bug: an admin set an explicit near-black text color, later clicked "Default color" to undo it, and the old color stayed live on the public site (invisible against the dark theme) because the click was never persisted. Custom colors also aren't theme-aware - a color picked while looking at light mode can be unreadable in dark mode or vice versa, so prefer "Default color" over a custom pick unless you've checked both.

## Enabling translation

Not required to deploy/run the app - without `GOOGLE_TRANSLATE_API_KEY` set, the Translate button just shows a clear "not configured" error and everything else works normally. To enable it:

1. In the GCP Console (no gcloud CLI needed - just the browser, sidesteps the ESET issues below entirely): APIs & Services → Library → enable **Cloud Translation API** (may prompt to attach a billing account - free tier covers a lot of typical admin usage).
2. APIs & Services → Credentials → Create Credentials → API key. Optionally restrict it to just the Cloud Translation API.
3. Set `GOOGLE_TRANSLATE_API_KEY=<the key>` as an env var - locally for testing, and add it to the `--set-env-vars` list in the Cloud Run deploy command below for production.

Endpoint: `POST /admin/api/translate` `{text, target: "th"|"lo"}` -> `{translatedText}`, a thin server-side proxy so the key never reaches the browser (see `admin_translate` in `customer_guide_server.py`).

## Architecture: why Cloud Run needs a storage change

Cloud Run containers are stateless - local disk writes don't survive a redeploy or a scale-to-zero/cold-start cycle. The app's two pieces of real state:

- **Content** (`customer_guide_content.json` - corridors -> topics -> ordered blocks)
- **Uploaded media** (`customer_guide_uploads/<corridor-id>/<section-slug>/<uuid>.<ext>`)

...are controlled by `DATA_DIR`, an env var read once at startup:

```python
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT))
CONTENT_PATH = DATA_DIR / "customer_guide_content.json"
UPLOADS_DIR = DATA_DIR / "customer_guide_uploads"
```

Locally (no `DATA_DIR` set) this defaults to the project folder - identical behavior to before this change. On Cloud Run, `DATA_DIR=/data` points at a **Cloud Storage bucket mounted via Cloud Storage FUSE** (`--add-volume`/`--add-volume-mount`), so the app's plain `open()`/`Path.write_text()` calls transparently persist to the bucket with zero other code changes.

`AUTH_PATH`/`SECRET_KEY_PATH` (admin PIN hash, Flask session key) deliberately stay off `DATA_DIR` - on Cloud Run these are set directly via `ADMIN_PIN`/`SECRET_KEY` env vars instead (the code already prefers env vars over the local-file fallback), so they never need bucket-backed storage.

**Known GCS FUSE limitations** (acceptable at this app's scale, worth knowing): no cross-instance file locking (last write wins - mitigated by `--max-instances=1`, see below), and writing a file fully stages it in memory (relevant for large video uploads - see the 32MB note below).

## First-deploy runbook

Placeholders below: `PROJECT_ID`, `PROJECT_NUMBER`, `REGION` (recommended `asia-southeast1` - Singapore, good middle ground for Thai/Lao customers), `BUCKET_NAME` (must be globally unique - using the project ID as a prefix guarantees this, e.g. `PROJECT_ID-guide-data`).

1. **Enable required APIs:**
   ```powershell
   gcloud.cmd services enable run.googleapis.com storage.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project=PROJECT_ID
   ```

2. **Create the bucket:**
   ```powershell
   gcloud.cmd storage buckets create gs://BUCKET_NAME --project=PROJECT_ID --location=REGION
   ```

3. **Migrate existing content into it** (one-time, only needed if there's pre-existing local content from testing before the app moved to Cloud Run - `customer_guide_uploads/` is corridor-nested now, e.g. `customer_guide_uploads/laos/registration/...`, so this copies the whole tree as-is):
   ```powershell
   gcloud.cmd storage cp "customer_guide_content.json" gs://BUCKET_NAME/customer_guide_content.json
   gcloud.cmd storage cp -r "customer_guide_uploads\*" gs://BUCKET_NAME/customer_guide_uploads/
   ```

4. **Grant the Cloud Run service account read-write access to the bucket:**
   ```powershell
   gcloud.cmd storage buckets add-iam-policy-binding gs://BUCKET_NAME --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role="roles/storage.objectUser"
   ```

5. **Deploy** (run from inside this repo - `.gcloudignore` scopes what gets uploaded as build context):
   ```powershell
   gcloud.cmd run deploy app-guide --source . --region=REGION --allow-unauthenticated --memory=1Gi --max-instances=1 --add-volume=name=data,type=cloud-storage,bucket=BUCKET_NAME --add-volume-mount=volume=data,mount-path=/data --set-env-vars=DATA_DIR=/data,ADMIN_PIN=<pick-a-fresh-pin>,SECRET_KEY=<generate-fresh: python -c "import secrets; print(secrets.token_hex(32))">,GOOGLE_TRANSLATE_API_KEY=<optional-see-below>
   ```
   - `--max-instances=1`: sidesteps GCS FUSE's lack of cross-instance write locking - fine for this traffic level (Cloud Run still handles many concurrent requests within one instance).
   - `--memory=1Gi`: GCS FUSE stages writes fully in memory; default 512Mi is too tight once video uploads are in play.
   - Never reuse a PIN/secret that's appeared in a chat transcript or another deploy - generate fresh ones each time this command is run for real.

Buildpacks auto-detect Python from `requirements.txt`, see `gunicorn` in it, and use the `Procfile`'s `web:` line as the entrypoint - no Dockerfile needed. Confirmed this repo's `requirements.txt` + `Procfile` combo matches exactly what buildpacks expect.

## Known follow-up: large video uploads

Cloud Run caps HTTP/1 request bodies at 32MB (no such cap on responses that stream, so **serving** existing videos back to customers via `send_file` is fine either way). A plain browser `FormData` upload through the admin panel risks hitting that 32MB request cap for anything but short/low-bitrate videos. Not yet fixed - the proper fix is a direct-to-bucket upload using a GCS signed URL (browser uploads straight to the bucket; only the small metadata POST goes through Cloud Run), which needs `roles/iam.serviceAccountTokenCreator` granted to the runtime service account plus new endpoints in `customer_guide_server.py` to mint the signed URL and register the resulting block. Do this if/when the admin actually needs to upload a video over ~25-30MB.

## Windows gcloud CLI setup gotchas (all hit and solved once already - don't re-derive)

These all showed up getting `gcloud` working on a Windows machine running **ESET Security**:

1. **`winget install Google.CloudSDK` partially fails** (exit code 2, no admin prompt completion) but still extracts the SDK to `%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk` - just incomplete (no PATH, no shims). Don't bother re-running winget; either finish via `install.bat` or use the bundled-zip approach below.

2. **Plain `gcloud` in PowerShell fails**: `running scripts is disabled on this system` - PowerShell resolves bare `gcloud` to `gcloud.ps1`, which the default execution policy blocks. **Always use `gcloud.cmd`**, not `gcloud`, in PowerShell.

3. **`setx PATH "..."` silently truncates** - it has a hidden ~1024-character limit and a real dev machine's PATH is usually already near/over that. Use this instead (no length limit, same effect):
   ```powershell
   [System.Environment]::SetEnvironmentVariable('Path', [System.Environment]::GetEnvironmentVariable('Path','User') + ';C:\path\to\gcloud\bin', 'User')
   ```

4. **The big one - ESET SSL/TLS protocol scanning breaks gcloud's Python HTTP client.** Symptom: `SSLCertVerificationError: self-signed certificate in certificate chain` (or later, a stricter variant: `CA cert does not include key usage extension`) on every call to a `*.googleapis.com`/`dl.google.com` host - both during `install.bat`'s own component-fetch step and during real `gcloud` commands (`init`, `config set project`, etc.). Root cause: ESET intercepts HTTPS with its own certificate for content inspection; Windows/schannel/curl tolerate it, but Python's stricter OpenSSL-based validation (which gcloud's bundled Python uses) rejects it.

   **Two fixes, in order of preference:**
   - **Clean fix**: in ESET, Advanced setup (F5) → Web and Email → SSL/TLS → "List of applications excluded from SSL/TLS filtering" → add gcloud's bundled Python executable (find it with `Get-ChildItem -Path "<gcloud-sdk-path>\platform\bundledpython" -Filter "python.exe" -Recurse`). This was identified as the right fix but not yet completed/verified - **do this first on a fresh machine before falling back to the workaround below.**
   - **Workaround** (used to get as far as project creation before switching machines): export everything Windows already trusts to a PEM bundle and point gcloud at it, permanently:
     ```powershell
     $pemPath = "$env:USERPROFILE\gcloud-ca-bundle.pem"
     Remove-Item $pemPath -ErrorAction SilentlyContinue
     Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root | ForEach-Object {
         $b64 = [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks')
         "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----" | Add-Content -Path $pemPath -Encoding ascii
     }
     [System.Environment]::SetEnvironmentVariable('CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE', $pemPath, 'User')
     $env:CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE = $pemPath
     ```
     This got login and `gcloud init` (project creation) working, but later hit the *stricter* "CA cert does not include key usage extension" variant on `gcloud config set project` - meaning the workaround is incomplete/fragile against ESET's cert specifically. If this recurs, try the ESET exclusion instead of continuing to patch the cert bundle.

5. **`gcloud run deploy --source .`** uploads the whole current directory as build context (minus `.gcloudignore` exclusions) - if this repo ever gets nested inside a larger working directory that has unrelated secrets/files again, re-add a deny-all-then-allow-list `.gcloudignore` like this repo's, don't rely on `.gitignore` semantics alone.

## Progress as of last session (2026-08-24, work PC with ESET)

- GCP project created: `gme-related-project` (number `217749099623`), personal (not GME-org) Google account.
- Got as far as `gcloud config list` showing the account correctly, but `project` wasn't persisted and `gcloud config set project ...` then hit the stricter ESET cert error.
- Bucket/IAM/migrate/deploy commands above were prepared but **not yet run** - still true, no Cloud Run deploy has happened yet as of this update.
- Corridors + translation shipped locally since then (this repo is up to date) - the local `customer_guide_content.json`/`customer_guide_uploads/` on the work PC now has real Laos data (12 topics, 44 blocks) plus an empty, ready-to-fill Thailand corridor mirroring the same 12 topic names. That data was zipped separately (not in this repo - see chat history) for transfer to the personal PC; migrate it into the bucket at step 3 above once gcloud is working there.
- Translation not yet enabled (no `GOOGLE_TRANSLATE_API_KEY` set anywhere) - the button is fully wired up client- and server-side, just needs the key (see "Enabling translation" above) whenever that's wanted.
- User is switching to a personal PC (presumably without ESET) to continue - try the plain runbook there first; only pull in the gotchas section if the same errors resurface.
