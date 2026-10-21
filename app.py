"""SkinTrack: a privacy-conscious skincare analysis prototype."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError


APP_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
load_dotenv(APP_DIR / ".env")
DB_PATH = Path(os.getenv("SKINTRACK_DB_PATH", APP_DIR / "skintrack.db"))
API_URL_DEFAULT = (
    "https://genai.hkbu.edu.hk/api/v0/rest/deployments/"
    "gemini-2.5-pro/chat/completions?api-version=v1"
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
API_TIMEOUT_SECONDS = 45


class SkinTrackError(Exception):
    """An expected, user-safe application error."""


def configured_value(name: str, default: str = "") -> str:
    environment_value = os.getenv(name)
    if environment_value:
        return environment_value
    try:
        return str(st.secrets.get(name, default))
    except st.errors.StreamlitSecretNotFoundError:
        return default


def get_api_config(api_key_input: str | None = None) -> tuple[str, str]:
    api_key = (api_key_input or configured_value("HKBU_API_KEY")).strip()
    api_url = configured_value("HKBU_API_URL", API_URL_DEFAULT).strip()
    if not api_key:
        raise SkinTrackError(
            "Enter your HKBU API key above or configure HKBU_API_KEY in .env."
        )
    if not api_url.startswith("https://"):
        raise SkinTrackError("HKBU_API_URL must be an HTTPS URL.")
    return api_key, api_url


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                username TEXT PRIMARY KEY,
                skin_type TEXT NOT NULL DEFAULT '',
                concerns TEXT NOT NULL DEFAULT '',
                budget TEXT NOT NULL DEFAULT '',
                avoid_ingredients TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cleanliness REAL,
                quality REAL,
                description TEXT NOT NULL,
                estimated_age TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES profiles(username)
            )
            """
        )


def authenticated_user(request: Any | None) -> str:
    username = request if isinstance(request, str) else getattr(request, "username", None)
    if not username:
        raise SkinTrackError("Your session could not be verified. Please sign in again.")
    return str(username)


def validate_image(image_path: str | None) -> tuple[bytes, str]:
    if not image_path:
        raise SkinTrackError("Upload a face photo before starting an analysis.")
    path = Path(image_path)
    try:
        if not path.is_file():
            raise SkinTrackError("The uploaded image could not be found. Please upload it again.")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise SkinTrackError("Choose an image smaller than 8 MB.")
        raw = path.read_bytes()
        with Image.open(path) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except UnidentifiedImageError as exc:
        raise SkinTrackError("Use a valid JPEG, PNG, or WebP image.") from exc
    except OSError as exc:
        raise SkinTrackError("The uploaded image could not be read. Please try another file.") from exc
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise SkinTrackError("Use a JPEG, PNG, or WebP image.")
    mime_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image_format]
    return raw, mime_type


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise SkinTrackError("The analysis service returned an unreadable result.")
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise SkinTrackError("The analysis service returned an unreadable result.") from exc
    if not isinstance(result, dict):
        raise SkinTrackError("The analysis service returned an unexpected result format.")
    return result


def _score(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SkinTrackError(f"The {label} score could not be read.")
    if not 0 <= value <= 10:
        raise SkinTrackError(f"The {label} score is outside the valid 0–10 range.")
    return float(value)


def parse_analysis_response(response_json: dict[str, Any]) -> dict[str, Any]:
    try:
        content = response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SkinTrackError("The analysis service returned an unexpected result format.") from exc
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise SkinTrackError("The analysis service returned an empty result.")
    data = _extract_json_object(content)
    cleanliness = _score(data.get("skin_cleanliness"), "Skin Cleanliness")
    quality = _score(data.get("skin_quality"), "Skin Quality")
    description = data.get("description")
    age = data.get("estimated_age")
    if not isinstance(description, str) or not description.strip():
        description = "Unable to read"
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        age = str(age)
    if not isinstance(age, str) or not age.strip():
        age = "Unable to read"
    return {
        "cleanliness": cleanliness,
        "quality": quality,
        "description": description.strip(),
        "estimated_age": age.strip(),
    }


def analyze_image(
    image_path: str | None,
    consent: bool,
    api_key_input: str | None = None,
) -> tuple[str, str, str, str, dict[str, Any] | None]:
    if not consent:
        return "Confirm photo processing consent to continue.", "—", "—", "—", None
    try:
        raw, mime_type = validate_image(image_path)
        api_key, api_url = get_api_config(api_key_input)
        encoded_image = base64.b64encode(raw).decode("ascii")
        payload = {
            "model": "gemini-2.5-pro",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Review the provided face photo for general cosmetic skincare information only. "
                                "Do not diagnose a disease. Return only a JSON object with numeric "
                                "skin_cleanliness and skin_quality scores from 0 to 10, a brief cautious "
                                "description, and estimated_age as a string. If a score or age cannot be "
                                "reasonably estimated, use null for that field. State uncertainty."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"},
                        },
                    ],
                }
            ],
            "temperature": 0.2,
        }
        response = requests.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            raise SkinTrackError(
                f"The analysis service returned an error (HTTP {response.status_code}). "
                "Please try again later."
            )
        try:
            parsed = parse_analysis_response(response.json())
        except requests.exceptions.JSONDecodeError as exc:
            raise SkinTrackError("The analysis service returned an unreadable response.") from exc
        return (
            "Analysis complete. Review the uncertainty note before relying on these results.",
            _display_score(parsed["cleanliness"]),
            _display_score(parsed["quality"]),
            _format_analysis(parsed),
            parsed,
        )
    except requests.Timeout:
        return "The analysis request timed out. Please try again.", "—", "—", "—", None
    except requests.RequestException:
        return "Could not reach the analysis service. Check your connection and try again.", "—", "—", "—", None
    except SkinTrackError as exc:
        return str(exc), "—", "—", "—", None


def _display_score(value: float | None) -> str:
    return f"{value:g} / 10" if value is not None else "Unable to read"


def _format_analysis(parsed: dict[str, Any]) -> str:
    return (
        f"**Skin condition:** {parsed['description']}\n\n"
        f"**Estimated Age:** {parsed['estimated_age']} *(rough, unreliable estimate; not an identity or health measure)*\n\n"
        "**Note:** Image-based observations may be inaccurate and are not a medical diagnosis."
    )


def _profile_values(username: str) -> tuple[str, str, str, str]:
    initialize_database()
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        row = connection.execute(
            "SELECT skin_type, concerns, budget, avoid_ingredients FROM profiles WHERE username = ?",
            (username,),
        ).fetchone()
    return tuple(row) if row else ("", "", "", "")


def load_profile(request: Any) -> tuple[str, str, str, str]:
    return _profile_values(authenticated_user(request))


def save_profile(
    skin_type: str, concerns: str, budget: str, avoid_ingredients: str, request: Any
) -> str:
    username = authenticated_user(request)
    initialize_database()
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute(
            """
            INSERT INTO profiles (username, skin_type, concerns, budget, avoid_ingredients, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                skin_type = excluded.skin_type,
                concerns = excluded.concerns,
                budget = excluded.budget,
                avoid_ingredients = excluded.avoid_ingredients,
                updated_at = excluded.updated_at
            """,
            (
                username,
                skin_type.strip(),
                concerns.strip(),
                budget.strip(),
                avoid_ingredients.strip(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return "Profile saved."


def save_analysis(parsed: dict[str, Any] | None, request: Any) -> str:
    if not parsed:
        return "Run a successful analysis before saving a history record."
    username = authenticated_user(request)
    initialize_database()
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute(
            """
            INSERT INTO analyses
                (username, created_at, cleanliness, quality, description, estimated_age)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                parsed["cleanliness"],
                parsed["quality"],
                parsed["description"],
                parsed["estimated_age"],
            ),
        )
    return "Analysis saved to your private history."


def get_history(username: str) -> pd.DataFrame:
    initialize_database()
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        rows = connection.execute(
            """
            SELECT id, created_at, cleanliness, quality, description, estimated_age
            FROM analyses WHERE username = ? ORDER BY created_at DESC, id DESC
            """,
            (username,),
        ).fetchall()
    return pd.DataFrame(
        rows,
        columns=["ID", "Created (UTC)", "Skin Cleanliness", "Skin Quality", "Description", "Estimated Age"],
    )


def history_view(request: Any) -> tuple[pd.DataFrame, go.Figure, str]:
    username = authenticated_user(request)
    history = get_history(username)
    figure = go.Figure()
    if not history.empty:
        ordered = history.sort_values(["Created (UTC)", "ID"])
        for column, label, color in [
            ("Skin Cleanliness", "Skin Cleanliness", "#418267"),
            ("Skin Quality", "Skin Quality", "#91b47d"),
        ]:
            valid = ordered.dropna(subset=[column])
            figure.add_trace(
                go.Scatter(
                    x=valid["Created (UTC)"],
                    y=valid[column],
                    mode="lines+markers",
                    name=label,
                    line={"color": color},
                )
            )
        figure.update_yaxes(range=[0, 10], title="Score / 10")
        figure.update_xaxes(title="Saved analysis (UTC)")
        figure.update_layout(template="plotly_white", legend={"orientation": "h"})
    else:
        figure.update_layout(
            template="plotly_white",
            annotations=[{"text": "No saved analyses yet", "showarrow": False}],
        )
    info = (
        "Trend reflects changes in model output only; it cannot establish skin improvement or product effectiveness."
        if not history.empty
        else "Save an analysis to begin your private history."
    )
    return history, figure, info


def delete_history(record_id: int | float | None, request: Any) -> tuple[str, pd.DataFrame, go.Figure, str]:
    username = authenticated_user(request)
    if record_id is None:
        message = "Enter a record ID from your history."
    else:
        initialize_database()
        with closing(sqlite3.connect(DB_PATH)) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM analyses WHERE id = ? AND username = ?",
                (int(record_id), username),
            )
        message = "Record deleted." if cursor.rowcount else "Record not found in your history."
    history, figure, info = history_view(request)
    return message, history, figure, info


def build_routine(
    skin_type: str, concerns: str, budget: str, avoid_ingredients: str
) -> tuple[str, str]:
    profile_text = " ".join([skin_type or "", concerns or "", budget or "", avoid_ingredients or ""]).strip()
    if len(profile_text) < 4:
        message = "Add your skin type, a concern, budget, or ingredients to avoid in My Profile for tailored guidance."
        return message, message

    concern_text = (concerns or "").lower()
    avoid = [item.strip() for item in (avoid_ingredients or "").split(",") if item.strip()]
    avoid_note = f" Avoid ingredients you listed: {', '.join(avoid)}." if avoid else ""
    budget_note = f"Keep options within your stated budget ({budget.strip()})." if budget and budget.strip() else "Choose an option within your personal budget."
    skin_note = f" Your self-reported skin type is {skin_type.strip()}." if skin_type and skin_type.strip() else ""

    if "dry" in (skin_type or "").lower() or "dryness" in concern_text:
        moisturizer = "a fragrance-free moisturizer with humectant/emollient ingredients"
        reason = "Your profile mentions dry skin or dryness."
    elif "oil" in (skin_type or "").lower() or "acne" in concern_text or "breakout" in concern_text:
        moisturizer = "a lightweight, non-comedogenic moisturizer"
        reason = "Your profile mentions oily skin or breakouts."
    else:
        moisturizer = "a gentle moisturizer suited to your comfort and skin type"
        reason = "Your profile supports a gentle, adaptable routine."

    concern_step = ""
    if "sensitive" in concern_text or "redness" in concern_text:
        concern_step = "Introduce one fragrance-free product at a time and patch test; avoid harsh scrubs.\n"
    elif "dark spot" in concern_text or "uneven tone" in concern_text:
        concern_step = "If tolerated, consider a simple product category for uneven tone; introduce gradually and stop if irritated.\n"
    elif "acne" in concern_text or "breakout" in concern_text:
        concern_step = "A gentle, non-comedogenic cleanser may suit your stated concern; seek professional advice for persistent or painful symptoms.\n"

    morning = (
        "1. Gentle cleanser (or rinse if appropriate).\n"
        f"2. {moisturizer.capitalize()}.\n"
        "3. Broad-spectrum sunscreen, SPF 30 or higher; reapply as directed.\n"
        f"{concern_step}"
        f"Product-category match: {reason} {budget_note}{avoid_note}{skin_note}"
    )
    evening = (
        "1. Gently cleanse to remove sunscreen and daily residue.\n"
        f"2. Apply {moisturizer}.\n"
        "3. Add at most one optional product at a time; patch test and discontinue if irritation occurs.\n"
        f"Product-category match: {reason} {budget_note}{avoid_note}{skin_note}\n\n"
        "General skincare information only—not a diagnosis or prescription. Consult a qualified professional for persistent concerns."
    )
    return morning, evening


def refresh_routine(request: Any) -> tuple[str, str]:
    username = authenticated_user(request)
    skin_type, concerns, budget, avoid = _profile_values(username)
    return build_routine(skin_type, concerns, budget, avoid)


def remove_temp_upload(image_path: str | None) -> None:
    if not image_path:
        return
    try:
        path = Path(image_path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if path.is_relative_to(temp_root) and path.is_file():
            path.unlink()
    except OSError as exc:
        LOGGER.warning("Could not clean up a temporary uploaded image (%s).", type(exc).__name__)


def _auth_credentials() -> list[tuple[str, str]]:
    accounts = []
    for index in (1, 2):
        username = configured_value(f"SKINTRACK_USER{index}").strip()
        password = configured_value(f"SKINTRACK_PASSWORD{index}")
        if username and password:
            accounts.append((username, password))
    return accounts


def _render_login(credentials: list[tuple[str, str]]) -> None:
    st.title("SkinTrack")
    st.caption("Sign in to access your private skincare workspace.")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if any(
            hmac.compare_digest(username, saved_username)
            and hmac.compare_digest(password, saved_password)
            for saved_username, saved_password in credentials
        ):
            st.session_state["authenticated_user"] = username
            st.rerun()
        st.error("The username or password is incorrect.")


def _render_streamlit_app() -> None:
    st.set_page_config(page_title="SkinTrack", page_icon="🌿", layout="wide")
    st.markdown(
        """
        <style>
        .stApp { background: #f7faf7; }
        [data-testid="stHeader"] { background: rgba(0,0,0,0); }
        div[data-testid="stMetric"] {
            background: white; border: 1px solid #dce8dd;
            padding: 16px; border-radius: 14px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    credentials = _auth_credentials()
    if len(credentials) != 2 or len({username for username, _ in credentials}) != 2:
        st.error(
            "Configure two distinct SKINTRACK_USER1 / SKINTRACK_PASSWORD1 and "
            "SKINTRACK_USER2 / SKINTRACK_PASSWORD2 pairs in Streamlit Secrets or .env."
        )
        st.stop()

    if st.session_state.pop("_clear_analysis_key", False):
        st.session_state["analysis_api_key"] = ""

    username = st.session_state.get("authenticated_user")
    if not username:
        _render_login(credentials)
        return

    try:
        initialize_database()
    except sqlite3.Error:
        LOGGER.exception("Could not initialize SkinTrack SQLite database.")
        st.error("SkinTrack could not initialize its database. Check the configured storage path.")
        st.stop()

    with st.sidebar:
        st.markdown(f"Signed in as **{username}**")
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()

    st.title("🌿 SkinTrack")
    st.caption("A private skincare tracking prototype · General information, not medical advice")
    analysis_tab, profile_tab, history_tab, routine_tab = st.tabs(
        ["Skin Analysis", "My Profile", "History", "My Routine"]
    )

    with analysis_tab:
        st.markdown(
            "Upload a JPEG, PNG, or WebP face photo (max 8 MB). If you proceed, the image is sent "
            "to the configured HKBU analysis service for processing; it is not saved in SkinTrack's database."
        )
        api_key_input = st.text_input(
            "HKBU API key (optional if configured in Streamlit Secrets)",
            type="password",
            key="analysis_api_key",
            help="Used only for this analysis request; not saved to the database or project files.",
        )
        uploaded_image = st.file_uploader(
            "Face photo",
            type=["jpg", "jpeg", "png", "webp"],
            key="analysis_image",
        )
        if uploaded_image:
            st.image(uploaded_image, caption="Photo preview", width="stretch")
        consent = st.checkbox(
            "I understand and consent to sending this photo to the configured analysis service.",
            key="analysis_consent",
        )
        if st.button("Analyze photo", type="primary", key="analyze_button"):
            if uploaded_image is None:
                st.session_state["analysis_status"] = (
                    "Upload a face photo before starting an analysis.",
                    "error",
                )
                st.session_state["analysis_result"] = None
            elif not consent:
                st.session_state["analysis_status"] = (
                    "Confirm photo processing consent to continue.",
                    "error",
                )
                st.session_state["analysis_result"] = None
            else:
                temp_path = None
                try:
                    suffix = Path(uploaded_image.name).suffix or ".img"
                    with tempfile.NamedTemporaryFile(
                        mode="wb", suffix=suffix, dir=tempfile.gettempdir(), delete=False
                    ) as temp_image:
                        temp_image.write(uploaded_image.getvalue())
                        temp_path = temp_image.name
                    with st.spinner("Analyzing photo securely..."):
                        result = analyze_image(temp_path, True, api_key_input)
                    st.session_state["analysis_status"] = (
                        result[0],
                        "success" if result[4] else "error",
                    )
                    st.session_state["analysis_result"] = result
                    st.session_state["analysis_saved"] = False
                finally:
                    if temp_path:
                        remove_temp_upload(temp_path)
            st.session_state["_clear_analysis_key"] = True
            st.rerun()

        status = st.session_state.get("analysis_status")
        if status:
            getattr(st, status[1])(status[0])
        analysis = st.session_state.get("analysis_result")
        if analysis:
            left, right = st.columns(2)
            left.metric("Skin Cleanliness KPI", analysis[1])
            right.metric("Skin Quality KPI", analysis[2])
            st.markdown(analysis[3])
            parsed = analysis[4]
            already_saved = st.session_state.get("analysis_saved", False)
            if st.button(
                "Saved to history" if already_saved else "Save this analysis to my history",
                disabled=not parsed or already_saved,
            ):
                st.success(save_analysis(parsed, username))
                st.session_state["analysis_saved"] = True

    with profile_tab:
        st.markdown(
            "Your self-reported details guide general routine suggestions. "
            "Only this signed-in account can access its profile."
        )
        current_profile = _profile_values(username)
        profile_key = f"profile_{username}_"
        st.session_state.setdefault(profile_key + "skin_type", current_profile[0])
        st.session_state.setdefault(profile_key + "concerns", current_profile[1])
        st.session_state.setdefault(profile_key + "budget", current_profile[2])
        st.session_state.setdefault(profile_key + "avoid", current_profile[3])
        with st.form("profile_form"):
            skin_type = st.selectbox(
                "Skin type (self-reported)",
                ["", "Normal", "Dry", "Oily", "Combination", "Sensitive", "Not sure"],
                key=profile_key + "skin_type",
            )
            concerns = st.text_input(
                "Focus concerns",
                placeholder="e.g., dryness, sensitivity, uneven tone",
                key=profile_key + "concerns",
            )
            budget = st.text_input(
                "Budget",
                placeholder="e.g., under HKD 300 per product",
                key=profile_key + "budget",
            )
            avoid = st.text_input(
                "Ingredients to avoid",
                placeholder="Comma-separated",
                key=profile_key + "avoid",
            )
            save_profile_button = st.form_submit_button("Save profile", type="primary")
        if save_profile_button:
            st.success(save_profile(skin_type, concerns, budget, avoid, username))

    with history_tab:
        st.markdown(
            "Trends describe model output changes only, not proof of skin improvement or product effectiveness."
        )
        history = get_history(username)
        if history.empty:
            st.info("No saved analyses yet. Run and save an analysis to begin your history.")
        else:
            st.dataframe(history, hide_index=True, use_container_width=True)
            history, figure, note = history_view(username)
            st.plotly_chart(figure, use_container_width=True)
            st.caption(note)
            record_ids = [int(record_id) for record_id in history["ID"].tolist()]
            selected_id = st.selectbox("Record details", record_ids, format_func=lambda value: f"Record {value}")
            selected = history.loc[history["ID"] == selected_id].iloc[0]
            st.markdown(f"**Description:** {selected['Description']}")
            st.markdown(
                f"**Estimated Age:** {selected['Estimated Age']} "
                "*(rough, unreliable estimate)*"
            )
            with st.form("delete_history_form"):
                delete_id = st.selectbox("Select a record to delete", record_ids)
                delete_clicked = st.form_submit_button("Delete selected record")
            if delete_clicked:
                message, _, _, _ = delete_history(delete_id, username)
                if message == "Record deleted.":
                    st.success(message)
                    st.rerun()
                st.error(message)

    with routine_tab:
        st.markdown(
            "Transparent, rules-based general skincare information. Your self-reported profile is primary; "
            "image analysis is not used to diagnose."
        )
        skin_type, concerns, budget, avoid = _profile_values(username)
        morning, evening = build_routine(skin_type, concerns, budget, avoid)
        morning_col, evening_col = st.columns(2)
        with morning_col:
            st.subheader("Morning")
            st.markdown(morning)
        with evening_col:
            st.subheader("Evening")
            st.markdown(evening)


if __name__ == "__main__":
    _render_streamlit_app()
