# SkinTrack

SkinTrack is a single-file Python + Streamlit prototype for consent-based skin-photo review, private self-reported profiles, saved analysis history, KPI trends, and explainable routine suggestions. It is not a medical product and does not train or fine-tune a model.

## Install and run on macOS

```bash
cd "/Users/quyichen/Desktop/y4s1/Comp4145/Project/Skin"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Edit `.env` locally and set both account username/password pairs. You may also set `HKBU_API_KEY`; otherwise enter it in the masked field on the Skin Analysis page. Never commit `.env`.

```bash
streamlit run app.py
```

Open the local URL printed by Streamlit and sign in with one of the configured accounts.

## API integration and privacy

The requested HKBU Gemini 2.5 Pro deployment URL is used by default. Configure `HKBU_API_KEY` and optional `HKBU_API_URL` through environment variables or Streamlit Secrets, or enter the key in the masked Skin Analysis field. A key entered in the field is used only for that analysis request and is not saved to SQLite or project files.

The workspace contained no school API example. The request therefore assumes an OpenAI-compatible chat-completions gateway, Bearer authentication, JSON `messages`, and a multimodal `image_url` data URL. Confirm this wire format and the exact response contract with HKBU before relying on real analysis.

Photos are checked (JPEG, PNG, WebP; maximum 8 MB), sent only after explicit consent, and are not stored in SQLite. Temporary upload files are removed after analysis. API failures never generate placeholder scores. Estimated age is a rough, unreliable model estimate. Scores must be numeric values from 0 through 10; missing values display as “Unable to read”.

## Accounts and stored data

Set two unique username/password pairs in local `.env` or Streamlit Secrets. The app validates credentials server-side and keeps the authenticated username in the Streamlit session. Profile/history operations use that identity and parameterized SQL with a username filter. This is a classroom prototype, not hardened production authentication.

SQLite uses `skintrack.db` in the application directory by default and is excluded from version control. Local restarts preserve records. Streamlit Community Cloud's local filesystem is generally ephemeral; database history may disappear after restart/redeploy. Use a managed database or durable external storage if records must persist. Do not upload a local user database or private photos.

## Deploy to Streamlit Community Cloud

1. Push this project to GitHub. Do not upload `.env`, `skintrack.db`, `.venv`, or private photos.
2. In Streamlit Community Cloud, create an app from the repository, choose branch `main`, set **Main file path** to `app.py`, then deploy.
3. Open the app's **Settings → Secrets** and add the following TOML, replacing the example account values with unique strong credentials:

   ```toml
   HKBU_API_KEY = "your-HKBU-key"
   SKINTRACK_USER1 = "first-test-user"
   SKINTRACK_PASSWORD1 = "use-a-long-unique-password"
   SKINTRACK_USER2 = "second-test-user"
   SKINTRACK_PASSWORD2 = "use-another-long-unique-password"
   # Optional URL override:
   # HKBU_API_URL = "https://genai.hkbu.edu.hk/api/v0/rest/deployments/gemini-2.5-pro/chat/completions?api-version=v1"
   ```

4. Save secrets and reboot the app. Sign in and run a consented test. If it fails to start, inspect **Manage app → Logs**.

## Scope and limitations

- Three added functions: account-scoped profile, user-controlled analysis history/trends, and a profile-driven rules-based routine.
- No open registration, password reset, e-commerce, medical diagnosis, image model training, or efficacy claims.
- Trends represent model output variation, not proof of clinical improvement.
- The two-account login is a minimal classroom prototype; production use requires a security and privacy review.
