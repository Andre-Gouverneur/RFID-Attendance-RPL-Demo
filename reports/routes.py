from flask import render_template, redirect, url_for, flash, request, send_file
from flask_login import login_required
from datetime import datetime, date, timedelta
from collections import defaultdict
import pandas as pd
import io
import holidays as pyholidays
from sqlalchemy.orm import joinedload
from sqlalchemy import exc

from rfid_attendance_backend import db
from rfid_attendance_backend.helpers import admin_required
from ..models import Staff, Attendance, Leave
from ..helpers import process_daily_logs # Ensure this is imported

# Import the blueprint from the parent package
from . import reports_bp

@reports_bp.route('/attendance_reports', methods=['GET', 'POST'])
@login_required
@admin_required
def attendance_reports():
    all_staff = Staff.query.order_by(Staff.name).all()
    # records = [] # This will be set by pagination later

    # Default filters
    selected_date_str = None
    selected_filter_type = 'all' # 'all', 'day', 'month', 'week'
    selected_staff_id = 'all' # 'all' or staff.id

    # Determine filter values from GET or POST request
    if request.method == 'POST':
        selected_date_str = request.form.get('filter_date')
        selected_filter_type = request.form.get('filter_type', 'all')
        selected_staff_id = request.form.get('staff_filter', 'all')
    elif request.method == 'GET':
        selected_date_str = request.args.get('filter_date')
        selected_filter_type = request.args.get('filter_type', 'all')
        selected_staff_id = request.args.get('staff_filter', 'all')
        # --- NEW: Get pagination page from GET request ---
        page = request.args.get('page', 1, type=int)
        # --- END NEW ---

    # Start building the query
    query = Attendance.query.options(joinedload(Attendance.related_staff))

    # Apply Staff Filter
    if selected_staff_id and selected_staff_id != 'all':
        try:
            staff_id_int = int(selected_staff_id)
            staff_obj = db.session.get(Staff, staff_id_int)
            if staff_obj:
                query = query.filter(Attendance.staff_id == staff_id_int)
            else:
                flash("Selected staff member not found. Displaying all staff.", 'warning')
                selected_staff_id = 'all'
        except ValueError:
            flash("Invalid staff selection. Displaying all staff.", 'warning')
            selected_staff_id = 'all'

    # Apply Date Filters and determine the range for holidays
    report_start_date = None
    report_end_date = None

    if selected_date_str and selected_filter_type != 'all':
        try:
            selected_date_dt = datetime.strptime(selected_date_str, '%Y-%m-%d')

            if selected_filter_type == 'day':
                query = query.filter(Attendance.date == selected_date_dt.date())
                report_start_date = selected_date_dt.date()
                report_end_date = selected_date_dt.date()
            elif selected_filter_type == 'month':
                query = query.filter(
                    db.extract('year', Attendance.timestamp) == selected_date_dt.year,
                    db.extract('month', Attendance.timestamp) == selected_date_dt.month
                )
                report_start_date = selected_date_dt.date().replace(day=1)
                report_end_date = (selected_date_dt.date().replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1) # Last day of month
            elif selected_filter_type == 'week':
                start_of_week = selected_date_dt - timedelta(days=selected_date_dt.weekday())
                end_of_week = start_of_week + timedelta(days=6)
                query = query.filter(
                    Attendance.date >= start_of_week.date(),
                    Attendance.date <= end_of_week.date()
                )
                report_start_date = start_of_week.date()
                report_end_date = end_of_week.date()
        except ValueError:
            flash("Invalid date format. Please use YYYY-MM-DD.", 'danger')
            selected_date_str = None
            selected_filter_type = 'all'

    # If no specific date filter, default to a sensible range (e.g., last 30 days or current year)
    if not report_start_date or not report_end_date:
        # Default to last 30 days if no explicit date filter is applied,
        # but ensure the query is still bound by the actual data range,
        # otherwise all historical data will be paginated if no filter is active.
        # This part requires careful thought if 'all' filter type should mean *all* data or a recent window.
        # For now, let's keep it as-is, meaning the date filtering only applies if filter_type is not 'all'.
        pass # No change to query if filter_type is 'all' or selected_date_str is None

    # Order the final results (this should be done before paginate)
    query = query.order_by(Attendance.date.desc())

    # --- START PAGINATION CODE ---
    per_page = 20 # Adjust the number of items per page as needed
    # Ensure 'page' is available for pagination, defaulted to 1 if not in GET args
    # It's already defined above for GET, but let's ensure it's always set.
    if 'page' not in locals(): # Check if 'page' variable was set in the GET block
        page = 1 # Default if POST request or initial load without page param
    
    attendance_logs_pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    records = attendance_logs_pagination.items # Get the items for the current page
    # --- END PAGINATION CODE ---

    # Fetch SA Public Holidays for the relevant period
    # Create a set of holiday dates for quick lookup
    holidays_in_period = {}
    # Ensure report_start_date and report_end_date are set before trying to iterate years
    # If no date filters, you might want to default these for the holiday lookup,
    # e.g., to cover the range of records fetched or a fixed large range.
    if not report_start_date: # Default for holiday lookup if not set by filters
        # Using a fixed range for holidays if no date filter is applied,
        # or you can set it to the min/max dates of 'records' if records is large and fetched all
        report_start_date = date.today() - timedelta(days=365) # Example: last year for holidays
    if not report_end_date:
        report_end_date = date.today() + timedelta(days=365) # Example: next year for holidays


    for year in range(report_start_date.year, report_end_date.year + 1):
        sa_holidays = pyholidays.SouthAfrica(years=year)
        for hol_date, hol_name in sa_holidays.items():
            # Only include holidays within our reporting period
            # Make sure hol_date is always a date object for comparison
            if isinstance(hol_date, datetime):
                hol_date = hol_date.date()
            if report_start_date <= hol_date <= report_end_date:
                holidays_in_period[hol_date] = hol_name


    return render_template(
        'attendance_reports.html',
        records=records, # Now contains only records for the current page
        attendance_logs_pagination=attendance_logs_pagination, # NEW: Pass the pagination object
        staff_list=all_staff,
        selected_date=selected_date_str, # Pass back the string for input field
        selected_filter_type=selected_filter_type,
        selected_staff_id=selected_staff_id,
        now=datetime.now(), # Pass 'now' for default date input value if needed
        holidays_in_period=holidays_in_period # Pass the dictionary of holidays
    )

@reports_bp.route('/summary_reports', methods=['GET', 'POST'])
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

@reports_bp.route('/export_report', methods=['GET'])
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
            'Action': record.action_type
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

@reports_bp.route('/export_summary_reports', methods=['GET'])
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