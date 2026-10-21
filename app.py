"""SkinTrack: a privacy-conscious skincare analysis prototype."""

from __future__ import annotations

import base64
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

import gradio as gr
import pandas as pd
import plotly.graph_objects as go
import requests
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError


APP_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
load_dotenv(APP_DIR / ".env")
DB_PATH = Path(os.getenv("SKINTRACK_DB_PATH", APP_DIR / "skintrack.db"))
APP_CSS = """
.gradio-container { max-width: 1180px !important; background: #f7faf7; }
.skintrack-header { padding: 22px 26px; border-radius: 18px; background: linear-gradient(120deg,#e7f1e7,#f4f8ef); }
.skintrack-header h1 { color: #254f3a; margin-bottom: 4px; }
.metric-card { border: 1px solid #dce8dd; border-radius: 14px; padding: 14px; background: white; }
"""
API_URL_DEFAULT = (
    "https://genai.hkbu.edu.hk/api/v0/rest/deployments/"
    "gemini-2.5-pro/chat/completions?api-version=v1"
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
API_TIMEOUT_SECONDS = 45


class SkinTrackError(Exception):
    """An expected, user-safe application error."""


def get_api_config(api_key_input: str | None = None) -> tuple[str, str]:
    api_key = (api_key_input or os.getenv("HKBU_API_KEY", "")).strip()
    api_url = os.getenv("HKBU_API_URL", API_URL_DEFAULT).strip()
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


def authenticated_user(request: gr.Request | None) -> str:
    username = getattr(request, "username", None) if request else None
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


def load_profile(request: gr.Request) -> tuple[str, str, str, str]:
    return _profile_values(authenticated_user(request))


def save_profile(
    skin_type: str, concerns: str, budget: str, avoid_ingredients: str, request: gr.Request
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


def save_analysis(parsed: dict[str, Any] | None, request: gr.Request) -> str:
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


def history_view(request: gr.Request) -> tuple[pd.DataFrame, go.Figure, str]:
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


def delete_history(record_id: int | float | None, request: gr.Request) -> tuple[str, pd.DataFrame, go.Figure, str]:
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


def refresh_routine(request: gr.Request) -> tuple[str, str]:
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


def make_app() -> gr.Blocks:
    initialize_database()
    with gr.Blocks(title="SkinTrack | Skin Analysis") as demo:
        gr.HTML(
            "<div class='skintrack-header'><h1>SkinTrack</h1>"
            "<p>A private skincare tracking prototype · General information, not medical advice</p></div>"
        )
        with gr.Tabs():
            with gr.Tab("Skin Analysis"):
                gr.Markdown(
                    "Upload a JPEG, PNG, or WebP face photo (max 8 MB). If you proceed, the image is sent "
                    "to the configured HKBU analysis service for processing; it is not saved in SkinTrack's database."
                )
                api_key_input = gr.Textbox(
                    label="HKBU API key (optional if configured in .env)",
                    type="password",
                    placeholder="Paste your API key here",
                    info="Used only for this analysis request; never stored in the database or project files.",
                )
                image = gr.Image(type="filepath", label="Face photo", sources=["upload"])
                consent = gr.Checkbox(
                    label="I understand and consent to sending this photo to the configured analysis service.",
                    value=False,
                )
                analyze_button = gr.Button("Analyze photo", variant="primary")
                status = gr.Markdown("Waiting for a photo.")
                with gr.Row():
                    cleanliness = gr.Textbox(label="Skin Cleanliness KPI", value="—", interactive=False)
                    quality = gr.Textbox(label="Skin Quality KPI", value="—", interactive=False)
                result_text = gr.Markdown()
                result_state = gr.State(None)
                analyze_button.click(
                    analyze_image,
                    inputs=[image, consent, api_key_input],
                    outputs=[status, cleanliness, quality, result_text, result_state],
                    show_progress="full",
                ).then(remove_temp_upload, inputs=[image], outputs=None).then(
                    lambda: "",
                    inputs=None,
                    outputs=[api_key_input],
                )
                save_button = gr.Button("Save this analysis to my history")
                save_status = gr.Markdown()
                save_button.click(save_analysis, inputs=[result_state], outputs=[save_status])

            with gr.Tab("My Profile"):
                gr.Markdown("Your self-reported details guide general routine suggestions. No open registration is enabled.")
                skin_type = gr.Dropdown(
                    ["", "Normal", "Dry", "Oily", "Combination", "Sensitive", "Not sure"],
                    label="Skin type (self-reported)",
                    value="",
                    allow_custom_value=True,
                )
                concerns = gr.Textbox(label="Focus concerns", placeholder="e.g., dryness, sensitivity, uneven tone")
                budget = gr.Textbox(label="Budget", placeholder="e.g., under HKD 300 per product")
                avoid = gr.Textbox(label="Ingredients to avoid", placeholder="Comma-separated, based on your preference")
                profile_status = gr.Markdown()
                gr.Button("Save profile", variant="primary").click(
                    save_profile,
                    inputs=[skin_type, concerns, budget, avoid],
                    outputs=[profile_status],
                )
                demo.load(load_profile, outputs=[skin_type, concerns, budget, avoid])

            with gr.Tab("History"):
                gr.Markdown(
                    "Only your signed-in account's saved analyses are shown. Trends describe model output changes, "
                    "not proof of skin improvement or product effectiveness."
                )
                history_table = gr.Dataframe(
                    headers=["ID", "Created (UTC)", "Skin Cleanliness", "Skin Quality", "Description", "Estimated Age"],
                    interactive=False,
                    label="Saved analyses",
                )
                trend_plot = gr.Plot(label="KPI trend")
                history_note = gr.Markdown()
                history_status = gr.Markdown()
                refresh_button = gr.Button("Refresh history")
                refresh_button.click(history_view, outputs=[history_table, trend_plot, history_note])
                delete_id = gr.Number(label="Record ID to delete", precision=0)
                gr.Button("Delete my record").click(
                    delete_history,
                    inputs=[delete_id],
                    outputs=[history_status, history_table, trend_plot, history_note],
                )
                demo.load(history_view, outputs=[history_table, trend_plot, history_note])

            with gr.Tab("My Routine"):
                gr.Markdown(
                    "Recommendations are transparent, rules-based general skincare information. "
                    "Your self-reported profile is primary; image analysis is not used to diagnose."
                )
                morning = gr.Markdown()
                evening = gr.Markdown()
                gr.Button("Generate my routine", variant="primary").click(
                    refresh_routine, outputs=[morning, evening]
                )
                demo.load(refresh_routine, outputs=[morning, evening])
        image.clear(remove_temp_upload, inputs=[image], outputs=None)
    return demo


def _auth_credentials() -> list[tuple[str, str]]:
    accounts = []
    for index in (1, 2):
        username = os.getenv(f"SKINTRACK_USER{index}", "").strip()
        password = os.getenv(f"SKINTRACK_PASSWORD{index}", "")
        if username and password:
            accounts.append((username, password))
    return accounts


if __name__ == "__main__":
    app = make_app()
    credentials = _auth_credentials()
    if len(credentials) != 2 or len({username for username, _ in credentials}) != 2:
        raise SystemExit(
            "Configure two distinct SKINTRACK_USERn / SKINTRACK_PASSWORDn pairs "
            "in .env or the deployment environment before launch."
        )
    app.launch(
        auth=credentials,
        theme=gr.themes.Soft(primary_hue="green"),
        css=APP_CSS,
        show_error=True,
    )
