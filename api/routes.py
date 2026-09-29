from flask import request, jsonify
from datetime import datetime, date, timedelta
from sqlalchemy.exc import SQLAlchemyError
from flask_login import login_required
from rfid_attendance_backend import db
from rfid_attendance_backend.helpers import admin_required, get_public_holidays
from rfid_attendance_backend.models import Staff, Attendance, Leave
from ..helpers import get_relevant_shift

# Import the blueprint
from . import api_bp

# Global variable to store the last scanned RFID UID specifically for the 'Add Staff' feature
last_scanned_uid_for_add_staff = None

@api_bp.route('/log_attendance', methods=['POST'])
def log_attendance():
    data = request.get_json()
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

         # NEW DEBUG CODE
        logging.info(f"DEBUG: Processing scan for Staff ID {staff.id}, is_active: {staff.is_active}, shift_id: {staff.shift_id}, current_status: {staff.current_status}")


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

                    relevant_shift = get_relevant_shift(staff, now)

                    if not relevant_shift:
                        # Scan is still outside the extended window, so it's a true DENIED_NO_SCHEDULE
                        message_for_response = f"Scan Denied for {staff.name}: No active shift found for today ({current_weekday_abbr}) at this time."
                        action_status_for_response = "DENIED_NO_SCHEDULE"
                        log_action = "DENIED"
                        http_status_code = 403
                        staff.current_status = 'OFF_SCHEDULE'
                        db.session.add(staff)
                    else:
                        # --- NEW IN/LUNCH/OUT LOGIC BLOCK ---
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

                        elif staff.current_status == 'IN' and now.time() >= relevant_shift.end_time:
                            # --- REFINED 'OUT' LOGIC for overnight shifts ---
                            log_action = 'OUT'
                            # Get the latest 'IN' record for the staff member that is not yet 'OUT'
                            latest_open_in_record = Attendance.query.filter(
                                Attendance.staff_id == staff.id,
                                Attendance.action_type == 'IN',
                                Attendance.last_out.is_(None)
                            ).order_by(Attendance.timestamp.desc()).first()

                            if latest_open_in_record:
                                latest_open_in_record.last_out = now
                                duration = now - latest_open_in_record.timestamp
                                latest_open_in_record.total_duration = int(duration.total_seconds())

                                out_status = 'On Time Out'
                                shift_end_compare_dt = datetime.combine(latest_open_in_record.date, relevant_shift.end_time)
                                if relevant_shift.end_time < relevant_shift.start_time:
                                    shift_end_compare_dt += timedelta(days=1)
                                if now < shift_end_compare_dt:
                                    out_status = 'Early Out'
                                elif now > shift_end_compare_dt + timedelta(minutes=relevant_shift.grace_period_minutes): # Adjust for grace period
                                    out_status = 'Late Out'
                                
                                latest_open_in_record.status = f'Present ({out_status})'
                                db.session.add(latest_open_in_record)

                                staff.current_status = 'OUT'
                                staff.last_check_in_out = now
                                db.session.add(staff)
                                message_for_response = f"{staff.name} checked OUT successfully ({out_status})."
                                action_status_for_response = "CHECKED_OUT"
                                http_status_code = 200
                            else:
                                # This is a fallback in case of a missing 'IN' record
                                message_for_response = f"Scan Denied for {staff.name}: No active 'IN' record found to check out from."
                                action_status_for_response = "DENIED_NO_IN_RECORD"
                                http_status_code = 403
                        
                        # ... (existing 'LUNCH' logic and 'else' block)

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
    
 # New API endpoint for the 'Add Staff' page
@api_bp.route('/get_scanned_uid_for_add_staff', methods=['GET'])
@login_required
@admin_required
def get_scanned_uid_for_add_staff():
    global last_scanned_uid_for_add_staff
    if last_scanned_uid_for_add_staff:
        uid = last_scanned_uid_for_add_staff
        # We clear it after sending it once to ensure we get the *next* scan if user re-scans
        last_scanned_uid_for_add_staff = None
        return jsonify({"uid": uid})
    return jsonify({"uid": None}) # Return None if no UID has been scanned yet

# New API endpoint to explicitly clear the stored UID
@api_bp.route('/clear_scanned_uid_for_add_staff', methods=['POST'])
@login_required
@admin_required
def clear_scanned_uid_for_add_staff():
    global last_scanned_uid_for_add_staff
    last_scanned_uid_for_add_staff = None
    return jsonify({"message": "UID capture state cleared"})

# Staff List API Endpoint (for ESP32 to fetch known users)
@api_bp.route('/staff', methods=['GET'])
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