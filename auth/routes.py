from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from rfid_attendance_backend.helpers import admin_required
from . import auth_bp
from rfid_attendance_backend import db, login_manager
from ..models import User, Staff # Note the '..' for importing from the parent directory

@auth_bp.route('/login', methods=['GET', 'POST'])
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

@auth_bp.route('/logout')
@login_required # User must be logged in to log out
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# --- NEW: Admin User Management Route ---
@auth_bp.route('/manage_users', methods=['GET', 'POST'])
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

@login_manager.user_loader
def load_user(user_id):
    # This should now query the database for the user by ID
    return User.query.get(int(user_id)) # Cast user_id to int as it comes as a string
