import sqlite3
from pathlib import Path

from streamlit import connection


def init_db(db_path: str | Path = "omnitwin.db") -> sqlite3.Connection:
    """Initialize the local state database and return its connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS unified_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK (
                source IN ('gmail', 'calendar', 'whatsapp', 'sms', 'linkedin')
            ),
            external_id TEXT,
            item_type TEXT NOT NULL,
            account_id TEXT,
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            content TEXT,
            occurred_at TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
            processing_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                processing_status IN ('pending', 'processing', 'processed')
            ),
            executive_summary TEXT,
            urgency TEXT CHECK (urgency IN ('SPAM', 'FYI', 'URGENT')),
            urgency_score REAL CHECK (urgency_score BETWEEN 0 AND 100),
            processed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source, external_id)
        );

        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
            ),
            target TEXT,
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New conversation',
            model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            analytics_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    inbox_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(unified_inbox)")
    }
    new_columns = {
        "is_read": "INTEGER NOT NULL DEFAULT 0",
        "processing_status": "TEXT NOT NULL DEFAULT 'pending'",
        "executive_summary": "TEXT",
        "urgency": "TEXT",
        "urgency_score": "REAL",
        "processed_at": "TEXT",
    }
    for column, definition in new_columns.items():
        if column not in inbox_columns:
            connection.execute(
                f"ALTER TABLE unified_inbox ADD COLUMN {column} {definition}"
            )

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_unified_inbox_occurred_at
            ON unified_inbox (occurred_at);
        CREATE INDEX IF NOT EXISTS idx_unified_inbox_source_occurred_at
            ON unified_inbox (source, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_unified_inbox_pending
            ON unified_inbox (is_read, processing_status, occurred_at);

        CREATE INDEX IF NOT EXISTS idx_action_logs_status_created_at
            ON action_logs (status, created_at);
        CREATE INDEX IF NOT EXISTS idx_action_logs_action_type_created_at
            ON action_logs (action_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
            ON conversations (updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created_at
            ON chat_messages (conversation_id, created_at, id);
        """
    )
    connection.commit()
    return connection
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_unified_inbox_occurred_at
            ON unified_inbox (occurred_at);
        CREATE INDEX IF NOT EXISTS idx_unified_inbox_source_occurred_at
            ON unified_inbox (source, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_unified_inbox_pending
            ON unified_inbox (is_read, processing_status, occurred_at);

        CREATE INDEX IF NOT EXISTS idx_action_logs_status_created_at
            ON action_logs (status, created_at);
        CREATE INDEX IF NOT EXISTS idx_action_logs_action_type_created_at
            ON action_logs (action_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
            ON conversations (updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created_at
            ON chat_messages (conversation_id, created_at, id);
        """
    )
    
    # --- ADD THIS HELPER FUNCTION HERE ---
def seed_mock_data(conn):
    # Completely clear old records so nothing repeats or gets stuck
    conn.execute("DELETE FROM unified_inbox")
    conn.commit()

    mock_items = [
        # Gmail
        ("gmail", "email_01", "email", "careers@cognition-ai.blr", "manoj@gmail.com", "GenAI Intern Interview: Coding Round", "Hi Manoj, your OmniTwin AI project architecture impressed our engineering lead. We would like to schedule a 45-minute technical discussion this Thursday.", "2026-10-05 09:30:00", "URGENT", 95.0),
        ("gmail", "email_02", "email", "nptel.exams@iitm.ac.in", "manoj@gmail.com", "NPTEL AI Course: Score Card & Certificate", "Dear Manoj Kumar K, Congratulations! You have successfully completed the NPTEL course 'Artificial Intelligence: Concepts and Techniques' with a Elite score.", "2026-10-05 08:15:00", "FYI", 85.0),
        
        # WhatsApp (Real, distinct chat messages)
        ("whatsapp", "wa_01", "chat", "+919876543210", "Manoj", "Rahul (Project Mate)", "Bro, check the latest commit on GitHub. Fixed the async queue bug for the voice receptionist agent. Ready for testing!", "2026-10-05 11:10:00", "URGENT", 92.0),
        ("whatsapp", "wa_02", "chat", "+919988776655", "Manoj", "Indiranagar Dev Meetup", "Hey Manoj! Are you dropping by the GenAI meetup this Saturday at 3 PM? A few Series-A startup founders are attending.", "2026-10-04 19:45:00", "FYI", 80.0),
        ("whatsapp", "wa_03", "chat", "+919123456789", "Manoj", "Mom", "Don't stay up too late coding on your laptop again. Have you eaten your dinner properly?", "2026-10-05 20:30:00", "FYI", 70.0),

        # LinkedIn (Notifications, Connection Requests, Certificate posts, Recruiter Messages)
        ("linkedin", "li_01", "connection", "Priya Sharma", "Manoj Kumar", "New Connection Request", "Priya Sharma (HR Lead @ Koramangala AI) sent you a connection request: 'Hi Manoj, loved your multi-agent architecture project post.'", "2026-10-05 10:00:00", "URGENT", 94.0),
        ("linkedin", "li_02", "certificate_post", "Manoj Kumar K", "Network", "Certificate Achievement", "Manoj Kumar K posted: 'Thrilled to share that I have successfully completed the NPTEL certification in Artificial Intelligence: Concepts and Techniques with Elite status! 🚀🤖 #AI #ContinuousLearning #NPTEL'", "2026-10-05 09:00:00", "FYI", 90.0),
        ("linkedin", "li_03", "notification", "LinkedIn Notifications", "Manoj Kumar", "Profile View Milestone", "Your profile appeared in 64 search results this week by recruiters from top AI startups in Bengaluru.", "2026-10-04 14:20:00", "FYI", 60.0),
        ("linkedin", "li_04", "message", "Karan Mehta", "Manoj Kumar", "Direct Message", "Karan Mehta: 'Hey Manoj, are you open to discussing junior AI engineering roles or internships in tech startups around Indiranagar?'", "2026-10-04 16:30:00", "URGENT", 88.0),

        # Calendar
        ("calendar", "cal_01", "meeting", "Google Calendar", "Manoj Kumar", "System Architecture Review", "Reviewing low-latency RAG pipeline design and SQLite state persistence.", "2026-10-06 11:00:00", "URGENT", 90.0),
        ("calendar", "cal_02", "reminder", "Google Calendar", "Manoj Kumar", "Gym: Heavy Leg Session", "Squats, inclined leg press, and core stabilization.", "2026-10-05 18:00:00", "FYI", 40.0)
    ]
    
    conn.executemany("""
        INSERT OR REPLACE INTO unified_inbox (
            source, external_id, item_type, sender, recipient, subject, content, occurred_at, urgency, urgency_score, processing_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processed')
    """, mock_items)
    conn.commit()
if __name__ == "__main__":
    conn = init_db()         # This creates the table 'unified_inbox'
    seed_mock_data(conn)     # This populates the mock data
    conn.close()
    print("✅ Database initialized and seeded successfully!")