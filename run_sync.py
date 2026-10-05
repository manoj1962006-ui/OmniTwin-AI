import sqlite3
from googleapiclient.discovery import build
from connectors import load_google_credentials

print("Loading credentials...")
creds = load_google_credentials()
if creds:
    svc = build("gmail", "v1", credentials=creds)
    results = svc.users().messages().list(userId="me", maxResults=5).execute()
    msgs = results.get("messages", [])
    
    conn = sqlite3.connect("omnitwin.db")
    cursor = conn.cursor()
    count = 0
    
    for m in msgs:
        d = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = d.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "No Subject")
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "Unknown")
        snippet = d.get("snippet", "")
        
        cursor.execute('''
            INSERT OR IGNORE INTO unified_inbox (source, item_type, sender, recipient, subject, content, occurred_at, is_read)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 0)
        ''', ("gmail", "email", sender, "me", subject, snippet))
        count += 1
        
    conn.commit()
    conn.close()
    print(f"Successfully synchronized {count} live records directly into database!")
else:
    print("No valid credentials found.")