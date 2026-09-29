# tasks.py

from app import db # This gives you access to the database object
from app import Staff # This gives you access to the Staff model
from datetime import datetime, time, date
from sqlalchemy.exc import InvalidRequestError, IntegrityError, SQLAlchemyError
# Import the Celery app instance and db from your main Flask application
from app import celery_app, db
# Correctly import your models
from celery.schedules import crontab

celery_app.conf.beat_schedule = {
    'run-auto-checkout-at-5pm-daily': {
        'task': 'tasks.automatic_checkout',
        'schedule': crontab(hour=17, minute=0), # Schedules the task for 5:00 PM every day
    },
}

# The '@celery_app.task' decorator registers this function as a Celery task
@celery_app.task
def automatic_checkout():
    """
    Automatically checks out staff members who are marked as 'IN' at the end of the day.
    This function runs as a background task via Celery.
    """
    # The 'with app.app_context():' is no longer needed; Celery handles the context.
    print(f"[{datetime.now()}] Running automatic checkout job...")
    try:
        # Define the auto-checkout time for today (e.g., 5 PM)
        auto_checkout_time = datetime.combine(date.today(), time(17, 0, 0))

        # Find all staff members who are currently marked as 'IN'
        stuck_in_staff = Staff.query.filter_by(current_status='IN').all()

        if not stuck_in_staff:
            print("No staff found marked 'IN' for automatic checkout.")
            return

        for staff in stuck_in_staff:
            # Ensure they checked in today or are genuinely stuck from a previous day
            if staff.last_check_in_out and staff.last_check_in_out.date() == date.today():
                # Create an automatic OUT attendance record
                auto_out_log = Attendance(
                    staff_id=staff.id,
                    rfid_uid=staff.rfid_uid,
                    timestamp=auto_checkout_time,
                    date=date.today(),
                    action_type='AUTO_OUT'
                )
                db.session.add(auto_out_log)

                # Update the staff member's status
                staff.current_status = 'OUT'
                staff.last_check_in_out = auto_checkout_time
                print(f"Auto-checked out {staff.name} (ID: {staff.id}) at {auto_checkout_time}")

            elif staff.current_status == 'IN' and (not staff.last_check_in_out or staff.last_check_in_out.date() < date.today()):
                # This case handles staff who might have been stuck "IN" from a previous day
                print(f"Warning: {staff.name} (ID: {staff.id}) was stuck 'IN' from a previous day. Auto-checking out today.")
                auto_out_log = Attendance(
                    staff_id=staff.id,
                    rfid_uid=staff.rfid_uid,
                    timestamp=auto_checkout_time,
                    date=date.today(),
                    action_type='AUTO_OUT_PREVIOUS_DAY_STUCK'
                )
                db.session.add(auto_out_log)
                staff.current_status = 'OUT'
                staff.last_check_in_out = auto_checkout_time

        db.session.commit()
        print("Automatic checkout job completed successfully.")

    except exc.SQLAlchemyError as e:
        db.session.rollback()
        print(f"Database error during automatic checkout: {e}")
    except Exception as e:
        print(f"An unexpected error occurred during automatic checkout: {e}")

# The 'if __name__ == "__main__":' block is removed because Celery will manage execution.
