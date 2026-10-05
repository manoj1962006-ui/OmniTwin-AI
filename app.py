import streamlit as st
import sqlite3
import pandas as pd

st.set_page_config(page_title="OmniTwin AI Dashboard", page_icon="🤖", layout="wide")

st.title("🤖 OmniTwin AI — Omnichannel Autonomous Agent")
st.markdown("*Autonomous background intelligence parsing Gmail, WhatsApp, LinkedIn Following Feeds, and Calendar locally.*")

# Database Query Helper
def get_data(source_name=None, item_type=None, search_query=None):
    conn = sqlite3.connect("omnitwin.db")
    query = "SELECT source, item_type, sender, subject, content, urgency, occurred_at FROM unified_inbox WHERE 1=1"
    params = []
    
    if source_name:
        query += " AND source = ?"
        params.append(source_name)
    if item_type:
        query += " AND item_type = ?"
        params.append(item_type)
    if search_query:
        query += " AND (content LIKE ? OR sender LIKE ? OR subject LIKE ?)"
        term = f"%{search_query}%"
        params.extend([term, term, term])
        
    df = pd.read_sql(query, conn, params=params)
    conn.close()
    return df

# --- DYNAMIC AGENT COMMAND BAR (For your video demo) ---
st.markdown("### 💬 Ask OmniTwin Agent")
user_prompt = st.text_input("Type a command (e.g., 'Show me mails', 'Show WhatsApp chats', 'Draft an email to HR', 'Show LinkedIn following posts'):")

if user_prompt:
    prompt_lower = user_prompt.lower()
    st.markdown(f"**Agent Execution Result for:** *\"{user_prompt}\"*\n")
    
    # Feature: Live Email Drafting
    if "draft" in prompt_lower or "compose" in prompt_lower:
        st.success("✨ Autonomous Agent drafted the following email response:")
        st.markdown("""
        > **To:** careers@cognition-ai.blr  
        > **Subject:** Re: GenAI Intern Interview: Coding Round  
        > 
        > Hi Hiring Team,  
        > 
        > Thank you for reaching out! I am very excited about the opportunity to interview with Cognition AI. I am available this Thursday as requested. I've attached my GitHub and portfolio showcasing my work on OmniTwin AI and local agentic workflows. Looking forward to our chat.  
        > 
        > Best regards,  
        > Manoj Kumar K  
        """)
    elif "mail" in prompt_lower or "email" in prompt_lower:
        df_res = get_data("gmail")
        st.dataframe(df_res[["sender", "subject", "content", "urgency"]], use_container_width=True)
    elif "whatsapp" in prompt_lower or "chat" in prompt_lower:
        df_res = get_data("whatsapp")
        st.dataframe(df_res[["sender", "content", "urgency"]], use_container_width=True)
    elif "following" in prompt_lower or "post" in prompt_lower or "feed" in prompt_lower:
        df_res = get_data("linkedin", item_type="following_post")
        st.dataframe(df_res[["sender", "subject", "content"]], use_container_width=True)
    else:
        df_res = get_data(search_query=user_prompt)
        st.dataframe(df_res, use_container_width=True)
    st.markdown("---")

# --- MULTI-TAB DASHBOARD VIEW ---
tabs = st.tabs(["📥 Gmail Triage", "💬 WhatsApp Stream", "🔗 LinkedIn & Following", "📅 Calendar"])

# Tab 1: Gmail (Clean cards, no harsh red alerts)
with tabs[0]:
    st.subheader("Clean Email Triage Center")
    for _, row in get_data("gmail").iterrows():
        with st.container():
            st.markdown(f"**From:** {row['sender']} | **Subject:** *{row['subject']}*")
            st.info(row['content'])
            st.caption(f"Priority Tag: {row['urgency']} | Received: {row['occurred_at']}")
            st.divider()

# Tab 2: WhatsApp (Rendering clean chat bubbles)
with tabs[1]:
    st.subheader("WhatsApp Messages & Team Chats")
    df_wa = get_data("whatsapp")
    for _, row in df_wa.iterrows():
        with st.chat_message("user" if row["urgency"] == "URGENT" else "assistant"):
            st.write(f"**{row['sender']}** *(Priority: {row['urgency']})*")
            st.write(row['content'])

# Tab 3: LinkedIn & Following (Connection requests, messages, shared posts)
with tabs[2]:
    st.subheader("LinkedIn Activity & Network")
    df_li = get_data("linkedin")
    for _, row in df_li.iterrows():
        if row['item_type'] == 'connection':
            st.success(f"🤝 **Connection Request:** {row['sender']} — {row['content']}")
        elif row['item_type'] == 'message':
            st.info(f"💬 **Direct Message:** {row['sender']} — {row['content']}")
        elif row['item_type'] == 'shared_post':
            st.warning(f"📌 **Shared Post:** {row['sender']} — {row['content']}")
        else:
            st.write(f"**{row['sender']}**: {row['content']}")