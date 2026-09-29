from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime, date, timedelta
from werkzeug.utils import secure_filename
import os

from rfid_attendance_backend import db
from rfid_attendance_backend.helpers import admin_required, get_public_holidays
from rfid_attendance_backend.models import Staff, Shift, Leave, Attendance, User
from ..helpers import days_list_to_string, days_string_to_list

# Import the blueprint from the parent package
from . import staff_management_bp

# Move your staff and shift management routes here
@staff_management_bp.route('/staff_management', methods=['GET', 'POST'])
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


@staff_management_bp.route('/edit_staff/<int:staff_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_staff(staff_id):
    staff_member = db.session.get(Staff, staff_id) # Correctly using db.session.get()
    leave_types = LeaveType.query.all()
    if not staff_member:
        flash('Staff member not found.', 'danger')
        return redirect(url_for('staff_management'))

    # Fetch all available shifts for the dropdown
    all_shifts = Shift.query.all() # <--- NEW: Fetch all shifts here

    if request.method == 'POST':
        # Retrieve form data
        new_name = request.form.get('name', '').strip() # Use .strip() for safety
        new_rfid_uid = request.form.get('rfid_uid', '').strip() # Use .get() and .strip() for safety
        new_is_active = True if request.form.get('is_active') == 'on' else False # Correctly gets checkbox value
        new_current_status = request.form.get('current_status')
        new_leave_type = request.form.get('current_leave_type') # Get the selected leave type

        # --- NEW: Get selected shift ID ---
        selected_shift_id = request.form.get('shift_id')
        if selected_shift_id:
            # Convert to int, ensure it's None if empty string is passed (e.g., "No Shift Assigned")
            try:
                staff_member.shift_id = int(selected_shift_id)
            except ValueError: # Handles case where selected_shift_id might not be an integer (e.g., '')
                staff_member.shift_id = None
        else:
            staff_member.shift_id = None # Explicitly set to None if nothing is selected or if "No Shift Assigned" is chosen

        # --- NEW LOGIC FOR RFID UID AND IS_ACTIVE STATUS ---
        # 1. Update Staff Name
        staff_member.name = new_name

        # 2. Handle RFID UID assignment/clearing
        if not new_rfid_uid: # If the RFID UID field was submitted empty
            staff_member.rfid_uid = None # Clear the RFID UID
        else:
            # Check if the new RFID UID is already assigned to another ACTIVE staff member
            # (Exclude the current staff member being edited)
            existing_staff_with_uid = Staff.query.filter(
                Staff.rfid_uid == new_rfid_uid,
                Staff.is_active == True,  # Only check active staff
                Staff.id != staff_id      # Exclude the staff member currently being edited
            ).first()

            if existing_staff_with_uid:
                flash(f'RFID UID "{new_rfid_uid}" is already assigned to active staff: {existing_staff_with_uid.name}. Please choose another UID or deactivate that staff member first.', 'danger')
                return redirect(url_for('edit_staff', staff_id=staff_id))
            else:
                staff_member.rfid_uid = new_rfid_uid # Assign the new UID if valid
        # --- END NEW LOGIC FOR RFID UID ---


        # --- Handle is_active status and associated RFID UID clearing ---
        staff_member.is_active = new_is_active
        if not new_is_active and staff_member.rfid_uid is not None:
            # If staff is being deactivated AND they currently have an RFID UID, clear it
            staff_member.rfid_uid = None
            flash(f'{staff_member.name} has been deactivated and their RFID tag has been cleared for reuse.', 'info')
        elif not new_is_active:
            flash(f'{staff_member.name} has been deactivated.', 'info')
        elif new_is_active and staff_member.rfid_uid is None and new_rfid_uid == '':
            # Warn if active but no tag assigned
            flash(f'{staff_member.name} is active but has no RFID tag assigned. They will not be able to scan.', 'warning')
        # --- END NEW LOGIC FOR IS_ACTIVE ---


        # --- Your existing logic for status and leave type changes ---
        # This part remains largely the same, but uses new_current_status
        if new_current_status != staff_member.current_status:
            staff_member.current_status = new_current_status
            if new_current_status == 'IN':
                staff_member.last_check_in_out = datetime.now()
                staff_member.leave_type = None # Clear leave type if returning from leave
            elif new_current_status == 'OUT':
                staff_member.last_check_in_out = None
                staff_member.leave_type = None # Clear leave type if returning from leave
            elif new_current_status == 'ON_LEAVE':
                staff_member.last_check_in_out = None
                staff_member.leave_type = new_leave_type # Set specific leave type
        elif new_current_status == 'ON_LEAVE': # If status remains ON_LEAVE, update type
            staff_member.leave_type = new_leave_type
        else: # If status is IN or OUT and not changing, ensure leave_type is None
            staff_member.leave_type = None


        try:
            db.session.commit()
            flash(f'Staff member {staff_member.name} updated successfully!', 'success')
            return redirect(url_for('staff_management'))
        except Exception as e:
            db.session.rollback() # Rollback on error
            flash(f'Error updating staff member: {str(e)}', 'danger')
            app.logger.error(f"Error updating staff {staff_id}: {e}") # Log the error for debugging
            return redirect(url_for('edit_staff', staff_id=staff_id))
            
    # GET request: Render the edit form with current staff data
    return render_template('edit_staff.html', staff_member=staff_member, all_shifts=all_shifts, leave_types=leave_types)

@staff_management_bp.route('/shifts', methods=['GET', 'POST'])
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

@staff_management_bp.route('/edit_shift/<int:shift_id>', methods=['GET', 'POST'])
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
        days_of_week_list = request.form.getlist('days_of_week')
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

# You will also move your delete routes here
@staff_management_bp.route('/delete_staff/<int:staff_id>', methods=['POST'])
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

@staff_management_bp.route('/delete_shift/<int:shift_id>', methods=['POST'])
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

@staff_management_bp.route('/toggle_staff_status/<int:staff_id>', methods=['POST'])
@login_required # Only accessible if logged in
@admin_required
def toggle_staff_status(staff_id):
    staff_member = Staff.query.get_or_404(staff_id)
    staff_member.is_active = not staff_member.is_active # Toggle the boolean status
    db.session.commit()

    status_message = "activated" if staff_member.is_active else "deactivated"
    flash(f"Staff member {staff_member.name} has been {status_message}.", 'success')
    return redirect(url_for('staff_management'))

# Route to add a new staff member
@staff_management_bp.route('/add_staff', methods=['GET', 'POST'])
@login_required
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

# Route to manage leave types (NEW)
@staff_management_bp.route('/manage_leave_types', methods=['GET', 'POST'])
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

# Route to delete a leave type
@staff_management_bp.route('/delete_leave_type/<int:leave_type_id>', methods=['POST'])
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