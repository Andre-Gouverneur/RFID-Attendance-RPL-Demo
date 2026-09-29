from . import main_bp
from flask import render_template, request, jsonify
from flask_login import login_required
# ... other imports (datetime, timedelta, etc.)

@main_bp.route('/')
def index():
    return render_template('index.html')

@main_bp.route('/dashboard', methods=['GET'])
@login_required
def dashboard():
    today_date = date.today()

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
    
    staff_members = Staff.query.order_by(Staff.name).all()
    total_staff = len(staff_members)
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

    for staff_member in staff_members:
        current_status_counts[staff_member.current_status] += 1
        staff_member.time_in_state = "N/A"
        
        # Calculate total lunch duration for TODAY.
        total_lunch_today = lunch_durations.get(staff_member.id, timedelta(0))
        
        # Add duration for any ongoing lunch breaks
        if staff_member.current_status == 'ON_LUNCH' and staff_member.last_check_in_out:
            current_lunch_duration = datetime.now() - staff_member.last_check_in_out
            total_lunch_today += current_lunch_duration
            
        staff_member.total_lunch_duration = format_duration(total_lunch_today)
        
        # Calculate time in state only for active staff
        if staff_member.is_active and staff_member.last_check_in_out:
            duration: timedelta = datetime.now() - staff_member.last_check_in_out
            staff_member.time_in_state = format_duration(duration)

        if staff_member.is_active:
            if staff_member.current_status == 'IN':
                staff_in.append(staff_member)
            elif staff_member.current_status == 'OUT':
                staff_out.append(staff_member)
            elif staff_member.current_status == 'ON_LUNCH':
                staff_on_lunch.append(staff_member)
            elif staff_member.current_status == 'ON_LEAVE':
                if staff_member.leave_type == 'Absent (Auto)':
                    duration: timedelta = datetime.now() - staff_member.last_check_in_out
                    days, seconds = duration.days, duration.seconds
                    hours = days * 24 + seconds // 3600
                    minutes = (seconds % 3600) // 60
                    staff_member.time_in_state = f"{int(hours)}h {int(minutes)}m (Auto Absent)"
                staff_on_leave.append(staff_member)
        else:
            staff_inactive.append(staff_member)

    page = request.args.get('page', 1, type=int)
    per_page = 10 
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
        staff_members=staff_members
    )