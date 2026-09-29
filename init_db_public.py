import sqlite3
import os
import datetime

# Define the path for the database file
DATABASE = 'attendance.db'

def init_db():
    print(f"Checking for existing database '{DATABASE}'...")
    # If the database exists, we will remove it to apply the new schema
    if os.path.exists(DATABASE):
        os.remove(DATABASE)
        print(f"Deleted existing database '{DATABASE}' to apply new schema.")
    else:
        print(f"Database '{DATABASE}' not found. Creating a new one.")

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # Create staff table with new columns: is_active, current_status, last_check_in_out, AND leave_type
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rfid_uid TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1 NOT NULL, -- 1 for True (active), 0 for False (inactive)
            current_status TEXT DEFAULT 'OUT' NOT NULL, -- 'IN', 'OUT', or 'ON_LEAVE'
            last_check_in_out TEXT, -- Store as TEXT for datetime
            leave_type TEXT -- <--- ADDED THIS LINE for Staff table
        )
    ''')

    # Create attendance table with new columns: action_type and date
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rfid_uid TEXT NOT NULL,
            name TEXT,
            timestamp TEXT NOT NULL,
            action_type TEXT NOT NULL, -- <--- RENAMED 'status' to 'action_type'
            date TEXT NOT NULL, -- <--- ADDED THIS LINE for Attendance table (for filtering by date)
            staff_id INTEGER, -- Adding this if your Attendance model uses it (it should for FK)
            FOREIGN KEY(staff_id) REFERENCES staff(id) -- Added FK if not already there
        )
    ''')

    # Optional: Add some initial staff members for testing
    print("Adding initial staff members (if not already present)...")
    # Note: Added an empty string for leave_type for initial staff
    initial_staff = [
        ('DEMO0001', 'Demo User 1', 1, 'OUT', None, ''), # Fictional demo user
        ('AABBCCDD', 'Jane Smith', 1, 'OUT', None, ''), # Jane, active, OUT, no leave_type
        ('EEFF0011', 'John Doe', 0, 'OUT', None, ''), # John, inactive, OUT, no leave_type
        # IMPORTANT: Use fictional/demo RFID UIDs and names in public repositories.
    ]

    for uid, name, is_active, current_status, last_check_in_out_str, leave_type_str in initial_staff: # Updated for leave_type
        try:
            cursor.execute(
                "INSERT INTO staff (rfid_uid, name, is_active, current_status, last_check_in_out, leave_type) VALUES (?, ?, ?, ?, ?, ?)", # Updated for leave_type
                (uid, name, is_active, current_status, last_check_in_out_str, leave_type_str) # Updated for leave_type
            )
            print(f"Added: {name} ({uid}) - Active: {bool(is_active)}, Status: {current_status}")
        except sqlite3.IntegrityError:
            print(f"Staff member {name} ({uid}) already exists. Skipping.")

    conn.commit()
    conn.close()
    print(f"Database '{DATABASE}' initialized successfully with new schema.")

if __name__ == '__main__':
    init_db()
