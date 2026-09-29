# helpers.py
from datetime import datetime, time, timedelta, date
from functools import wraps
from flask import redirect, url_for, flash
from flask_login import current_user
from rfid_attendance_backend.models import Staff, Shift, Attendance, Leave
from rfid_attendance_backend import login_manager, db
from holidays import SouthAfrica as ZA

# --- New Helper Functions for Days ---
def days_list_to_string(days_list):
    """
    Converts a list of day abbreviations (e.g., ['Mon', 'Wed']) to a comma-separated string.
    """
    if not days_list:
        return ""
    return ", ".join(days_list)

def days_string_to_list(days_string):
    """
    Converts a comma-separated string of day abbreviations to a list.
    """
    if not days_string:
        return []
    return [day.strip() for day in days_string.split(',')]

# --- Define the get_relevant_shift function ---
def get_relevant_shift(staff, timestamp):
    """
    Determines the relevant shift for a given staff member at a specific timestamp.
    """
    shift = Shift.query.filter_by(staff_id=staff.id).first()
    if shift:
        return shift
    
    # Fallback to a default shift if no specific one is found
    # This is an example, adjust based on your logic
    default_start = time(8, 0, 0)
    default_end = time(16, 0, 0)
    
    # Create a dictionary representing a dummy shift for consistency
    return {
        'id': None,
        'start_time': default_start,
        'end_time': default_end,
        'late_grace_period': 15,
        'leave_grace_period': 15
    }

# --- New function to process daily logs ---
def process_daily_logs(daily_logs, staff_list, public_holidays):
    """
    Processes daily attendance logs to determine check-in/out times, lateness, etc.
    """
    daily_report = {}
    
    # A mapping of staff ID to a dictionary of their daily logs
    logs_by_staff = {}
    for log in daily_logs:
        if log.staff_id not in logs_by_staff:
            logs_by_staff[log.staff_id] = []
        logs_by_staff[log.staff_id].append(log)

    for staff in staff_list:
        # Get all logs for this staff member for the day
        staff_logs = sorted(logs_by_staff.get(staff.id, []), key=lambda x: x.timestamp)
        
        check_in = None
        check_out = None
        late_check_in = False
        early_check_out = False

        if staff_logs:
            first_log = staff_logs[0]
            last_log = staff_logs[-1]

            # Check-in time is the first log of the day, unless it's an AUTO_OUT
            if first_log.action_type != 'AUTO_OUT':
                check_in = first_log.timestamp

            # Check-out time is the last log of the day
            if last_log.action_type not in ['AUTO_OUT', 'AUTO_OUT_PREVIOUS_DAY_STUCK']:
                check_out = last_log.timestamp
            else:
                # If last log is an auto-checkout, the second-to-last log might be a manual checkout
                if len(staff_logs) > 1 and staff_logs[-2].action_type not in ['AUTO_OUT', 'AUTO_OUT_PREVIOUS_DAY_STUCK']:
                    check_out = staff_logs[-2].timestamp
                else:
                    # If staff only checked in, use the auto-checkout time as the checkout time
                    if last_log.action_type in ['AUTO_OUT', 'AUTO_OUT_PREVIOUS_DAY_STUCK']:
                        check_out = last_log.timestamp
        
        # Check if the staff member was on leave
        on_leave = Leave.query.filter_by(staff_id=staff.id, start_date=first_log.date(), end_date=first_log.date()).first() if staff_logs else None
        
        # Get the relevant shift for today
        shift = get_relevant_shift(staff, daily_logs[0].date() if daily_logs else date.today())
        
        total_hours = None
        # Calculate total hours worked if both check-in and check-out exist
        if check_in and check_out:
            total_hours = (check_out - check_in).total_seconds() / 3600
        
        is_holiday = daily_logs[0].date() in public_holidays if daily_logs else False

        daily_report[staff.id] = {
            'staff': staff,
            'check_in': check_in,
            'check_out': check_out,
            'total_hours': total_hours,
            'late_check_in': late_check_in,
            'early_check_out': early_check_out,
            'on_leave': on_leave,
            'is_holiday': is_holiday
        }

    return daily_report

# --- Other helper functions and decorators ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or (hasattr(current_user, 'staff') and current_user.staff.role != 'admin'):
            flash('Admin access required.', 'danger')
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated_function

# Define the get_public_holidays function here
def get_public_holidays(year):
    sa_holidays = ZA(years=year)
    return list(sa_holidays.keys())