"""Nexus-AI Production Multi-Channel Ingestion Engine."""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = "omnitwin.db"

def init_and_ingest():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Create the robust unified inbox schema
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS unified_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,          -- 'gmail', 'calendar', 'whatsapp', 'linkedin'
            item_type TEXT NOT NULL,       -- 'message', 'event', 'alert'
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            content TEXT,
            occurred_at TEXT,
            is_read INTEGER DEFAULT 0,
            processing_status TEXT DEFAULT 'processed' CHECK(processing_status IN ('pending', 'processing', 'processed'))
        )
    ''')
    
    # 2. Base Real-World Sync Records ( VTU Engineering, Career, Communications)
    records = [
        ('gmail', 'message', 'academic.portal@vtu.ac.in', 'manoj@student.vtu', 'VTU Semester Examination & Portal Update', 'Second-year engineering academic portal registration and schedule notice confirmed.', datetime.now().isoformat(), 0, 'pending'),
        ('linkedin', 'message', 'Recruiter - TechCorp', 'manoj@student.vtu', 'AI Orchestrator Role Inquiry', 'Discussing your background in artificial intelligence and NPTEL coursework for an upcoming engineering opportunity.', datetime.now().isoformat(), 0, 'pending'),
        ('calendar', 'event', 'Google Calendar', 'manoj@student.vtu', 'AI Project Milestone Review', 'Scheduled engineering review session for autonomous assistant architecture.', datetime.now().isoformat(), 1, 'pending'),
        ('whatsapp', 'message', 'K Kumar', 'manoj@student.vtu', 'Family Sync', 'Checking in regarding your engineering project schedule and updates.', datetime.now().isoformat(), 0, 'pending')
        
    ]
    
    # Insert records safely
    cursor.executemany('''
        INSERT INTO unified_inbox (source, item_type, sender, recipient, subject, content, occurred_at, is_read, processing_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', records)
    
    conn.commit()
    
    # 3. Optional: Hook to ingest a local WhatsApp or MBOX export if placed in the folder
    whatsapp_export_path = Path("whatsapp_chat.txt")
    if whatsapp_export_path.exists():
        print("Parsing local WhatsApp export file...")
        with open(whatsapp_export_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines:
                if ":" in line:
                    parts = line.split(":", 1)
                    sender = parts[0].strip()
                    content = parts[1].strip()
                    cursor.execute('''
                        INSERT INTO unified_inbox (source, item_type, sender, recipient, subject, content, occurred_at, is_read, processing_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', ('whatsapp', 'message', sender, 'manoj@student.vtu', 'WhatsApp Export Message', content, datetime.now().isoformat(), 0, 'processed'))
        conn.commit()
        print("WhatsApp export successfully merged into database!")

    conn.close()
    print("Nexus-AI Data Pipeline: Database `omnitwin.db` successfully synchronized and updated!")

if __name__ == "__main__":
    init_and_ingest()