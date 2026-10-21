---
title: SkinTrack
sdk: gradio
sdk_version: 6.30.0
app_file: app.py
pinned: false
---

# SkinTrack

SkinTrack is a single-file Python + Gradio prototype for consent-based skin-photo review, private self-reported profiles, saved analysis history, KPI trends, and explainable routine suggestions. It is not a medical product and does not train or fine-tune a model.

## Requirements

- macOS with Python 3.10 or later
- An HKBU API key for real image analysis
- Two test-account username/password pairs for authenticated app launch

## Install and run on macOS

```bash
cd "/Users/quyichen/Desktop/y4s1/Comp4145/Project/Skin"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Edit `.env` locally and set both `SKINTRACK_USER1` / `SKINTRACK_PASSWORD1` and `SKINTRACK_USER2` / `SKINTRACK_PASSWORD2`. The application requires two distinct configured accounts before launch. You can set `HKBU_API_KEY` in `.env` or paste it into the masked API key field under Skin Analysis for an individual request. Never commit `.env`. The application loads `.env` automatically when started with the command below.

```bash
python app.py
```

Use the local URL printed by Gradio and sign in with one of the configured accounts.

## API integration and privacy

Alternatively, enter your API key in the masked Skin Analysis field. A key entered there is used only for that analysis request, is not saved to SQLite, logs, or project files, and the input clears after the request. Live API connectivity has not been verified.

The app uses the requested HKBU Gemini 2.5 Pro deployment URL by default and reads `HKBU_API_KEY` / optional `HKBU_API_URL` from the environment. The workspace supplied no API integration example; the request currently assumes an OpenAI-compatible chat-completions gateway: `Authorization: Bearer ...`, JSON `messages`, and a multimodal `image_url` data URL. Confirm this wire format and the exact response contract with HKBU before relying on real analysis. An API key was not available during this implementation, so live connectivity is not claimed.

Photos are checked (JPEG, PNG, WebP; maximum 8 MB), sent only after explicit consent, and are not stored in SQLite. Temporary upload files are removed after analysis/clear where possible. API failures never generate placeholder scores. Estimated age is a rough, unreliable model estimate. All scores are validated as numeric values from 0 through 10; missing values display as “Unable to read”.

## Accounts and stored data

Set two unique username/password pairs in the deployment environment. Gradio authentication provides login; every profile/history operation obtains the user from the authenticated Gradio request and uses parameterized SQL with a username filter. This is a classroom prototype, not hardened production authentication: use strong secrets, HTTPS, access controls, and a managed database before public/production use.

SQLite uses `skintrack.db` in the application directory by default and is excluded from version control. Local restarts preserve records. A typical ephemeral Gradio cloud filesystem does **not** guarantee database persistence through rebuilds/restarts. For persistence, mount or provision durable storage and set `SKINTRACK_DB_PATH` to that mounted path; otherwise treat cloud history as temporary. Do not deploy a real user's database or private photo.

## Deploy to Hugging Face Spaces (Gradio)

1. Create a private/public Gradio Space using the Gradio SDK and Python.
2. Upload `app.py`, `requirements.txt`, and `README.md`; do not upload `.env`, `skintrack.db`, or photos.
3. Add `HKBU_API_KEY`, `HKBU_API_URL`, and both account username/password pairs as Space secrets.
4. Deploy, open the Space URL, sign in with a test account, and verify the app before sharing. Confirm durable disk availability separately if history must survive restarts.

No cloud deployment was performed for this deliverable; therefore no live platform link is available yet. Real deployment also requires an account, repository/Space, and API credentials.

## Scope and limitations

- Three added functions: account-scoped profile, user-controlled analysis history/trends, and a profile-driven rules-based routine.
- No open registration, password reset, e-commerce, medical diagnosis, image model training, or efficacy claims.
- Trends represent model output variation, not proof of clinical improvement.
- This prototype's login passwords are managed by Gradio's built-in authentication; production use requires a security and privacy review.
