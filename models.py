from rfid_attendance_backend import db
from datetime import datetime, date, time
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.orm import relationship
from flask_wtf import FlaskForm
from wtforms import StringField, TimeField, SubmitField
from wtforms.validators import DataRequired, ValidationError
from wtforms.widgets import ListWidget, CheckboxInput
from wtforms.fields import SelectMultipleField

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

# New form for adding leave types
class LeaveTypeForm(FlaskForm):
    name = StringField('Leave Type Name', validators=[DataRequired()])
    submit = SubmitField('Add Leave Type')

    def validate_name(self, field):
        if LeaveType.query.filter_by(name=field.data).first():
            raise ValidationError('This leave type already exists.')
        
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

class Staff(db.Model):
    __tablename__ = 'staff'
    id = db.Column(db.Integer, primary_key=True)
    rfid_uid = db.Column(db.String(50), unique=True, nullable=False) # CORRECTED LINE
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    current_status = db.Column(db.String(10), default='OUT', nullable=False) # 'IN' or 'OUT'
    last_check_in_out = db.Column(db.DateTime, nullable=True)
    leave_type = db.Column(db.String(50), nullable=True)
    attendance_records = db.relationship('Attendance', backref='related_staff', lazy=True, cascade="all, delete-orphan")
    shift_id = db.Column(db.Integer, db.ForeignKey('shifts.id'), nullable=True) # Staff can be assigned a shift
    assigned_shift = db.relationship('Shift', backref='staff_members') # Relationship to Shift model
    last_rfid_scan_time = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<Staff {self.name} ({self.rfid_uid})>"

class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    # Link to Staff table by ID. Made nullable=True to allow logging of unknown tags.
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    rfid_uid = db.Column(db.String(50), nullable=False)
    # Store as actual DateTime object for better querying and calculations
    timestamp = db.Column(db.DateTime, default=datetime.now, nullable=False)
    # Add a separate date column for easy filtering by date
    date = db.Column(db.Date, default=date.today, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)
    first_in = db.Column(db.DateTime, nullable=True)
    last_out = db.Column(db.DateTime, nullable=True)
    total_duration = db.Column(db.Integer, default=0, nullable=False) # Store in seconds
    status = db.Column(db.String(50), default='Pending', nullable=False) # e.g., 'Present', 'Incomplete (In)', 'Absent'

    def __repr__(self):
        return f"<Attendance {self.staff_id or 'UNKNOWN'} - {self.action_type} at {self.timestamp}>"

class User(UserMixin, db.Model):
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

class LeaveType(db.Model):
    __tablename__ = 'leave_types'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    leave_requests = relationship('Leave', backref='leave_type_obj', lazy=True)
