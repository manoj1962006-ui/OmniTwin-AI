import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import altair as alt
import pandas as pd
import streamlit as st

from assistant_backend import (
    analytics_snapshot,
    approve_mark_read,
    create_conversation,
    ensure_database,
    generate_answer,
    list_conversations,
    load_messages,
    record_action,
    retrieve_inbox_context,
    save_message,
    update_conversation_model,
)
from daemon import DEFAULT_MODEL, inject_mock_data, process_pending_items


DB_PATH = Path(os.environ.get("OMNITWIN_DB_PATH", Path(__file__).with_name("omnitwin.db")))
SOURCE_LABELS = {
    "gmail": "Gmail",
    "calendar": "Calendar",
    "whatsapp": "WhatsApp",
    "linkedin": "LinkedIn",
    "sms": "SMS",
}
SOURCE_ORDER = tuple(SOURCE_LABELS)
URGENCY_LEVELS = ("URGENT", "FYI", "SPAM")
MODEL_OPTIONS = {
    "Gemini 3.8 Flash": DEFAULT_MODEL,
}
FILE_TYPES = ["jpg", "jpeg", "png", "webp", "pdf", "txt", "md", "csv", "json", "docx"]
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json"}
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MAX_FILE_BYTES = 15 * 1024 * 1024


def _environment_key() -> str:
    try:
        secret = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        secret = ""
    return str(secret or os.environ.get("GEMINI_API_KEY", ""))


def _new_attachments(submission: object) -> list[dict[str, object]]:
    attachments: list[dict[str, object]] = []
    for uploaded in getattr(submission, "files", []):
        name = uploaded.name
        suffix = Path(name).suffix.lower()
        if suffix not in set(IMAGE_MIME) | TEXT_EXTENSIONS | {".pdf", ".docx"}:
            raise ValueError(f"Unsupported attachment type: {suffix or 'unknown'}")
        data = uploaded.getvalue()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name} exceeds the 15 MB attachment limit.")

        fallback_mime = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(suffix, uploaded.type or "application/octet-stream")
        attachment: dict[str, object] = {
            "name": name,
            "mime_type": IMAGE_MIME.get(suffix, fallback_mime),
            "data": data,
        }
        if suffix in TEXT_EXTENSIONS:
            attachment["mime_type"] = "text/plain"
            attachment["text_content"] = data.decode("utf-8", errors="replace")[:20000]
        elif suffix == ".docx":
            try:
                with ZipFile(BytesIO(data)) as archive:
                    document = ET.fromstring(archive.read("word/document.xml"))
                namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                attachment["text_content"] = " ".join(
                    node.text or "" for node in document.findall(".//w:t", namespace)
                )[:20000]
            except (BadZipFile, KeyError, ET.ParseError) as error:
                raise ValueError(f"Could not read {name} as a DOCX document.") from error
        attachments.append(attachment)

    audio = getattr(submission, "audio", None)
    if audio is not None:
        data = audio.getvalue()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Voice recording exceeds the 15 MB limit.")
        attachments.append(
            {"name": "Voice recording.wav", "mime_type": "audio/wav", "data": data}
        )
    if len(attachments) > 4:
        raise ValueError("Attach up to four files per message.")
    return attachments


def _render_attachments(attachments: list[dict[str, object]], message_id: str) -> None:
    for index, attachment in enumerate(attachments):
        name = str(attachment["name"])
        mime_type = str(attachment["mime_type"])
        data = attachment["data"]
        if mime_type.startswith("image/"):
            st.image(data, caption=name, width="stretch")
        elif mime_type.startswith("audio/"):
            st.audio(data, format=mime_type)
        else:
            st.download_button(
                f"Download {name}",
                data=data,
                file_name=name,
                mime=mime_type,
                key=f"attachment-{message_id}-{index}",
                icon=":material/download:",
            )
            if attachment.get("text_content"):
                with st.expander(f"Extracted text · {name}"):
                    st.text(str(attachment["text_content"])[:4000])


def _render_analytics(
    analytics: dict[str, object],
    message_id: int,
    db_path: Path,
) -> None:
    sources = analytics.get("sources", list(SOURCE_ORDER))
    question = str(analytics.get("question", ""))
    urgency = str(analytics.get("urgency", "All urgencies"))
    unread_only = bool(analytics.get("unread_only", False))
    records = retrieve_inbox_context(
        db_path,
        question,
        sources,
        urgency=urgency,
        unread_only=unread_only,
    )
    snapshot = analytics_snapshot(db_path, sources, records)
    metrics = snapshot["metrics"]

    with st.container(horizontal=True):
        st.metric("Inbox", metrics["total"], border=True)
        st.metric("Urgent", metrics["urgent"], border=True)
        st.metric("Unread", metrics["unread"], border=True)
        st.metric("Pending", metrics["pending"], border=True)

    trend = pd.DataFrame(snapshot["trend"])
    if not trend.empty:
        trend["day"] = pd.to_datetime(trend["day"], errors="coerce")
        trend = trend.dropna(subset=["day"])
    if not trend.empty:
        trend_chart = (
            alt.Chart(trend)
            .mark_line(point=True, strokeWidth=2)
            .encode(
                x=alt.X("day:T", title=None),
                y=alt.Y("items:Q", title="Messages", scale=alt.Scale(zero=True)),
                color=alt.Color("urgency:N", title="Urgency"),
                tooltip=["day:T", "urgency:N", "items:Q"],
            )
            .properties(height=190, title="Urgency trend · 14 days")
        )
        st.altair_chart(trend_chart, width="stretch", key=f"trend-{message_id}")

    sources_df = pd.DataFrame(snapshot["source_breakdown"])
    if not sources_df.empty:
        source_chart = (
            alt.Chart(sources_df)
            .mark_bar(cornerRadiusEnd=3)
            .encode(
                x=alt.X("items:Q", title="Messages"),
                y=alt.Y("source:N", title=None, sort="-x"),
                color=alt.Color("urgency:N", title="Urgency"),
                tooltip=["source:N", "urgency:N", "items:Q"],
            )
            .properties(height=max(100, 30 * sources_df["source"].nunique()), title="By source")
        )
        st.altair_chart(source_chart, width="stretch", key=f"sources-{message_id}")

    if records:
        table = pd.DataFrame(records).rename(
            columns={
                "source": "Source",
                "type": "Type",
                "sender": "From",
                "subject": "Subject",
                "summary": "Summary",
                "urgency": "Urgency",
                "urgency_score": "Score",
                "is_read": "Read",
                "received_at": "Received",
            }
        )
        visible_columns = [
            name
            for name in ("Source", "From", "Subject", "Summary", "Urgency", "Score", "Read", "Received")
            if name in table.columns
        ]
        st.dataframe(
            table[visible_columns],
            hide_index=True,
            column_config={
                "Score": st.column_config.ProgressColumn("Urgency score", min_value=0, max_value=100),
                "Summary": st.column_config.TextColumn("Executive summary", width="large"),
            },
            key=f"inbox-results-{message_id}",
        )
        unread_ids = [int(item["id"]) for item in records if not item["is_read"]]
        if unread_ids and st.button(
            f"Approve · mark {len(unread_ids)} as read",
            key=f"approve-read-{message_id}",
            icon=":material/mark_email_read:",
        ):
            changed = approve_mark_read(db_path, unread_ids)
            st.toast(f"Marked {changed} inbox item(s) as read.")
            st.rerun()
    else:
        st.caption("No inbox items match this scope.")


def _render_chat_message(message: dict[str, object], db_path: Path) -> None:
    role = str(message["role"])
    avatar = ":material/person:" if role == "user" else ":material/auto_awesome:"
    with st.chat_message(role, avatar=avatar):
        st.markdown(str(message["content"]))
        attachments = message.get("attachments", [])
        if attachments:
            _render_attachments(attachments, str(message["id"]))
        analytics = message.get("analytics")
        if role == "assistant" and analytics:
            _render_analytics(analytics, int(message["id"]), db_path)


def _render_inbox_overview(
    db_path: Path,
    sources: list[str],
    urgency: str,
    unread_only: bool,
) -> None:
    snapshot = analytics_snapshot(db_path, sources, [])
    metrics = snapshot["metrics"]
    with st.container(horizontal=True):
        st.metric("Messages", metrics["total"], border=True)
        st.metric("Urgent", metrics["urgent"], border=True)
        st.metric("Unread", metrics["unread"], border=True)
        st.metric("Awaiting analysis", metrics["pending"], border=True)

    trend = pd.DataFrame(snapshot["trend"])
    breakdown = pd.DataFrame(snapshot["source_breakdown"])
    if not trend.empty:
        trend["day"] = pd.to_datetime(trend["day"], errors="coerce")
        trend = trend.dropna(subset=["day"])
    if not trend.empty:
        chart = (
            alt.Chart(trend)
            .mark_line(point=True, strokeWidth=2)
            .encode(
                x=alt.X("day:T", title=None),
                y=alt.Y("items:Q", title="Messages", scale=alt.Scale(zero=True)),
                color=alt.Color("urgency:N", title="Urgency"),
                tooltip=["day:T", "urgency:N", "items:Q"],
            )
            .properties(height=180, title="Urgency trend · 14 days")
        )
        st.altair_chart(chart, width="stretch", key="overview-trend")
    if not breakdown.empty:
        chart = (
            alt.Chart(breakdown)
            .mark_bar(cornerRadiusEnd=3)
            .encode(
                x=alt.X("items:Q", title="Messages"),
                y=alt.Y("source:N", title=None, sort="-x"),
                color=alt.Color("urgency:N", title="Urgency"),
                tooltip=["source:N", "urgency:N", "items:Q"],
            )
            .properties(height=160, title="Across connected sources")
        )
        st.altair_chart(chart, width="stretch", key="overview-sources")

    recent = retrieve_inbox_context(
        db_path, "", sources, urgency=urgency, unread_only=unread_only, limit=12
    )
    if recent:
        table = pd.DataFrame(recent).rename(
            columns={
                "source": "Source",
                "sender": "From",
                "subject": "Subject",
                "summary": "Summary",
                "urgency": "Urgency",
                "urgency_score": "Score",
                "is_read": "Read",
                "received_at": "Received",
            }
        )
        st.dataframe(
            table[["Source", "From", "Subject", "Summary", "Urgency", "Score", "Read", "Received"]],
            hide_index=True,
            column_config={
                "Score": st.column_config.ProgressColumn("Urgency score", min_value=0, max_value=100),
                "Summary": st.column_config.TextColumn("Executive summary", width="large"),
            },
            key="overview-inbox-table",
        )
    else:
        st.caption("No inbox items match the active source and urgency filters.")


def _active_api_key() -> str:
    return st.session_state.get("omnitwin_active_api_key") or _environment_key()


def _activate_api_key() -> None:
    st.session_state.omnitwin_active_api_key = st.session_state.get(
        "omnitwin_api_key_draft", ""
    ).strip()


def _clear_api_key() -> None:
    st.session_state.omnitwin_active_api_key = ""
    st.session_state.omnitwin_api_key_draft = ""


def _load_recent_actions(db_path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT action_type, status, created_at, error FROM action_logs "
                "ORDER BY id DESC LIMIT 8"
            )
        ]
    finally:
        connection.close()


def _show_connection(db_path: Path) -> tuple[bool, dict[str, int]]:
    connection = None
    try:
        connection = sqlite3.connect(db_path, timeout=2)
        totals = connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN urgency='URGENT' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN processing_status='pending' THEN 1 ELSE 0 END)
            FROM unified_inbox
            """
        ).fetchone()
        return True, {
            "total": int(totals[0] or 0),
            "urgent": int(totals[1] or 0),
            "unread": int(totals[2] or 0),
            "pending": int(totals[3] or 0),
        }
    except sqlite3.Error:
        return False, {"total": 0, "urgent": 0, "unread": 0, "pending": 0}
    finally:
        if connection is not None:
            connection.close()


def _queue_quick_prompt(widget_key: str, pending_key: str) -> None:
    selected = st.session_state.get(widget_key)
    if selected:
        st.session_state[pending_key] = selected
    st.session_state[widget_key] = None


def main() -> None:
    st.set_page_config(
        page_title="OmniTwin · Executive console",
        page_icon=":material/flare:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    try:
        ensure_database(DB_PATH)
    except (OSError, sqlite3.Error) as error:
        st.error(f"Could not open the local database: {error}")
        st.stop()

    if "omnitwin_active_api_key" not in st.session_state:
        st.session_state.omnitwin_active_api_key = ""

    conversations = list_conversations(DB_PATH)
    model_default = MODEL_OPTIONS["Gemini 3.8 Flash"]
    if not conversations:
        create_conversation(DB_PATH, model_default)
        conversations = list_conversations(DB_PATH)
    conversation_by_id = {item["id"]: item for item in conversations}
    active_id = st.session_state.get("omnitwin_conversation_id")
    if active_id not in conversation_by_id:
        active_id = conversations[0]["id"]
        st.session_state.omnitwin_conversation_id = active_id

    database_online, totals = _show_connection(DB_PATH)
    recent_actions = _load_recent_actions(DB_PATH)
    conversation_titles = {item["id"]: item["title"] for item in conversations}

    with st.sidebar:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.title("OmniTwin", icon=":material/flare:")
            st.badge("SQLite live" if database_online else "Offline", color="green" if database_online else "red")
        if st.button("New conversation", icon=":material/add:", type="primary", width="stretch"):
            new_id = create_conversation(DB_PATH, model_default)
            st.session_state.omnitwin_conversation_id = new_id
            st.rerun()

        st.subheader("Your conversations")
        conversation_ids = [item["id"] for item in conversations]
        selected_id = st.selectbox(
            "Conversation history",
            options=conversation_ids,
            index=conversation_ids.index(active_id),
            format_func=lambda item_id: conversation_titles.get(item_id, "New conversation"),
            label_visibility="collapsed",
            key="conversation_history_selector",
        )
        if selected_id != st.session_state.omnitwin_conversation_id:
            st.session_state.omnitwin_conversation_id = selected_id
            st.rerun()

        st.subheader("Model")
        current_conversation = conversation_by_id[selected_id]
        current_model = current_conversation["model"]
        model_labels = list(MODEL_OPTIONS)
        active_model_label = next(
            (label for label, model in MODEL_OPTIONS.items() if model == current_model),
            model_labels[0],
        )
        model_label = st.selectbox(
            "Gemini model",
            model_labels,
            index=model_labels.index(active_model_label),
            key=f"model-{selected_id}",
        )
        model = MODEL_OPTIONS[model_label]
        if model != current_model:
            update_conversation_model(DB_PATH, selected_id, model)

        st.subheader("Connected sources")
        selected_sources = []
        for source in SOURCE_ORDER:
            state_key = f"source-{source}"
            st.session_state.setdefault(state_key, True)
            if st.checkbox(SOURCE_LABELS[source], key=state_key):
                selected_sources.append(source)
        urgency_scope = st.selectbox(
            "Urgency filter", ("All urgencies", *URGENCY_LEVELS), key="urgency-filter"
        )
        unread_only = st.checkbox("Unread only", key="unread-only")

        with st.expander("API key", expanded=False):
            st.caption("Use a key from secrets/environment or keep one in this session.")
            st.text_input(
                "Gemini API key",
                type="password",
                key="omnitwin_api_key_draft",
                placeholder="Paste key for this session",
            )
            with st.container(horizontal=True):
                st.button("Use key", icon=":material/key:", on_click=_activate_api_key)
                st.button("Clear", icon=":material/delete:", on_click=_clear_api_key)
            st.badge("Key active" if _active_api_key() else "Key required", color="green" if _active_api_key() else "orange")

        st.subheader("Ingestion")
        batch_size = st.number_input("Batch size", min_value=1, max_value=500, value=50, step=10)
        if st.button("Analyze pending", icon=":material/bolt:", width="stretch"):
            key = _active_api_key()
            if not key:
                st.warning("Add a Gemini API key first.")
            else:
                try:
                    with st.spinner("Analyzing pending inbox items..."):
                        count = process_pending_items(DB_PATH, api_key=key, model=model, limit=int(batch_size))
                    st.success(f"Analysis complete: {count} item(s).")
                    st.rerun()
                except Exception as error:
                    record_action(DB_PATH, "inbox_analysis", "failed", str(DB_PATH), error=str(error))
                    st.error(f"Ingestion failed: {error}")
        if st.button("Inject demo data", icon=":material/science:", width="stretch"):
            try:
                inserted = inject_mock_data(DB_PATH)
                processed = process_pending_items(DB_PATH, use_mock_analysis=True, limit=int(batch_size))
                st.success(f"Added {inserted} demo items and analyzed {processed}.")
                st.rerun()
            except Exception as error:
                record_action(DB_PATH, "mock_ingestion", "failed", str(DB_PATH), error=str(error))
                st.error(f"Demo ingestion failed: {error}")

        st.subheader("Live inbox")
        with st.container(horizontal=True):
            st.metric("Messages", totals["total"])
            st.metric("Urgent", totals["urgent"])
        st.caption(f"{totals['unread']} unread · {totals['pending']} awaiting analysis")
        with st.expander("Recent activity"):
            if recent_actions:
                st.dataframe(pd.DataFrame(recent_actions), hide_index=True)
            else:
                st.caption("No actions recorded yet.")
        st.caption(f"Local database · {DB_PATH.name}")

    messages = load_messages(DB_PATH, selected_id)
    with st.expander("Unified inbox overview", expanded=not bool(messages), icon=":material/insights:"):
        _render_inbox_overview(DB_PATH, selected_sources, urgency_scope, unread_only)

    title = conversation_titles.get(selected_id, "New conversation")
    if messages:
        st.caption(f"EXECUTIVE CONSOLE  /  {title}")
    else:
        st.caption("EXECUTIVE CONSOLE  /  PRIVATE WORKSPACE")
        st.title("What should we look into?")
        st.write("A clear view across your inbox, calendar, and conversations.")
        prompt_widget_key = f"quick-prompt-{selected_id}"
        pending_prompt_key = f"pending-prompt-{selected_id}"
        st.pills(
            "Quick prompts",
            [
                "What is urgent?",
                "Show unread LinkedIn messages and urgent calendar conflicts",
                "Summarize my latest emails",
            ],
            key=prompt_widget_key,
            label_visibility="collapsed",
            on_change=_queue_quick_prompt,
            args=(prompt_widget_key, pending_prompt_key),
        )

    for message in messages:
        _render_chat_message(message, DB_PATH)

    submission = st.chat_input(
        "Ask OmniTwin anything about your streams...",
        accept_file="multiple",
        file_type=FILE_TYPES,
        accept_audio=True,
        audio_sample_rate=16000,
        max_upload_size=15,
        submit_mode="disable",
        key=f"chat-input-{selected_id}",
    )
    quick_prompt = st.session_state.pop(f"pending-prompt-{selected_id}", None)
    if submission is not None or quick_prompt:
        question = quick_prompt or submission.text.strip()
        try:
            attachments = _new_attachments(submission) if submission is not None else []
        except (ValueError, OSError) as error:
            st.error(str(error))
            st.stop()
        if question or attachments:
            prompt_text = question or (
                "Please review my voice recording and attached files."
                if any(str(item["mime_type"]).startswith("audio/") for item in attachments)
                else "Please review the attached files."
            )
            save_message(DB_PATH, selected_id, "user", prompt_text, attachments)
            user_messages = load_messages(DB_PATH, selected_id)
            user_message = user_messages[-1]
            _render_chat_message(user_message, DB_PATH)

            with st.chat_message("assistant", avatar=":material/auto_awesome:"):
                key = _active_api_key()
                analytics = None
                try:
                    with st.status("Reading your connected streams", expanded=False) as status:
                        records = retrieve_inbox_context(
                            DB_PATH,
                            question,
                            selected_sources,
                            urgency=urgency_scope,
                            unread_only=unread_only,
                        )
                        analytics = analytics_snapshot(DB_PATH, selected_sources, records)
                        analytics.update(
                            {
                                "sources": selected_sources,
                                "question": question,
                                "urgency": urgency_scope,
                                "unread_only": unread_only,
                            }
                        )
                        if not key:
                            answer = (
                                "Your inbox data is ready, but Gemini is not configured. "
                                "Add a key in the sidebar to enable synthesized answers. "
                                f"I found {len(records)} matching item(s) in the selected sources."
                            )
                        else:
                            status.write(f"Retrieved {len(records)} matching item(s).")
                            answer, analytics = generate_answer(
                                DB_PATH,
                                selected_id,
                                question,
                                selected_sources,
                                urgency_scope,
                                unread_only,
                                messages,
                                attachments,
                                api_key=key,
                                model=model,
                            )
                            analytics.update(
                                {
                                    "sources": selected_sources,
                                    "question": question,
                                    "urgency": urgency_scope,
                                    "unread_only": unread_only,
                                }
                            )
                        status.update(label="Answer ready", state="complete")
                except Exception as error:
                    answer = f"I couldn't complete that request: {error}"
                    if analytics is None:
                        analytics = analytics_snapshot(DB_PATH, selected_sources, [])
                    analytics.update(
                        {
                            "sources": selected_sources,
                            "question": question,
                            "urgency": urgency_scope,
                            "unread_only": unread_only,
                        }
                    )
                st.markdown(answer)
                save_message(DB_PATH, selected_id, "assistant", answer, analytics=analytics)
                assistant_message = load_messages(DB_PATH, selected_id)[-1]
                _render_analytics(assistant_message["analytics"], assistant_message["id"], DB_PATH)


def run() -> None:
    main()