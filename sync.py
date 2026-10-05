import os
from pathlib import Path
from googleapiclient.discovery import build
from connectors import load_google_credentials
from database import insert_inbox_records, normalize_inbox_record
import sqlite3

def sync_google_streams(db_path = 'omnitwin.db'):
    creds = load_google_credentials()
    if not creds:
        print('No creds')
        return 0
    conn = sqlite3.connect(db_path)
    count = 0
    try:
        svc = build('gmail', 'v1', credentials=creds)
        msgs = svc.users().messages().list(userId='me', maxResults=5).execute().get('messages', [])
        for m in msgs:
            d = svc.users().messages().get(userId='me', id=m['id'], format='full').execute()
            h = d.get('payload', {}).get('headers', [])
            subj = next((x['value'] for x in h if x['name'].lower() == 'subject'), 'No Subject')
            snd = next((x['value'] for x in h if x['name'].lower() == 'from'), 'Unknown')
            rec = normalize_inbox_record(source='gmail', external_id=m['id'], item_type='email', payload={'sender': snd, 'subject': subj, 'snippet': d.get('snippet', '')})
            count += insert_inbox_records([rec], db_path)
        print(f'Synced {count} records.')
    except Exception as e:
        print(e)
    finally:
        conn.close()
    return count

if __name__ == '__main__':
    sync_google_streams()
