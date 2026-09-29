# ~/rfid_attendance_backend/automatic_absence_check.py

import sys
import os
from datetime import datetime, time, timedelta
import holidays as pyholidays # Renamed for clarity

# Get the current day of the week (Monday is 0 and Sunday is 6)
today = datetime.datetime.today().weekday()

# Check if today is a weekday (Monday=0 to Friday=4)
if today >= 5:
    print("Skipping job, today is a weekend.")
    # Exit the script if it's a weekend
    exit()

# Add the parent directory of this script to the Python path
# so it can find 'app' and 'models'
from app import app, db, Staff, Attendance, Leave # <-- Added 'Leave' model here

# Define the cut-off time for automatic absence
ABSENCE_CUTOFF_TIME = time(8, 0) # 08:00 AM

def check_and_mark_absent():
    with app.app_context():
        print(f"[{datetime.now()}] Running automatic absence check...")
        today = datetime.now().date() # Get today's date

        # Get South African public holidays for the current year
        sa_holidays = pyholidays.SouthAfrica(years=today.year)

        if today in sa_holidays:
            print(f"[{datetime.now()}] Today ({today}) is a public holiday in South Africa: {sa_holidays.get(today)}. Skipping absence check.")
            return # Exit the function if it's a public holiday

        # Find all active staff members
        active_staff = Staff.query.filter_by(is_active=True).all()

        for staff_member in active_staff:
            # We only consider marking as absent if they are currently OUT
            # and have not checked in today
            if staff_member.current_status == 'OUT' or \
               (staff_member.current_status == 'ON_LEAVE' and staff_member.leave_type == 'Absent (Auto)'):
                
                # Check if staff member has an 'IN' log entry for today
                has_checked_in_today = Attendance.query.filter_by(
                    staff_id=staff_member.id,
                    action_type='IN'
                ).filter(
                    Attendance.timestamp >= datetime.combine(today, time.min),
                    Attendance.timestamp <= datetime.combine(today, time.max)
                ).first()

                # If no check-in today and it's past the cutoff time, mark as absent
                if not has_checked_in_today and datetime.now().time() >= ABSENCE_CUTOFF_TIME:
                    
                    # 1. Check if a leave record for today already exists to prevent duplicates
                    existing_leave_record = Leave.query.filter_by(
                        staff_id=staff_member.id,
                        start_date=today,
                        end_date=today
                    ).first()

                    if not existing_leave_record:
                        # 2. Create a new entry in the Leave table
                        new_leave_record = Leave(
                            staff_id=staff_member.id,
                            leave_type='Absent (Auto)', # A specific type for this system-generated leave
                            start_date=today,
                            end_date=today,
                            reason='Automatically marked as absent for not checking in.',
                            approved=True # Automatically approve this system-generated leave
                        )
                        db.session.add(new_leave_record)
                        
                        # 3. Update the staff member's current status and leave type
                        staff_member.current_status = 'ON_LEAVE'
                        staff_member.leave_type = 'Absent (Auto)'
                        staff_member.last_check_in_out = datetime.now()
                        db.session.add(staff_member)
                        
                        print(f"  -> Marked {staff_member.name} as 'Absent (Auto)' and created a leave record.")
                    else:
                        print(f"  -> {staff_member.name} already has an automatic leave record for today. Skipping.")
                
        try:
            db.session.commit()
            print(f"[{datetime.now()}] Automatic absence check completed and changes committed.")
        except Exception as e:
            db.session.rollback()
            print(f"[{datetime.now()}] Error during automatic absence check: {e}")

if __name__ == '__main__':
    check_and_mark_absent()