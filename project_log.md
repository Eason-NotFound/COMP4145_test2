# SkinTrack project log

## 2026-10-21 — Initial implementation and prototype delivery (Gradio)

### Objective
Read the COMP4145 Lab 2 Idea 2 brief, build the requested single-file Gradio prototype and three additions, and validate the core behavior.

### Prompt used
The implementation request was the user's full Chinese task brief in this session: develop SkinTrack using Python/Gradio (not Streamlit), integrate the specified HKBU Gemini endpoint without exposing secrets, implement account-scoped profiles/history/routine recommendations, and test realistic failure and isolation cases. The assignment's original prototype prompt requests an image upload + submit UI, HKBU API use, cleanliness/quality scores, skin description, estimated age, and a single Python file.

### Starting evidence and scope
- The workspace initially contained only `COMP4145-VibeCoding_Lab2.docx`; there was no README, source code, API example, API key, Git repository, or deployment configuration.
- The assignment document was read, including Idea 2, original prototype prompt, submission requirements, and 25%-each rubric categories.
- No three selected features were recorded in the supplied material. This implementation uses the user's confirmed three: profiles/access, history/trends, and routine recommendations.
- The specified service's request format was not independently verified from a school example. The app documents its OpenAI-compatible multimodal chat-completions assumption.

### Planned work / scrum boards reconstructed from the supplied brief
The document asks for pre-Week 1, end-Week 1, and end-Week 2 boards, but the workspace contains no dated progress evidence. The following initial and Week 1 boards are reconstructions from the assignment scope—not contemporaneous records and not claims about work that happened then.

**Before Week 1 (reconstructed planning board)**
| Backlog | Ready | In progress | Done |
|---|---|---|---|
| Build the image-analysis prototype | Choose three additions | — | Read Idea 2 and rubric |
| Confirm gateway request/response contract | Plan consent, privacy, and errors | — | Define scope boundary (no model training) |

**End of Week 1 (reconstructed initial update)**
| Backlog | Ready | In progress | Done |
|---|---|---|---|
| Live API verification; cloud deployment | Validate two-account isolation; test on-device UX | Build history and routine | Extract assignment requirements; implement app shell, consent, API adapter, profile/history tables |
| Durable cloud database configuration | Test with actual HKBU credentials | — | Create setup/deployment documentation |

### Actual modifications
- `app.py`: Gradio tabs, consent-gated image upload/API adapter, secure configuration lookup, validation, SQLite profiles and analyses, owner-filtered history/delete, Plotly trends, and rule-based routine.
- `tests/test_app.py`: focused automated coverage of input handling, API failures/mocked success, score parsing, data ownership, history operations, profile-driven recommendation changes, and repeated saves.
- `requirements.txt`, `.env.example`, `.gitignore`, `README.md`: tested/pinned dependencies, secret names, local setup, API assumption, privacy, and cloud persistence/deployment notes.

### Issues and resolutions
- **No school API sample or credentials:** retained the assignment's HKBU Gemini URL; implemented a clearly documented OpenAI-compatible request assumption and honest missing-key behavior. Live connectivity remains unverified.
- **No selected additions in source materials:** followed the user's explicit selection of profile, history/trends, and personalized routine without expanding the number of features.
- **No deployment credentials/account:** prepare deployment files and steps; do not fabricate an accessible link.

### Tests
- **Automated/simulated:** 13 unit tests passed under Python 3.13.11, including missing photo/consent/key, one-request key input, invalid image type/size, mocked multimodal API success, timeout/HTTP error, response parsing and score constraints, per-account history/deletion isolation, repeated save behavior, profile persistence, and recommendation changes.
- **Local UI:** Gradio server constructed and returned HTTP 200 (`text/html`) on localhost.
- **Dependency versions verified in the test environment:** gradio 6.30.0; requests 2.34.2; python-dotenv 1.2.4; pandas 3.0.6; plotly 7.1.0; Pillow 12.3.0.
- **Live API:** not run; there was no API key or supplied real test photo. Request/response compatibility remains to be confirmed against HKBU documentation.
- **Cloud deployment:** not run; there was no hosting account/repository or deployment secret configuration. No platform link is available.

### Next steps
Remaining: confirm HKBU gateway contract and run one consented API request; deploy to a Gradio-compatible cloud account with secrets; add durable mounted storage if cloud history must persist.

## 2026-10-21 — Add masked API key input

### Objective
Provide an in-app field so the user can enter their HKBU API key without editing a configuration file.

### Prompt used
“我有api key，https://genai.hkbu.edu.hk/?overlay=developer&developer_section=credentials这个是网站，给我输入的地方让我填入数据”

### Changes
- Added a password-masked HKBU API key input to the Skin Analysis tab.
- A per-request key overrides `HKBU_API_KEY`; it is not written to the database or logs and is cleared after an analysis attempt.
- Retained environment-variable configuration as the safer option for local development/deployment secrets.
- Updated README and tests for per-request key use.

### Validation
Automated test confirms a manually supplied key is passed in the authorization header and does not require environment configuration; full suite rerun below.

Result: 13 tests passed; the Gradio page with the new field returned HTTP 200 locally. No real HKBU request was made.

## 2026-10-21 — Diagnose Streamlit Cloud loading failure and migrate UI

### Objective
Find why the deployed page did not load when the user selected Streamlit Cloud and align the application with the requested Streamlit platform.

### Root cause
- The GitHub deployment commit used a Gradio application in `app.py`, and `requirements.txt` did not install Streamlit.
- Streamlit Cloud executes the configured entry point as a Streamlit script; the old entry point instead constructed and launched a Gradio server. Streamlit's public proxy does not serve that embedded Gradio server as the app page.
- After migration, the first UI smoke run uncovered an additional Streamlit 1.50 incompatibility: `st.file_uploader()` has no `max_upload_size` argument. The argument was removed; the upload cap is set in `.streamlit/config.toml` and independently validated against file size in application code.
- Dependency resolution also exposed incompatible old pins (`pandas==3.0.6` and `Pillow==12.3.0`) with Streamlit 1.50. Compatible versions were pinned after installation.

### Changes made
- Replaced the Gradio entry point with a Streamlit app while retaining image/API analysis, login, profile, history/trends, and routine features.
- Updated dependencies, Streamlit theme/upload config, and Streamlit Community Cloud deployment instructions.
- Added a minimal two-account server-validated sign-in flow backed by environment variables or Streamlit Secrets.

### Tests
- 13 unit tests pass.
- Streamlit AppTest renders logged-out and authenticated views; all four tabs render with no script exceptions.
- Python compilation and `pip check` pass.
- Live HKBU API and cloud deployment remain untested; no service URL/log access was provided.
