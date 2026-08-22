---
name: customer-guide-deploy
description: Deploys/maintains the GME customer app guide (customer_guide_server.py, a Flask app teaching customers how to use the GME remittance app - registration, 4-digit code, autodebit+3-digit code, add receiver, send money, limitations) to Google Cloud Run, backed by a Cloud Storage bucket for persistent data. Covers the full first-deploy runbook (enable APIs, create bucket, migrate content, grant IAM, deploy) plus every gotcha hit getting gcloud working on Windows (ESET SSL interception, PowerShell execution policy blocking gcloud.ps1, setx's 1024-char truncation, Cloud Run's 32MB HTTP/1 upload limit). Use this skill whenever the user asks to deploy, redeploy, or troubleshoot this app on Cloud Run, asks about its Cloud Storage-backed data layer, or hits gcloud CLI setup problems on Windows.
---

# Customer Guide - Cloud Run Deployment

## What this app is

A public, no-login customer-facing site (`customer_guide_server.py` + `customer_guide_static/`) with an admin CMS behind a single PIN, teaching GME app customers how to register, use the 4-digit verification code, set up autodebit (3-digit code), add a receiver, send money, and understand limits. Admin can add/rename/reorder topics and upload/reorder/caption images and videos, with block-level text styling (bold/italic/size/color/align) and cross-topic links. See the code comments in `customer_guide_server.py` for the full data model.

## Architecture: why Cloud Run needs a storage change

Cloud Run containers are stateless - local disk writes don't survive a redeploy or a scale-to-zero/cold-start cycle. The app's two pieces of real state:

- **Content** (`customer_guide_content.json` - topics + ordered blocks)
- **Uploaded media** (`customer_guide_uploads/<slug>/<uuid>.<ext>`)

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

3. **Migrate existing content into it** (one-time, only needed if there's pre-existing local content from testing before the app moved to Cloud Run):
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
   gcloud.cmd run deploy app-guide --source . --region=REGION --allow-unauthenticated --memory=1Gi --max-instances=1 --add-volume=name=data,type=cloud-storage,bucket=BUCKET_NAME --add-volume-mount=volume=data,mount-path=/data --set-env-vars=DATA_DIR=/data,ADMIN_PIN=<pick-a-fresh-pin>,SECRET_KEY=<generate-fresh: python -c "import secrets; print(secrets.token_hex(32))">
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

## Progress as of last session (2026-08-22, work PC with ESET)

- GCP project created: `gme-related-project` (number `217749099623`), personal (not GME-org) Google account.
- Got as far as `gcloud config list` showing the account correctly, but `project` wasn't persisted and `gcloud config set project ...` then hit the stricter ESET cert error.
- Bucket/IAM/migrate/deploy commands above were prepared but **not yet run**.
- User is switching to a personal PC (presumably without ESET) to continue - try the plain runbook there first; only pull in the gotchas section if the same errors resurface.
