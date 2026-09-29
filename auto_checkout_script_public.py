import os
import sys
from datetime import datetime, date, time, timedelta
import logging
from sqlalchemy import and_, or_

# IMPORTANT: Adjust this path if your 'app.py' (or the file defining 'app' and 'db')
# is not directly in the parent directory of this script.
# This line adds your project root to the Python path so imports work.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '')))

# Setup basic logging for the cron job itself
# This will create a specific log file for your auto-checkout operations
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    filename='auto_checkout_cron.log', # Dedicated log for cron job
                    filemode='a') # 'a' for append mode

try:
    # Import your Flask app instance (named 'app'), db, and models from where they are defined
    # This assumes your main app file is named 'app.py' and your models are imported/defined there.
    from app import app, db, Staff, Attendance, Leave, get_public_holidays # Adjust if app, db, models are elsewhere
    from sqlalchemy import exc # For database exception handling
except ImportError as e:
    logging.error(f"CRITICAL ERROR: Could not import app components: {e}")
    logging.error("Please ensure 'auto_checkout_script.py' is in the correct directory relative to 'app.py' and 'app.py' defines 'app', 'db', 'Staff', 'Attendance', 'Leave', 'get_public_holidays'.")
    sys.exit(1) # Exit if essential imports fail

# --- Define the automatic_checkout function ---
def automatic_checkout():
    with app.app_context(): # Essential for accessing Flask app context (db, models)
        logging.info(f"[{datetime.now()}] Running automatic checkout job...")
        try:
            # Define the auto-checkout time for today (5 PM)
            auto_checkout_time = datetime.combine(date.today(), time(17, 0, 0)) # 17:00:00 (5 PM)

            # Find all staff members who are currently marked as 'IN' or 'Lunch-In'
            stuck_in_staff = Staff.query.filter(or_(
                 Staff.current_status == 'IN',
                 Staff.current_status == 'Lunch_in'
            )).all()

            if not stuck_in_staff:
                logging.info("No staff found marked 'IN' for automatic checkout.")
                return

            for staff in stuck_in_staff:
                logging.debug(f"Processing staff: {staff.name} (ID: {staff.id}), current_status: {staff.current_status}, last_check_in_out: {staff.last_check_in_out}")

                # --- CRITICAL CHANGE HERE ---
                # Find the MOST RECENT open attendance record for this staff member, regardless of date.
                latest_open_in_record = Attendance.query.filter(
                    Attendance.staff_id == staff.id,
                    Attendance.first_in.isnot(None), # Must have an IN time
                    Attendance.last_out.is_(None)     # Must not have an OUT time yet (meaning it's open)
                ).order_by(Attendance.first_in.desc()).first() # Order by first_in descending to get the latest

                if latest_open_in_record:
                    logging.info(f"Auto-checking out {staff.name} (ID: {staff.id}) who scanned IN at {latest_open_in_record.first_in.strftime('%Y-%m-%d %H:%M')}.")

                    # Update the found record with the auto-checkout time
                    latest_open_in_record.last_out = auto_checkout_time

                    # Calculate duration for this segment
                    if latest_open_in_record.first_in:
                        duration = auto_checkout_time - latest_open_in_record.first_in
                        latest_open_in_record.total_duration = int(duration.total_seconds())
                    else:
                        latest_open_in_record.total_duration = 0
                        logging.warning(f"No first_in for {staff.name}'s attendance record ({latest_open_in_record.id}) when auto-checking out.")

                    latest_open_in_record.status = 'Auto-Checked-Out (Forced)' # Use a clear status

                    # Update the staff's overall status
                    staff.current_status = 'OUT'
                    staff.last_check_in_out = auto_checkout_time
                    db.session.add(staff) # Ensure staff object is tracked for update
                    db.session.add(latest_open_in_record) # Ensure attendance record is tracked for update

                elif staff.current_status == 'IN':
                    # Fallback for truly desynced states: staff.current_status is 'IN' but NO open IN record was found.
                    # This scenario should be rare with the above fix, but is a good safeguard.
                    logging.warning(f"Warning: {staff.name} (ID: {staff.id}) was marked 'IN' but NO open IN record found. Forcing OUT status for today.")

                    # Create a new attendance record for today representing this forced auto-logout
                    auto_out_log = Attendance(
                        staff_id=staff.id,
                        rfid_uid=staff.rfid_uid,
                        timestamp=auto_checkout_time,
                        date=date.today(), # Log this specific forced action for today
                        action_type='AUTO_OUT_FORCED_DESYNC', # More specific action type
                        first_in=None,
                        last_out=auto_checkout_time,
                        total_duration=0,
                        status='Auto-Checked-Out (Forced - No IN record found)'
                    )
                    db.session.add(auto_out_log)

                    staff.current_status = 'OUT'
                    staff.last_check_in_out = auto_checkout_time
                    db.session.add(staff)

            db.session.commit()
            logging.info("Automatic checkout job completed successfully.")

        except exc.SQLAlchemyError as e:
            db.session.rollback()
            logging.error(f"Database error during automatic checkout: {e}", exc_info=True)
        except Exception as e:
            logging.error(f"An unexpected error occurred during automatic checkout: {e}", exc_info=True)


# --- Main execution block for the script ---
if __name__ == '__main__':
    automatic_checkout()