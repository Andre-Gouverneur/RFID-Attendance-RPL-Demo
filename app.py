import os
import io
import logging

import pandas as pd
import holidays as pyholidays
import pytz

from collections import defaultdict
from datetime import datetime, date, time, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user, 
                         login_required, current_user)
from flask_migrate import Migrate
from flask_wtf import FlaskForm
from wtforms import StringField, TimeField, SubmitField, SelectMultipleField, widgets
from wtforms.validators import DataRequired, ValidationError
from wtforms.widgets import CheckboxInput, ListWidget
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from sqlalchemy import exc, and_, or_, func
from sqlalchemy.orm import joinedload, relationship
from sqlalchemy.exc import IntegrityError
from celery import Celery

def make_celery(app):
    celery_app = Celery(
        app.import_name,
        broker=app.config.get('CELERY_BROKER_URL')
    )
    celery_app.conf.update(app.config)

    # This class allows tasks to have access to the Flask app context
    class ContextTask(celery_app.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app.Task = ContextTask
    return celery_app

# The Flask app instance and its configuration must be defined first
app = Flask(__name__)
app.config['broker_url'] = 'redis://redis:6379/0'
app.config['result_backend'] = 'redis://redis:6379/0'

# Now you can create the Celery app instance using the defined function
celery_app = make_celery(app)

def get_public_holidays(year):
    # This will include the 'move to Monday' rule automatically
    za_holidays = pyholidays.ZA(years=year)
    
    # Get the dictionary items and sort them by date
    sorted_holidays = sorted(za_holidays.items())
    
    # Return the sorted list of (date, holiday_name) tuples
    return sorted_holidays

# Create a function to check for birthdays
def is_today_their_birthday(birthdate):
    """Checks if a given date's month and day match today's month and day."""
    if birthdate:
        today = date.today()
        # Compare only the month and day, ignoring the year
        return birthdate.month == today.month and birthdate.day == today.day
    return False

# Helper function to format timedelta into a human-readable string
def format_duration(td):
    if td is None:
        return "N/A"
    seconds = int(td.total_seconds())
    if seconds < 0:
        return "Invalid Time" # Should not happen if last_check_in_out is always in the past

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600: # Less than 1 hour
        minutes = seconds // 60
        remaining_seconds = seconds % 60
        return f"{minutes}m {remaining_seconds}s"
    elif seconds < 86400: # Less than 1 day
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    else: # 1 day or more
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h"

# Global variable to store the last scanned RFID UID specifically for the 'Add Staff' feature
last_scanned_uid_for_add_staff = None

# --- Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'attendance.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'your_super_secret_key_here' # IMPORTANT: Change this to a strong, random key!

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # This tells Flask-Login where to redirect if login is required

migrate = Migrate(app, db)

last_scanned_uid_for_add_staff = None

# Custom field for multiple checkboxes
class MultiCheckboxField(SelectMultipleField):
    """
    A multiple-select, except displays a list of checkboxes.
    Iterating the field will produce a list of String `_SelectField` objects.
    """
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()

# Your existing ShiftForm, updated
class ShiftForm(FlaskForm):
    name = StringField('Shift Name', validators=[DataRequired()])
    start_time = TimeField('Start Time (HH:MM)', validators=[DataRequired()], format='%H:%M')
    end_time = TimeField('End Time (HH:MM)', validators=[DataRequired()], format='%H:%M')
    grace_period_minutes = StringField('Grace Period (Minutes)', default='5')
    break_duration_minutes = StringField('Break Duration (Minutes)', default='60')

    days_of_week = MultiCheckboxField(
        'Days of Week',
        choices=[
            ('Mon', 'Monday'),
            ('Tue', 'Tuesday'),
            ('Wed', 'Wednesday'),
            ('Thu', 'Thursday'),
            ('Fri', 'Friday'),
            ('Sat', 'Saturday'),
            ('Sun', 'Sunday')
        ],
        default=['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
        validators=[DataRequired()]
    )
    submit = SubmitField('Save Shift')

# --- NEW: Database Model for Shifts ---

 #Helper functions to convert between list and string of days (These should be in app.py too)
def days_list_to_string(days_list):
    # Ensure consistent order (optional but good practice)
    ordered_days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    return ",".join(day for day in ordered_days if day in days_list)

def days_string_to_list(days_string):
    return days_string.split(',') if days_string else []

def days_string_to_list(days_str):
    """Converts a comma-separated string of days (e.g., 'Mon,Wed,Fri') to a list."""
    if days_str:
        return [day.strip() for day in days_str.split(',') if day.strip()]
    return []

class Shift(db.Model):
    __tablename__ = 'shifts'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    grace_period_minutes = db.Column(db.Integer, default=5, nullable=False)
    break_duration_minutes = db.Column(db.Integer, default=60, nullable=False)

    # NEW FIELD: stores a comma-separated string of applicable days
    # e.g., "Mon,Tue,Wed,Thu,Fri" or "Mon,Tue,Wed,Thu"
    days_of_week = db.Column(db.String(100), nullable=False, default="Mon,Tue,Wed,Thu,Fri")

    def __repr__(self):
        return f"<Shift {self.name} ({self.start_time}-{self.end_time})>"

# --- Database Model for Staff Members ---
class Staff(db.Model):
    __tablename__ = 'staff'
    id = db.Column(db.Integer, primary_key=True)
    rfid_uid = db.Column(db.String(50), unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    current_status = db.Column(db.String(10), default='OUT', nullable=False)
    last_check_in_out = db.Column(db.DateTime, nullable=True)
    leave_type = db.Column(db.String(50), nullable=True)
    
    # Corrected relationship. This is the only one linking to Attendance.
    attendance_records = db.relationship('Attendance', back_populates='staff_member')
    
    shift_id = db.Column(db.Integer, db.ForeignKey('shifts.id'), nullable=True)
    assigned_shift = db.relationship('Shift', backref='staff_members')
    last_rfid_scan_time = db.Column(db.DateTime, nullable=True)
    birthdate = db.Column(db.Date)

    def __repr__(self):
        return f"<Staff {self.name} ({self.rfid_uid})>"

# --- Database Model for Attendance Logs ---
class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    rfid_uid = db.Column(db.String(50), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.now, nullable=False)
    date = db.Column(db.Date, default=date.today, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)
    first_in = db.Column(db.DateTime, nullable=True)
    last_out = db.Column(db.DateTime, nullable=True)
    total_duration = db.Column(db.Integer, default=0, nullable=False)
    status = db.Column(db.String(50), default='Pending', nullable=False)
    comment = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    
    # Corrected relationship. This is the only one linking to Staff.
    staff_member = db.relationship('Staff', back_populates='attendance_records')

    def __repr__(self):
        return f"<Attendance {self.staff_id or 'UNKNOWN'} - {self.action_type} at {self.timestamp}>"
# --- Database Model for Users ---
class User(UserMixin, db.Model): # Inherit from db.Model and UserMixin
    __tablename__ = 'users' # Or choose a suitable table name
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False) # Store password hashes, not plain passwords
    is_admin = db.Column(db.Boolean, default=False) # Optional: for admin roles
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"

# --- Database Model for a Leave Request (MODIFIED) ---
class Leave(db.Model):
    __tablename__ = 'leave_requests'
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    # NEW: Link to the LeaveType model via a foreign key
    leave_type_id = db.Column(db.Integer, db.ForeignKey('leave_types.id', name='fk_leave_type'), nullable=False)
    
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False)
    reason = db.Column(db.Text, nullable=True)
    requested_at = db.Column(db.DateTime, default=datetime.now)

    staff = db.relationship('Staff', backref=db.backref('leave_requests', lazy=True))

    def __repr__(self):
        return f"<Leave {self.id} for Staff {self.staff_id} ({self.start_date} to {self.end_date})>"

# New form for adding leave types
class LeaveTypeForm(FlaskForm):
    name = StringField('Leave Type Name', validators=[DataRequired()])
    submit = SubmitField('Add Leave Type')

    def validate_name(self, field):
        if LeaveType.query.filter_by(name=field.data).first():
            raise ValidationError('This leave type already exists.')

class LeaveType(db.Model):
    __tablename__ = 'leave_types'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    leave_requests = relationship('Leave', backref='leave_type_obj', lazy=True)

@login_manager.user_loader
def load_user(user_id):
    # This should now query the database for the user by ID
    return User.query.get(int(user_id)) # Cast user_id to int as it comes as a string

def process_daily_logs(staff_member, daily_logs, report_date, holidays_data):
    """
    Processes a list of attendance logs for a single staff member for a single day
    and returns daily summary details, considering assigned shifts.
    """
    first_in = None
    last_out = None
    total_duration_seconds = 0
    daily_status = 'Absent'

    # Get the assigned shift for the staff member
    # If using daily shift assignments, you'd query a ShiftAssignment model here
    assigned_shift = staff_member.assigned_shift # Assuming one-to-many relationship

    # 1. Handle Public Holidays (Priority 1)
    if report_date in holidays_data:
        daily_status = 'Holiday'
        return {
            'name': staff_member.name,
            'date': report_date,
            'first_in': None,
            'last_out': None,
            'total_duration': 0,
            'status': daily_status,
            'lateness': None, # New fields
            'early_departure': None,
            'expected_in': None,
            'expected_out': None
        }

    # 2. Handle On Leave (Priority 2 - if you implement a Leave model)
    # This would involve querying your Leave model for this staff_member and report_date
    # For now, let's assume if there are no logs and it's not a holiday, they are Absent.
    # A full leave system would check if staff_member.is_on_leave(report_date)

    in_times = []
    out_times = []
    for log in daily_logs:
        if log.action_type in ['IN', 'CHECKED_IN_LATE']:
            in_times.append(log.timestamp)
        elif log.action_type == 'OUT':
            out_times.append(log.timestamp)
        elif log.action_type == 'AUTO_OUT' or log.action_type == 'AUTO_OUT_PREVIOUS_DAY_STUCK':
            out_times.append(log.timestamp) # Include auto-outs in calculations

    if in_times:
        first_in = min(in_times)
    if out_times:
        last_out = max(out_times)

    # Calculate Total Duration (between first_in and last_out)
    if first_in and last_out and last_out > first_in:
        total_duration_seconds = (last_out - first_in).total_seconds()
        daily_status = 'Present'
    elif first_in and not last_out:
        daily_status = 'Incomplete (In)' # Checked in but not out
    elif last_out and not first_in:
        daily_status = 'Incomplete (Out)' # Checked out but not in
    elif not daily_logs and staff_member.is_active:
        daily_status = 'Absent' # No logs for an active staff member
    elif not staff_member.is_active:
        daily_status = 'Inactive' # Staff member is marked inactive

    # --- NEW: Shift-based Calculations ---
    lateness = None
    early_departure = None
    expected_in = None
    expected_out = None

    if assigned_shift:
        expected_start_dt = datetime.combine(report_date, assigned_shift.start_time)
        expected_end_dt = datetime.combine(report_date, assigned_shift.end_time)

        expected_in = assigned_shift.start_time
        expected_out = assigned_shift.end_time

        # Calculate Lateness
        if first_in and first_in > (expected_start_dt + timedelta(minutes=assigned_shift.grace_period_minutes)):
            lateness_td = first_in - expected_start_dt
            lateness = int(lateness_td.total_seconds()) # Lateness in seconds

        # Calculate Early Departure
        # Only if there was a valid OUT scan and it's before the expected end time
        if last_out and last_out < (expected_end_dt - timedelta(minutes=assigned_shift.grace_period_minutes)):
            early_departure_td = expected_end_dt - last_out
            early_departure = int(early_departure_td.total_seconds()) # Early departure in seconds
        
        # Adjust total duration for break if applicable (This is a simplified approach)
        # More complex: sum durations of in/out pairs, subtract known breaks
        # For simplicity, if present, subtract fixed break duration from expected or actual duration
        if daily_status == 'Present' and assigned_shift.break_duration_minutes > 0:
            total_duration_seconds -= (assigned_shift.break_duration_minutes * 60)
            if total_duration_seconds < 0: total_duration_seconds = 0 # Ensure it doesn't go negative


    return {
        'name': staff_member.name,
        'date': report_date,
        'first_in': first_in,
        'last_out': last_out,
        'total_duration': total_duration_seconds,
        'status': daily_status,
        'lateness': lateness, # Pass new calculated values
        'early_departure': early_departure,
        'expected_in': expected_in, # Pass expected times for display
        'expected_out': expected_out
    }

# --- NEW: Decorator for Admin-Only Access ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        if not current_user.is_admin:
            flash('You do not have administrative privileges to access this page.', 'danger')
            return redirect(url_for('dashboard')) # Or some other appropriate page
        return f(*args, **kwargs)
    return decorated_function

@app.route('/delete_leave_type/<int:leave_type_id>', methods=['POST'])
@login_required
@admin_required
def delete_leave_type(leave_type_id):
    leave_type = LeaveType.query.get_or_404(leave_type_id)
    try:
        db.session.delete(leave_type)
        db.session.commit()
        flash(f'Leave type "{leave_type.name}" deleted successfully!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(f'Cannot delete leave type "{leave_type.name}" because it is currently in use.', 'danger')
    
    return redirect(url_for('manage_leave_types'))

# --- Route to manage leave types (NEW) ---
@app.route('/manage_leave_types', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_leave_types():
    form = LeaveTypeForm()
    if form.validate_on_submit():
        new_leave_type = LeaveType(name=form.name.data)
        db.session.add(new_leave_type)
        try:
            db.session.commit()
            flash(f"Leave type '{new_leave_type.name}' added successfully!", 'success')
            return redirect(url_for('manage_leave_types'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error adding leave type: {e}", 'danger')

    leave_types = LeaveType.query.order_by(LeaveType.name).all()
    return render_template('manage_leave_types.html', form=form, leave_types=leave_types)

# --- NEW: Admin User Management Route ---
@app.route('/manage_users', methods=['GET', 'POST'])
@login_required
@admin_required # Only admins can access this page
def manage_users():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add_user':
            username = request.form.get('username')
            password = request.form.get('password')
            is_admin_check = request.form.get('is_admin') == 'on' # Checkbox value

            if not username or not password:
                flash('Username and password are required.', 'danger')
            else:
                existing_user = User.query.filter_by(username=username).first()
                if existing_user:
                    flash('Username already exists. Please choose a different one.', 'danger')
                else:
                    new_user = User(username=username, is_admin=is_admin_check)
                    new_user.set_password(password)
                    db.session.add(new_user)
                    db.session.commit()
                    flash(f'User "{username}" created successfully!', 'success')
        elif action == 'reset_password':
            user_id = request.form.get('user_id')
            new_password = request.form.get('new_password')
            user_to_update = User.query.get(user_id)
            if user_to_update and new_password:
                user_to_update.set_password(new_password)
                db.session.commit()
                flash(f'Password for user "{user_to_update.username}" reset successfully!', 'success')
            else:
                flash('Error resetting password.', 'danger')
        elif action == 'toggle_admin':
            user_id = request.form.get('user_id')
            user_to_update = User.query.get(user_id)
            if user_to_update and user_to_update != current_user: # Prevent admin from revoking their own admin status
                user_to_update.is_admin = not user_to_update.is_admin
                db.session.commit()
                flash(f'Admin status for "{user_to_update.username}" toggled.', 'success')
            else:
                flash('Cannot change admin status for current user or user not found.', 'danger')
        elif action == 'delete_user':
            user_id = request.form.get('user_id')
            user_to_delete = User.query.get(user_id)
            if user_to_delete and user_to_delete != current_user: # Prevent admin from deleting their own account
                db.session.delete(user_to_delete)
                db.session.commit()
                flash(f'User "{user_to_delete.username}" deleted successfully.', 'success')
            else:
                flash('Cannot delete current user or user not found.', 'danger')

        return redirect(url_for('manage_users')) # Redirect to refresh the page after action

    users = User.query.all() # Fetch all users to display them
    return render_template('manage_users.html', users=users)

# --- Authentication Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard')) # Redirect if already logged in

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard')) # Redirect to dashboard after login
        else:
            flash('Invalid username or password.', 'danger')
            return render_template('login.html')

    return render_template('login.html')

@app.route('/logout')
@login_required # User must be logged in to log out
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# --- Dashboard Route ---
@app.route('/dashboard', methods=['GET'])
#@login_required
def dashboard():
    today_date = date.today()

    # NEW: A helper function to check for birthdays
    def is_today_their_birthday(birthdate):
        if birthdate and birthdate.month == today_date.month and birthdate.day == today_date.day:
            return True
        return False
    
    # NEW: A helper function to format timedelta objects, moved to the top
    def format_duration(duration: timedelta):
        if not isinstance(duration, timedelta) or duration.total_seconds() == 0:
            return "0 min"
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    
    # NEW: Create a list of dictionaries to easily add the new birthday attribute
    staff_members_data = []
    
    staff_members_all = Staff.query.order_by(Staff.name).all()
    total_staff = len(staff_members_all)
    sa_holidays = pyholidays.SouthAfrica(years=today_date.year)
    is_today_a_holiday = today_date in sa_holidays
    today_holiday_name = sa_holidays.get(today_date)
    
    # NEW: Logic to calculate total lunch duration for today
    lunch_records = Attendance.query.filter(
        and_(
            Attendance.date == today_date,
            or_(
                Attendance.action_type == 'LUNCH_OUT',
                Attendance.action_type == 'LUNCH_IN'
            )
        )
    ).order_by(Attendance.timestamp).all()
    
    lunch_durations = defaultdict(timedelta)
    last_lunch_out = {}
    for record in lunch_records:
        staff_id = record.staff_id
        if record.action_type == 'LUNCH_OUT':
            last_lunch_out[staff_id] = record.timestamp
        elif record.action_type == 'LUNCH_IN' and staff_id in last_lunch_out:
            lunch_duration = record.timestamp - last_lunch_out[staff_id]
            lunch_durations[staff_id] += lunch_duration
            del last_lunch_out[staff_id]
    
    staff_in = []
    staff_out = []
    staff_on_leave = []
    staff_on_lunch = []
    staff_inactive = []
    current_status_counts = {
        'IN': 0, 'ON_LUNCH': 0, 'OUT': 0, 'ON_LEAVE': 0, 'OFF_SCHEDULE': 0
    }

    for staff_member in staff_members_all:
        staff_data = staff_member.__dict__.copy()
        staff_data['is_birthday'] = is_today_their_birthday(staff_member.birthdate)

        current_status_counts[staff_member.current_status] += 1
        staff_data['time_in_state'] = "N/A"
        
        # Calculate total lunch duration for TODAY.
        total_lunch_today = lunch_durations.get(staff_member.id, timedelta(0))
        
        # Add duration for any ongoing lunch breaks
        if staff_member.current_status == 'ON_LUNCH' and staff_member.last_check_in_out:
            current_lunch_duration = datetime.now() - staff_member.last_check_in_out
            total_lunch_today += current_lunch_duration
            
        staff_data['total_lunch_duration'] = format_duration(total_lunch_today)
        
        # Calculate time in state only for active staff
        if staff_member.is_active and staff_member.last_check_in_out:
            duration: timedelta = datetime.now() - staff_member.last_check_in_out
            staff_data['time_in_state'] = format_duration(duration)

        if staff_member.is_active:
            if staff_member.current_status == 'IN':
                staff_in.append(staff_data)
            elif staff_member.current_status == 'OUT':
                staff_out.append(staff_data)
            elif staff_member.current_status == 'ON_LUNCH':
                staff_on_lunch.append(staff_data)
            elif staff_member.current_status == 'ON_LEAVE':
                if staff_member.leave_type == 'Absent (Auto)':
                    duration: timedelta = datetime.now() - staff_member.last_check_in_out
                    days, seconds = duration.days, duration.seconds
                    hours = days * 24 + seconds // 3600
                    minutes = (seconds % 3600) // 60
                    staff_data['time_in_state'] = f"{int(hours)}h {int(minutes)}m (Auto Absent)"
                staff_on_leave.append(staff_data)
        else:
            staff_inactive.append(staff_data)
        
        staff_members_data.append(staff_data)

    page = request.args.get('page', 1, type=int)
    per_page = 20 
    today = datetime.now().date()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    today_attendance_logs_pagination = Attendance.query \
        .filter(Attendance.timestamp >= today_start, Attendance.timestamp <= today_end) \
        .order_by(Attendance.timestamp.desc()) \
        .paginate(page=page, per_page=per_page, error_out=False)
    today_attendance_logs = today_attendance_logs_pagination.items
    
    # FINAL AND CORRECT RETURN STATEMENT
    return render_template(
        'dashboard.html',
        is_today_a_holiday=is_today_a_holiday,
        today_holiday_name=today_holiday_name,
        today_attendance_logs=today_attendance_logs,
        today_attendance_logs_pagination=today_attendance_logs_pagination,
        today=today_date,
        staff_in=staff_in,
        staff_out=staff_out,
        staff_on_leave=staff_on_leave,
        staff_on_lunch=staff_on_lunch,
        staff_inactive=staff_inactive,
        total_staff=total_staff,
        lunch_durations=lunch_durations,
        staff_members=staff_members_data # NEW: Pass the new list of dictionaries
    )
    
# --- Helper function for Summary Reports (defined globally) ---
def process_staff_logs(staff_id, logs, summary_dict):
    first_in_time = None
    last_out_time = None
    total_duration_seconds = 0
    
    # Filter logs to ensure we only consider 'IN' and 'OUT' for calculations
    in_logs = sorted([log for log in logs if log.action_type == 'IN'], key=lambda x: x.timestamp)
    out_logs = sorted([log for log in logs if log.action_type == 'OUT'], key=lambda x: x.timestamp)

    if in_logs:
        first_in_time = in_logs[0].timestamp
    
    if out_logs:
        last_out_time = out_logs[-1].timestamp
    
    # Calculate total work duration by pairing IN and OUT events
    current_in_time = None
    for log in sorted(logs, key=lambda x: x.timestamp): # Iterate through all logs chronologically
        if log.action_type == 'IN':
            current_in_time = log.timestamp
        elif log.action_type == 'OUT' and current_in_time: # Only calculate if there's a preceding IN
            total_duration_seconds += (log.timestamp - current_in_time).total_seconds()
            current_in_time = None # Reset for next IN

    staff_name = "Unknown"
    staff_obj = Staff.query.get(staff_id)
    if staff_obj:
        staff_name = staff_obj.name

    total_hours = total_duration_seconds / 3600

    summary_dict[staff_id] = {
        'name': staff_name,
        'first_in': first_in_time,
        'last_out': last_out_time,
        'total_hours': round(total_hours, 2)
    }

@app.route('/summary_reports', methods=['GET', 'POST'])
@login_required
@admin_required
def summary_reports():
    staff_members = Staff.query.order_by(Staff.name).all()

    if request.method == 'POST':
        selected_staff_id = request.form.get('staff_id', 'all')
        start_date_str = request.form.get('start_date', (date.today() - timedelta(days=30)).strftime('%Y-%m-%d'))
        end_date_str = request.form.get('end_date', date.today().strftime('%Y-%m-%d'))
    else: # GET request
        selected_staff_id = request.args.get('staff_id', 'all')
        start_date_str = request.args.get('start_date', (date.today() - timedelta(days=30)).strftime('%Y-%m-%d'))
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash("Invalid date format. Using default dates.", 'danger')
        start_date = (date.today() - timedelta(days=30)).date()
        end_date = date.today().date()

    if start_date > end_date:
        flash("Start date cannot be after end date. Adjusting end date.", 'warning')
        end_date = start_date

    holidays_in_report_period = {}
    relevant_years = range(start_date.year, end_date.year + 1)
    sa_holidays = pyholidays.SouthAfrica(years=relevant_years)
    for d in [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]:
        if d in sa_holidays:
            holidays_in_report_period[d] = sa_holidays[d]

    # Filter staff if a specific one is selected
    filtered_staff_list = []
    if selected_staff_id != 'all':
        try:
            staff_id_int = int(selected_staff_id)
            staff_member_obj = db.session.get(Staff, staff_id_int)
            if staff_member_obj:
                filtered_staff_list.append(staff_member_obj)
            else:
                flash("Selected staff member not found. Displaying all staff.", 'warning')
                selected_staff_id = 'all'
                filtered_staff_list = staff_members # Fallback to all staff
        except ValueError:
            flash("Invalid staff selection. Displaying all staff.", 'warning')
            selected_staff_id = 'all'
            filtered_staff_list = staff_members # Fallback to all staff
    else:
        filtered_staff_list = staff_members # All staff

    # --- NEW CORE LOGIC FOR DAILY SUMMARIES ---
    daily_summary_records = []
    
    # Pre-fetch all relevant attendance records for the entire filtered period and staff
    # This is more efficient than querying inside the inner loop
    attendance_query = Attendance.query.filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date
    )
    if selected_staff_id != 'all':
        attendance_query = attendance_query.filter(Attendance.staff_id == int(selected_staff_id))

    all_logs_in_period = attendance_query.order_by(
        Attendance.staff_id, Attendance.date, Attendance.timestamp
    ).all()

    # Group logs by staff_id and date for easy access
    grouped_logs = defaultdict(lambda: defaultdict(list))
    for log in all_logs_in_period:
        grouped_logs[log.staff_id][log.date].append(log)


    for staff_member in filtered_staff_list:
        current_date = start_date
        while current_date <= end_date:
            # Get logs for this specific staff member on this specific date
            daily_logs_for_staff = grouped_logs.get(staff_member.id, {}).get(current_date, [])
            
            # Process these daily logs
            summary_entry = process_daily_logs(staff_member, daily_logs_for_staff, current_date, holidays_in_report_period)
            daily_summary_records.append(summary_entry)
            
            current_date += timedelta(days=1)

    # Sort the final list for consistent display
    daily_summary_records.sort(key=lambda x: (x['name'], x['date']))

    return render_template(
        'summary_reports.html',
        all_staff=staff_members,
        summary_data=daily_summary_records, # Pass the new list of daily summaries
        selected_staff_id=selected_staff_id,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        holidays_in_report_period=holidays_in_report_period # Pass this to display the holiday banner
    )

# --- Export Summary Reports Route (needs similar updates) ---
@app.route('/export_summary_reports', methods=['POST'])
@login_required
@admin_required
def export_summary_reports():
    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    selected_staff_id = request.form.get('staff_id')

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date format. Using default dates.', 'danger')
        return redirect(url_for('summary_reports'))

    if start_date > end_date:
        flash("Start date cannot be after end date. Adjusting end date.", 'warning')
        end_date = start_date

    holidays_in_report_period = {}
    relevant_years = range(start_date.year, end_date.year + 1)
    sa_holidays = pyholidays.SouthAfrica(years=relevant_years)
    for d in [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]:
        if d in sa_holidays:
            holidays_in_report_period[d] = sa_holidays[d]

    # Filter staff if a specific one is selected
    staff_members = Staff.query.order_by(Staff.name).all() # Need all staff for iteration
    filtered_staff_list = []
    if selected_staff_id != 'all':
        try:
            staff_id_int = int(selected_staff_id)
            staff_member_obj = db.session.get(Staff, staff_id_int)
            if staff_member_obj:
                filtered_staff_list.append(staff_member_obj)
            else:
                flash("Selected staff member not found. Exporting all staff.", 'warning')
                selected_staff_id = 'all'
                filtered_staff_list = staff_members # Fallback to all staff
        except ValueError:
            flash("Invalid staff selection. Exporting all staff.", 'warning')
            selected_staff_id = 'all'
            filtered_staff_list = staff_members # Fallback to all staff
    else:
        filtered_staff_list = staff_members # All staff

    # --- NEW CORE LOGIC FOR DAILY SUMMARIES FOR EXPORT ---
    daily_summary_records_for_export = []
    
    # Pre-fetch all relevant attendance records
    attendance_query = Attendance.query.filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date
    )
    if selected_staff_id != 'all':
        attendance_query = attendance_query.filter(Attendance.staff_id == int(selected_staff_id))

    all_logs_in_period = attendance_query.order_by(
        Attendance.staff_id, Attendance.date, Attendance.timestamp
    ).all()

    grouped_logs = defaultdict(lambda: defaultdict(list))
    for log in all_logs_in_period:
        grouped_logs[log.staff_id][log.date].append(log)

    for staff_member in filtered_staff_list:
        current_date = start_date
        while current_date <= end_date:
            daily_logs_for_staff = grouped_logs.get(staff_member.id, {}).get(current_date, [])
            summary_entry = process_daily_logs(staff_member, daily_logs_for_staff, current_date, holidays_in_report_period)
            daily_summary_records_for_export.append(summary_entry)
            current_date += timedelta(days=1)
    
    daily_summary_records_for_export.sort(key=lambda x: (x['name'], x['date'])) # Sort for consistent export

    # Prepare data for DataFrame
    records_for_df = []
    for data in daily_summary_records_for_export: # Loop through the daily summaries
        # Format duration for export
        formatted_duration = 'N/A'
        if data['total_duration'] is not None and data['total_duration'] > 0:
            hours = int(data['total_duration'] // 3600)
            minutes = int((data['total_duration'] % 3600) // 60)
            formatted_duration = f"{hours:02d}:{minutes:02d}"

        records_for_df.append({
            'Staff Name': data['name'],
            'Date': data['date'].strftime('%Y-%m-%d'), # Add Date column
            'First In': data['first_in'].strftime('%H:%M:%S') if data['first_in'] else 'N/A', # Time only
            'Last Out': data['last_out'].strftime('%H:%M:%S') if data['last_out'] else 'N/A', # Time only
            'Total Work Duration': formatted_duration, # Use formatted duration
            'Status': data['status'] # Add Status column
        })

    if not records_for_df:
        flash('No data to export for the selected criteria.', 'info')
        return redirect(url_for('summary_reports'))

    df = pd.DataFrame(records_for_df)

    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='xlsxwriter')
    df.to_excel(writer, index=False, sheet_name='Summary Report')
    writer.close()

    output.seek(0)
    filename = f"summary_report_{start_date_str}_to_{end_date_str}.xlsx"

    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name=filename)

# --- API Endpoints for RFID Reader ---

# New API endpoint for the 'Add Staff' page to retrieve the scanned UID
@app.route('/api/get_scanned_uid_for_add_staff', methods=['GET'])
@login_required # Ensure only logged-in users can access
@admin_required
def get_scanned_uid_for_add_staff():
    global last_scanned_uid_for_add_staff
    if last_scanned_uid_for_add_staff:
        uid = last_scanned_uid_for_add_staff
        # We clear it after sending it once to ensure we get the *next* scan if user re-scans
        last_scanned_uid_for_add_staff = None
        return jsonify({"uid": uid})
    return jsonify({"uid": None}) # Return None if no UID has been scanned yet

# New API endpoint to explicitly clear the stored UID (e.g., when scan button is clicked)
@app.route('/api/clear_scanned_uid_for_add_staff', methods=['POST'])
@login_required # Ensure only logged-in users can access
@admin_required
def clear_scanned_uid_for_add_staff():
    global last_scanned_uid_for_add_staff
    last_scanned_uid_for_add_staff = None
    return jsonify({"message": "UID capture state cleared"})
    
# Log Attendance Route and Logic
@app.route('/api/log_attendance', methods=['POST'])
def log_attendance():
    print(f"Incoming data from RFID reader: {request.json}")
    data = request.get_json()
    out_status = "UNKNOWN"
    rfid_uid = data.get('rfid_uid')

    if not rfid_uid:
        logging.error("log_attendance: No RFID UID provided in request.")
        return jsonify({"message": "RFID UID is required in the request.", "action_status": "ERROR"}), 400

    now = datetime.now()
    DEBOUNCE_SECONDS = 5
    current_date_obj = date.today()
    current_time_obj = now.time()
    current_weekday_abbr = now.strftime('%a')

    message_for_response = ""
    action_status_for_response = ""
    log_action = "DENIED"
    http_status_code = 403

    try:
        staff = Staff.query.filter_by(rfid_uid=rfid_uid).with_for_update().first()
        print(f"DEBUG: Staff member found: {staff.name if staff else 'None'}")

        # NEW DEBUG CODE
        logging.info(f"DEBUG: Processing scan for Staff ID {staff.id if staff else 'None'}, is_active: {staff.is_active if staff else 'N/A'}, shift_id: {staff.shift_id if staff else 'N/A'}, current_status: {staff.current_status if staff else 'N/A'}")

        if not staff:
            global last_scanned_uid_for_add_staff
            last_scanned_uid_for_add_staff = rfid_uid
            log_action = "UNKNOWN"
            message_for_response = f"Unknown RFID tag: {rfid_uid}."
            action_status_for_response = "UNKNOWN_TAG"
            http_status_code = 404
            unknown_tag_log = Attendance(
                staff_id=None,
                rfid_uid=rfid_uid,
                timestamp=now,
                date=current_date_obj,
                action_type=log_action,
                status='Unknown Tag'
            )
            db.session.add(unknown_tag_log)
            db.session.commit()
            logging.warning(f"[{now}] log_attendance: {message_for_response}")
            return jsonify({"message": message_for_response, "action_status": action_status_for_response, "rfid_uid": rfid_uid}), http_status_code

        if staff.last_rfid_scan_time:
            time_since_last_scan = now - staff.last_rfid_scan_time
            if time_since_last_scan < timedelta(seconds=DEBOUNCE_SECONDS):
                logging.info(f"[{now}] log_attendance: Debounced scan for {staff.name} (RFID {rfid_uid}). Too soon: {time_since_last_scan.total_seconds():.2f}s. Returning 200.")
                return jsonify({'message': 'Scan ignored (too soon)'}), 200
        
        staff.last_rfid_scan_time = now
        db.session.add(staff)

        if not staff.is_active:
            message_for_response = f"Scan Denied for {staff.name}: Staff member is inactive."
            action_status_for_response = "DENIED_DISABLED_TAG"
            log_action = "DENIED"
            http_status_code = 403
        else:
            holidays = get_public_holidays(current_date_obj.year)
            if current_date_obj in holidays:
                message_for_response = f"Scan Denied for {staff.name}: Today is a Public Holiday ({holidays[current_date_obj]})."
                action_status_for_response = "DENIED_HOLIDAY"
                log_action = "DENIED"
                http_status_code = 403
            else:
                leave_record = Leave.query.filter(
                    Leave.staff_id == staff.id,
                    Leave.start_date <= current_date_obj,
                    Leave.end_date >= current_date_obj,
                    Leave.status == 'approved'
                ).first()

                if leave_record:
                    message_for_response = f"Scan Denied for {staff.name}: Currently on approved leave until {leave_record.end_date.strftime('%Y-%m-%d')}."
                    action_status_for_response = "DENIED_ON_LEAVE"
                    log_action = "DENIED"
                    http_status_code = 403
                    staff.current_status = 'ON_LEAVE'
                    db.session.add(staff)
                else:
                    relevant_shift = None
                    staff_assigned_shifts = []
                    if staff.shift_id:
                        primary_shift = db.session.get(Shift, staff.shift_id)
                        if primary_shift:
                            staff_assigned_shifts.append(primary_shift)

                    for shift in staff_assigned_shifts:
                        if current_weekday_abbr in days_string_to_list(shift.days_of_week):
                            shift_start_dt = datetime.combine(current_date_obj, shift.start_time)
                            shift_end_dt = datetime.combine(current_date_obj, shift.end_time)
                            
                            # For overnight shifts
                            if shift_end_dt < shift_start_dt:
                                if current_time_obj < shift.start_time:
                                    shift_start_dt = datetime.combine(current_date_obj - timedelta(days=1), shift.start_time)
                                shift_end_dt += timedelta(days=1)

                            grace_period_td = timedelta(minutes=shift.grace_period_minutes)
                            # Define the "early scan" window - allowing scans up to 180 minutes early
                            early_scan_window_td = timedelta(minutes=180)
                            
                            # The full check-in window
                            shift_scan_window_start = shift_start_dt - early_scan_window_td
                            shift_scan_window_end = shift_end_dt + grace_period_td

                            # Check if the current time falls within this expanded window
                            if shift_scan_window_start <= now <= shift_scan_window_end:
                                relevant_shift = shift
                                break
                    
                    # This debug line will show if a shift was found.
                    print(f"DEBUG: Relevant shift found: {relevant_shift.name if relevant_shift else 'None'}")

                    if relevant_shift is None:
                        message_for_response = f"Scan Denied for {staff.name}: No relevant shift found."
                        action_status_for_response = "DENIED_NO_SHIFT"
                        log_action = "DENIED"
                        http_status_code = 403
                    else:
                        # --- IN/LUNCH/OUT LOGIC BLOCK ---
                        # First, check if the staff member is checking in for the first time on their shift.
                        print(f"DEBUG: Staff status from database is '{staff.current_status}'")
                        if staff.current_status == 'OUT' or staff.current_status == 'ON_LEAVE' or staff.current_status == 'OFF_SCHEDULE':
                            log_action = 'IN'
                            status_type = 'On Time'
                            shift_start_compare_dt = datetime.combine(current_date_obj, relevant_shift.start_time)
                            if relevant_shift.end_time < relevant_shift.start_time and current_time_obj < relevant_shift.start_time:
                                shift_start_compare_dt = datetime.combine(current_date_obj - timedelta(days=1), relevant_shift.start_time)
                            grace_period_end_dt = shift_start_compare_dt + timedelta(minutes=relevant_shift.grace_period_minutes)
                            if now < shift_start_compare_dt:
                                status_type = 'Early In'
                            elif now > grace_period_end_dt:
                                status_type = 'Late'

                            attendance_segment_record = Attendance(
                                staff_id=staff.id,
                                rfid_uid=rfid_uid,
                                timestamp=now,
                                date=current_date_obj,
                                action_type=log_action,
                                first_in=now,
                                last_out=None,
                                total_duration=0,
                                status=f'Incomplete (In - {status_type})'
                            )
                            db.session.add(attendance_segment_record)
                            staff.current_status = 'IN'
                            staff.last_check_in_out = now
                            db.session.add(staff)
                            message_for_response = f"{staff.name} checked IN successfully ({status_type})."
                            action_status_for_response = "CHECKED_IN"
                            http_status_code = 200

                        # Check for a final check-out based on the 15:00 rule or the end of the shift.
                        # This condition must come before the generic "IN" check to prioritize the final check-out.
                        elif staff.current_status == 'IN' and (now.time() >= time(15, 0, 0) or now.time() >= relevant_shift.end_time):
                            log_action = 'OUT'
                            latest_open_in_record = Attendance.query.filter(
                                Attendance.staff_id == staff.id,
                                Attendance.date == current_date_obj,
                                Attendance.first_in.isnot(None),
                                Attendance.last_out.is_(None)
                            ).order_by(Attendance.timestamp.desc()).first()

                            if latest_open_in_record:
                                latest_open_in_record.last_out = now
                                duration = now - latest_open_in_record.first_in
                                latest_open_in_record.total_duration = int(duration.total_seconds())

                                out_status = 'On Time Out'
                                shift_end_compare_dt = datetime.combine(current_date_obj, relevant_shift.end_time)
                                if relevant_shift.end_time < relevant_shift.start_time:
                                    shift_end_compare_dt += timedelta(days=1)
                                if now < shift_end_compare_dt:
                                    out_status = 'Early Out'
                                elif now > shift_end_compare_dt:
                                    out_status = 'Late Out'

                                latest_open_in_record.status = f'Present ({out_status})'
                                db.session.add(latest_open_in_record)

                            staff.current_status = 'OUT'
                            staff.last_check_in_out = now
                            db.session.add(staff)
                            message_for_response = f"{staff.name} checked OUT successfully ({out_status})."
                            action_status_for_response = "CHECKED_OUT"
                            http_status_code = 200

                        # This block handles all other 'IN' scans that aren't a final check-out, so they must be a lunch scan.
                        elif staff.current_status == 'IN':
                            log_action = 'LUNCH_OUT'
                            lunch_out_record = Attendance(
                                staff_id=staff.id,
                                rfid_uid=rfid_uid,
                                timestamp=now,
                                date=current_date_obj,
                                action_type=log_action,
                                status='Out for Lunch'
                            )
                            db.session.add(lunch_out_record)
                            staff.current_status = 'ON_LUNCH'
                            staff.last_check_in_out = now
                            db.session.add(staff)
                            message_for_response = f"{staff.name} is now on LUNCH break."
                            action_status_for_response = "LUNCH_OUT"
                            http_status_code = 200

                        # This is the final state, where the staff member is returning from lunch.
                        elif staff.current_status == 'ON_LUNCH':
                            log_action = 'LUNCH_IN'
                            lunch_in_record = Attendance(
                                staff_id=staff.id,
                                rfid_uid=rfid_uid,
                                timestamp=now,
                                date=current_date_obj,
                                action_type=log_action,
                                status='Back from Lunch'
                            )
                            db.session.add(lunch_in_record)
                            staff.current_status = 'IN'
                            staff.last_check_in_out = now
                            db.session.add(staff)
                            message_for_response = f"{staff.name} is back from LUNCH."
                            action_status_for_response = "LUNCH_IN"
                            http_status_code = 200

                        # Fallback for any other status, which should not happen in normal operation.
                        else:
                            message_for_response = f"Scan Denied for {staff.name}: Unexpected current status '{staff.current_status}'. Contact admin."
                            action_status_for_response = "DENIED_UNEXPECTED_STATUS"
                            log_action = "DENIED"
                            http_status_code = 403

        if action_status_for_response.startswith("DENIED") or action_status_for_response.startswith("CHECKED_OUT_FORCED"):
            event_log = Attendance(
                staff_id=staff.id if staff else None,
                rfid_uid=rfid_uid,
                timestamp=now,
                date=current_date_obj,
                action_type=log_action,
                status=action_status_for_response
            )
            db.session.add(event_log)

        db.session.commit()
        logging.info(f"[{now}] log_attendance: Final transaction successful for {staff.name if staff else 'Unknown'}. Action: {action_status_for_response}.")
        return jsonify({"message": message_for_response, "action_status": action_status_for_response}), http_status_code

    except exc.SQLAlchemyError as e:
        db.session.rollback()
        logging.error(f"[{now}] log_attendance: Database error during attendance log for RFID {rfid_uid}: {e}", exc_info=True)
        return jsonify({"message": "Database error during attendance log.", "action_status": "ERROR_DB"}), 500
    except Exception as e:
        db.session.rollback()
        logging.error(f"[{now}] log_attendance: An unexpected error occurred during attendance log for RFID {rfid_uid}: {e}", exc_info=True)
        return jsonify({"message": f"An unexpected error occurred: {e}", "action_status": "ERROR_UNEXPECTED"}), 500

# --- Staff List API Endpoint (for ESP32 to fetch known users) ---
@app.route('/staff', methods=['GET'])
def get_staff():
    active_staff = Staff.query.filter_by(is_active=True).all()
    staff_data = []
    for s in active_staff:
        staff_data.append({
            "rfid_uid": s.rfid_uid,
            "name": s.name,
            "is_active": s.is_active
        })
    return jsonify(staff_data)

# --- Web Interface Routes ---

# Main Staff Management Page - Lists all staff (NOW PROTECTED)
@app.route('/staff_management', methods=['GET', 'POST'])
@login_required
@admin_required
def staff_management():
    if request.method == 'POST':
        name = request.form.get('name').strip()
        rfid_uid = request.form.get('rfid_uid').strip()
        is_active = True if request.form.get('is_active') == 'on' else False
        initial_status = request.form.get('initial_status')
        # Get leave_type only if status is ON_LEAVE
        initial_leave_type = request.form.get('initial_leave_type') if initial_status == 'ON_LEAVE' else None
        birthdate_str = request.form.get('birthdate')

        # New code to handle the birthdate
        birthdate = None
        if birthdate_str:
            try:
                birthdate = datetime.strptime(birthdate_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format for birthdate.', 'danger')
                return redirect(url_for('staff_management'))


        if not name or not rfid_uid:
            flash('Name and RFID UID are required.', 'danger')
        else:
            existing_rfid = Staff.query.filter_by(rfid_uid=rfid_uid).first()
            if existing_rfid:
                flash(f'RFID UID {rfid_uid} already exists for {existing_rfid.name}.', 'danger')
            else:
                try:
                    last_check_in_out = datetime.now() if initial_status == 'IN' else None

                    new_staff = Staff(
                        name=name,
                        rfid_uid=rfid_uid,
                        current_status=initial_status,
                        last_check_in_out=last_check_in_out,
                        is_active=is_active,
                        leave_type=initial_leave_type # Save the leave type
                    )
                    db.session.add(new_staff)
                    db.session.commit()
                    flash(f'Staff member {name} added successfully!', 'success')
                    return redirect(url_for('staff_management'))
                except Exception as e:
                    db.session.rollback()
                    flash(f'Error adding staff member: {str(e)}', 'danger')

    all_staff = Staff.query.order_by(Staff.name).all()
    return render_template('staff_management.html', all_staff=all_staff)

# Route to add a new staff member (NOW PROTECTED)
@app.route('/add_staff', methods=['GET', 'POST'])
@login_required # Only accessible if logged in
@admin_required
def add_staff():
    if request.method == 'POST':
        name = request.form['name']
        rfid_uid = request.form['rfid_uid']

        if not name or not rfid_uid:
            flash("Name and RFID UID are required!", 'danger')
            return redirect(url_for('add_staff'))

        existing_staff = Staff.query.filter_by(rfid_uid=rfid_uid).first()
        if existing_staff:
            flash("RFID UID already registered!", 'danger')
            return redirect(url_for('add_staff'))

        new_staff = Staff(name=name, rfid_uid=rfid_uid)
        db.session.add(new_staff)
        db.session.commit()
        flash(f"Staff member {name} added successfully!", 'success')
        return redirect(url_for('staff_management'))
    return render_template('add_staff.html')

# Route to edit an existing staff member
@app.route('/edit_staff/<int:staff_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_staff(staff_id):
    staff_member = db.session.get(Staff, staff_id)
    if not staff_member:
        flash('Staff member not found.', 'danger')
        return redirect(url_for('staff_management'))

    all_shifts = Shift.query.all()
    leave_types = LeaveType.query.all()

    if request.method == 'POST':
        new_name = request.form.get('name', '').strip()
        new_rfid_uid = request.form.get('rfid_uid', '').strip()
        new_is_active = 'is_active' in request.form
        
        new_current_status = request.form.get('staff_status')
        new_leave_type_id = request.form.get('leave_type')

        birthdate_str = request.form.get('birthdate')
        selected_shift_id = request.form.get('shift_id')
        
        new_comment = request.form.get('comment')

        birthdate = None
        if birthdate_str:
            try:
                birthdate = datetime.strptime(birthdate_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format for birthdate.', 'danger')
                return redirect(url_for('edit_staff', staff_id=staff_id))

        # --- Update Staff Details ---
        staff_member.name = new_name

        # --- NEW: Improved RFID UID handling ---
        if new_rfid_uid != staff_member.rfid_uid:
            if not new_rfid_uid:
                staff_member.rfid_uid = None
            else:
                existing_staff_with_uid = Staff.query.filter(
                    Staff.rfid_uid == new_rfid_uid,
                    Staff.is_active == True,
                    Staff.id != staff_id
                ).first()

                if existing_staff_with_uid:
                    flash(f'RFID UID "{new_rfid_uid}" is already assigned to active staff: {existing_staff_with_uid.name}. Please choose another UID or deactivate that staff member first.', 'danger')
                    return redirect(url_for('edit_staff', staff_id=staff_id))
                else:
                    staff_member.rfid_uid = new_rfid_uid

        staff_member.is_active = new_is_active
        if not new_is_active and staff_member.rfid_uid is not None:
            staff_member.rfid_uid = None
            flash(f'{staff_member.name} has been deactivated and their RFID tag has been cleared for reuse.', 'info')
        elif not new_is_active:
            flash(f'{staff_member.name} has been deactivated.', 'info')
        elif new_is_active and staff_member.rfid_uid is None and new_rfid_uid == '':
            flash(f'{staff_member.name} is active but has no RFID tag assigned. They will not be able to scan.', 'warning')

        if staff_member.current_status != new_current_status:
            action_type = "MANUAL_STATUS_CHANGE"
            if new_current_status == 'OUT':
                action_type = "MANUAL_OUT"
            elif new_current_status == 'IN':
                action_type = "MANUAL_IN"
            elif new_current_status == 'ON_LEAVE':
                action_type = "MANUAL_LEAVE"
            elif new_current_status == 'ON_LUNCH':
                action_type = "MANUAL_LUNCH"
            
            manual_log = Attendance(
                staff_id=staff_id,
                rfid_uid=staff_member.rfid_uid,
                timestamp=datetime.now(),
                date=date.today(),
                action_type=action_type,
                notes=new_comment
            )
            db.session.add(manual_log)
        
        staff_member.current_status = new_current_status
        if new_current_status == 'ON_LEAVE' and new_leave_type_id:
            staff_member.leave_type_id = int(new_leave_type_id)
        else:
            staff_member.leave_type_id = None

        staff_member.birthdate = birthdate
        staff_member.shift_id = int(selected_shift_id) if selected_shift_id else None

        try:
            db.session.commit()
            flash(f'Staff member {staff_member.name} updated successfully!', 'success')
            return redirect(url_for('staff_management'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating staff member: {str(e)}', 'danger')
            app.logger.error(f"Error updating staff {staff_id}: {e}")
            return redirect(url_for('edit_staff', staff_id=staff_id))

    return render_template('edit_staff.html', staff=staff_member, all_shifts=all_shifts, leave_types=leave_types)

# Route to edit an existing shift (PROTECTED)
@app.route('/edit_shift/<int:shift_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_shift(shift_id):
    shift = db.session.get(Shift, shift_id) # Get the shift by ID
    if not shift:
        flash('Shift not found.', 'danger')
        return redirect(url_for('manage_shifts')) # Redirect back to shift list

    if request.method == 'POST':
        # Retrieve form data
        name = request.form.get('name', '').strip()
        start_time_str = request.form.get('start_time')
        end_time_str = request.form.get('end_time')
        grace_period_minutes = request.form.get('grace_period_minutes', type=int)
        break_duration_minutes = request.form.get('break_duration_minutes', type=int)
        days_of_week_list = request.form.getlist('days_of_week')

        # Basic validation
        if not name or not start_time_str or not end_time_str:
            flash('Name, Start Time, and End Time are required.', 'danger')
            return redirect(url_for('edit_shift', shift_id=shift_id))

        try:
            start_time = datetime.strptime(start_time_str, '%H:%M').time()
            end_time = datetime.strptime(end_time_str, '%H:%M').time()
        except ValueError:
            flash('Invalid time format. Please use HH:MM.', 'danger')
            return redirect(url_for('edit_shift', shift_id=shift_id))

        if start_time >= end_time:
            flash('End time must be after start time.', 'danger')
            return redirect(url_for('edit_shift', shift_id=shift_id))

        # Check for non-negative values for grace period and break duration
        if grace_period_minutes is None or grace_period_minutes < 0:
            grace_period_minutes = 0 # Default to 0 if not provided or invalid
        if break_duration_minutes is None or break_duration_minutes < 0:
            break_duration_minutes = 0 # Default to 0 if not provided or invalid

        # Update shift object
        shift.name = name
        shift.start_time = start_time
        shift.end_time = end_time
        shift.grace_period_minutes = grace_period_minutes
        shift.break_duration_minutes = break_duration_minutes
        shift.days_of_week = days_list_to_string(days_of_week_list) 

        try:
            db.session.commit()
            flash(f'Shift "{shift.name}" updated successfully!', 'success')
            return redirect(url_for('manage_shifts'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating shift: {str(e)}', 'danger')
            app.logger.error(f"Error updating shift {shift_id}: {e}")
            return redirect(url_for('edit_shift', shift_id=shift_id))

    # GET request: Render the edit form with current shift data
    return render_template('edit_shift.html', shift=shift)

# Route to delete an existing shift (PROTECTED)
@app.route('/delete_shift/<int:shift_id>', methods=['POST'])
@login_required
@admin_required
def delete_shift(shift_id):
    try:
        shift_to_delete = Shift.query.get_or_404(shift_id)

        # Check if any staff members are assigned to this shift
        # This is the corrected line to check for assigned staff
        staff_count = Staff.query.filter_by(shift_id=shift_id).count()
        
        if staff_count > 0:
            flash(f"Error: Cannot delete shift '{shift_to_delete.name}' because it is assigned to {staff_count} staff member(s). Please re-assign them first.", 'danger')
            return redirect(url_for('manage_shifts'))
        
        db.session.delete(shift_to_delete)
        db.session.commit()
        flash(f"Shift '{shift_to_delete.name}' deleted successfully!", 'success')
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error deleting shift {shift_id}: {e}")
        flash(f"An error occurred while deleting the shift.", 'danger')
    
    return redirect(url_for('manage_shifts'))

# Route to delete a staff member (NOW PROTECTED)
@app.route('/delete_staff/<int:staff_id>', methods=['POST'])
@login_required # Only accessible if logged in
@admin_required
def delete_staff(staff_id):
    staff_member = Staff.query.get_or_404(staff_id)
    # Optional: Delete associated attendance records first if you want cascading delete
    # Attendance.query.filter_by(staff_id=staff_id).delete()
    db.session.delete(staff_member)
    db.session.commit()
    flash(f"Staff member {staff_member.name} deleted successfully!", 'success')
    return redirect(url_for('staff_management'))

# Route to toggle staff active status (NEW)
@app.route('/toggle_staff_status/<int:staff_id>', methods=['POST'])
@login_required # Only accessible if logged in
@admin_required
def toggle_staff_status(staff_id):
    staff_member = Staff.query.get_or_404(staff_id)
    staff_member.is_active = not staff_member.is_active # Toggle the boolean status
    db.session.commit()

    status_message = "activated" if staff_member.is_active else "deactivated"
    flash(f"Staff member {staff_member.name} has been {status_message}.", 'success')
    return redirect(url_for('staff_management'))


# --- Attendance Reports Route ---
@app.route('/attendance_reports', methods=['GET', 'POST'])
@login_required
@admin_required
def attendance_reports():
    # --- Data for the High-Level Summary ---
    today = date.today()
    total_staff = Staff.query.count()

    present_staff_count = Staff.query.filter(
        or_(Staff.current_status == 'IN', Staff.current_status == 'Lunch_in')
    ).count()
    
    absent_staff_count = Staff.query.filter(
        or_(Staff.current_status == 'OUT', Staff.current_status == 'Not Scanned')
    ).count()
    
    # --- NEW: Count staff on lunch ---
    on_lunch_count = Staff.query.filter(Staff.current_status == 'Lunch_in').count()
    # ---------------------------------

    on_leave_count = Staff.query.join(Leave).filter(
        Leave.start_date <= today, Leave.end_date >= today
    ).count()

    # --- Pagination Setup ---
    page = request.args.get('page', 1, type=int)
    per_page = 20 # Display 10 staff members per page

    # --- Data for the Detailed Breakdown with Pagination ---
    staff_pagination = Staff.query.options(joinedload(Staff.attendance_records)).paginate(
        page=page, 
        per_page=per_page, 
        error_out=False
    )

    reports = []
    for staff in staff_pagination.items:
        today_record = Attendance.query.filter(
            Attendance.staff_id == staff.id,
            func.date(Attendance.timestamp) == today
        ).order_by(Attendance.timestamp.desc()).first()

        if today_record and today_record.first_in and today_record.last_out:
            duration = today_record.last_out - today_record.first_in
        else:
            duration = None

        # New code to check for birthday
        is_birthday = False
        if staff.birthdate:
            if staff.birthdate.month == today.month and staff.birthdate.day == today.day:
                is_birthday = True
        
        
        report_data = {
            'id': today_record.id if today_record else None,
            'staff': staff,
            'status': staff.current_status,
            'check_in_time': today_record.first_in.time() if today_record and today_record.first_in else None,
            'check_out_time': today_record.last_out.time() if today_record and today_record.last_out else None,
            'total_hours': str(duration).split('.')[0] if duration else 'N/A',
            'comment': today_record.comment if today_record else None,
            'is_birthday': is_birthday
        }

        reports.append(report_data)
        
    current_year = date.today().year
    public_holidays = get_public_holidays(current_year)

    db.session.remove()
    return render_template(
        'attendance_reports.html',
        reports=reports,
        total_staff=total_staff,
        present_staff_count=present_staff_count,
        absent_staff_count=absent_staff_count,
        on_leave_count=on_leave_count,
        on_lunch_count=on_lunch_count, # <-- NEW: Pass the lunch count to the template
        public_holidays=public_holidays,
        time=time,
        pagination=staff_pagination
    )

# --- Export Detailed Attendance Report Route ---
@app.route('/export_report', methods=['GET'])
@login_required
@admin_required
def export_report():
    # Get filter criteria from URL query parameters (should match attendance_reports route)
    filter_date = request.args.get('filter_date')
    filter_type = request.args.get('filter_type', 'all')
    staff_filter_id = request.args.get('staff_filter', 'all')

    # Start building the query
    query = Attendance.query

    # Apply Staff Filter
    if staff_filter_id and staff_filter_id != 'all':
        try:
            staff_id_int = int(staff_filter_id)
            staff_obj = Staff.query.get(staff_id_int)
            if staff_obj:
                query = query.filter(Attendance.staff_id == staff_id_int)
            else:
                flash("Selected staff member not found. Exporting all staff.", 'warning')
                staff_filter_id = 'all'
        except ValueError:
            flash("Invalid staff selection. Exporting all staff.", 'warning')
            staff_filter_id = 'all'

    # Apply Date Filters
    if filter_date and filter_type != 'all':
        try:
            selected_date_dt = datetime.strptime(filter_date, '%Y-%m-%d')
            
            if filter_type == 'day':
                query = query.filter(Attendance.date == selected_date_dt.date())
            elif filter_type == 'month':
                query = query.filter(
                    db.extract('year', Attendance.timestamp) == selected_date_dt.year,
                    db.extract('month', Attendance.timestamp) == selected_date_dt.month
                )
            elif filter_type == 'week':
                start_of_week = selected_date_dt - timedelta(days=selected_date_dt.weekday())
                end_of_week = start_of_week + timedelta(days=6)
                query = query.filter(
                    Attendance.date >= start_of_week.date(),
                    Attendance.date <= end_of_week.date()
                )
        except ValueError:
            flash("Invalid date format for export.", 'danger')
            return redirect(url_for('attendance_reports'))

    # Fetch records for export
    records = query.order_by(Attendance.timestamp).all()

    if not records:
        flash('No data to export for the selected criteria.', 'info')
        return redirect(url_for('attendance_reports'))

    # Prepare data for DataFrame
    export_data = []
    for record in records:
        export_data.append({
            'Staff Name': record.related_staff.name if record.staff else 'UNKNOWN', # Use staff relationship
            'RFID UID': record.related_staff.rfid_uid if record.staff else 'N/A',
            'Timestamp': record.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'Date': record.date.strftime('%Y-%m-%d'),
            'Action': record.action
        })

    df = pd.DataFrame(export_data)

    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='xlsxwriter')
    df.to_excel(writer, index=False, sheet_name='Attendance Log')
    writer.close()

    output.seek(0)
    
    # Construct filename based on filters
    filename_parts = ['attendance_log']
    if staff_filter_id != 'all' and staff_obj:
        filename_parts.append(staff_obj.name.replace(" ", "_"))
    if filter_date and filter_type != 'all':
        filename_parts.append(f"{filter_type}_{filter_date}")
    
    filename = "_".join(filename_parts) + ".xlsx"

    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name=filename)

# Example: Add a new route for managing shifts
@app.route('/shifts', methods=['GET', 'POST'])
@login_required # Assuming only logged-in users can manage shifts
@admin_required
def manage_shifts():
    if request.method == 'POST':
        name = request.form.get('name')
        start_time_str = request.form.get('start_time')
        end_time_str = request.form.get('end_time')
        grace_period_minutes = request.form.get('grace_period_minutes', type=int, default=5)
        break_duration_minutes = request.form.get('break_duration_minutes', type=int, default=60)

        days_of_week_list = request.form.getlist('days_of_week')

        # Basic validation: Check for required fields and days
        if not name or not start_time_str or not end_time_str or not days_of_week_list:
            flash('All required fields (Shift Name, Start Time, End Time, and at least one Day of Week) must be filled.', 'danger')
            shifts = Shift.query.all() # Re-fetch shifts to display the page correctly
            return render_template('manage_shifts.html', shifts=shifts)

        try:
            start_time = datetime.strptime(start_time_str, '%H:%M').time()
            end_time = datetime.strptime(end_time_str, '%H:%M').time()
            days_of_week_db_string = days_list_to_string(days_of_week_list)

            new_shift = Shift(
                name=name,
                start_time=start_time,
                end_time=end_time,
                grace_period_minutes=grace_period_minutes,
                break_duration_minutes=break_duration_minutes,
                days_of_week=days_of_week_db_string
            )
            db.session.add(new_shift)
            db.session.commit()
            flash(f"Shift '{name}' added successfully!", 'success')
        except ValueError:
            flash("Invalid time format. Use HH:MM.", 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f"Error adding shift: {e}", 'danger')

        return redirect(url_for('manage_shifts'))

    shifts = Shift.query.all()
    return render_template('manage_shifts.html', shifts=shifts)

# To add a comment to a late attendance record
@app.route('/add_comment', methods=['POST'])
# @login_required
def add_comment():
    print("Received form data:", request.form)
    attendance_id = request.form.get('attendance_id')
    comment_text = request.form.get('comment')
    
    if not attendance_id or not comment_text:
        flash('Attendance ID or comment missing.', 'danger')
        return redirect(url_for('attendance_reports'))
    
    try:
        attendance_record = Attendance.query.get(attendance_id)
        if attendance_record:
            attendance_record.comment = comment_text
            db.session.commit()
            flash('Comment added successfully!', 'success')
        else:
            flash('Attendance record not found.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {e}', 'danger')
    finally:
        # --- Add this line to remove the session ---
        db.session.remove()

    return redirect(url_for('attendance_reports'))

# --- Database Initialization and App Run ---
with app.app_context():
    db.create_all() # Create tables if they don't exist

if __name__ == '__main__':
    # You can set debug=True during development for auto-reloading and debug info
    app.run(debug=True, host='0.0.0.0') # host='0.0.0.0' makes it accessible from other devices on your network
