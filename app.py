"""
================================================================================
  UNITE CBT - Complete Web Application (FIXED VERSION)
  Version: 2.2.0
  
  Features:
    - User Registration (admin approval required)
    - User Login/Logout
    - Admin Panel (manage users)
    - Dashboard with user info
    - Kiosk Builder (creates EXE with custom config)
    - Download Kiosk EXE (valid for 5 minutes)
================================================================================
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import subprocess
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Load environment variables from .env file if it exists
def load_env_file():
    """Load environment variables from .env file automatically."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, val = line.split('=', 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key and val and key not in os.environ:
                            os.environ[key] = val
            print("✅ Loaded environment configuration from .env file!")
        except Exception as e:
            print(f"Error loading .env file: {e}")

load_env_file()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'unite-cbt-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///unite_cbt.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max upload

# Create upload folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'


# =============================================================================
#  DATABASE MODELS
# =============================================================================

class User(UserMixin, db.Model):
    """User model with admin flag and approval status."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        """Convert user to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'full_name': self.full_name,
            'is_admin': self.is_admin,
            'is_approved': self.is_approved,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<User {self.username}>'


class KioskBuild(db.Model):
    """Track kiosk builds for download expiration."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    filepath = db.Column(db.String(500), nullable=False)
    config = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    expires_at = db.Column(db.DateTime, nullable=False)
    downloads = db.Column(db.Integer, default=0)
    
    def is_expired(self):
        """Check if the build has expired."""
        return self.expires_at < datetime.now()
    
    def time_remaining(self):
        """Get remaining time in seconds."""
        if self.is_expired():
            return 0
        return int((self.expires_at - datetime.now()).total_seconds())
    
    def to_dict(self):
        """Convert build to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'filename': self.filename,
            'filepath': self.filepath,
            'config': json.loads(self.config) if self.config else {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'downloads': self.downloads,
            'is_expired': self.is_expired(),
            'time_remaining': self.time_remaining()
        }
    
    def __repr__(self):
        return f'<KioskBuild {self.id}>'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =============================================================================
#  HELPER FUNCTIONS
# =============================================================================

def admin_required(f):
    """Decorator to require admin privileges."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def approved_required(f):
    """Decorator to require user approval."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        if not current_user.is_approved and not current_user.is_admin:
            flash('Your account is pending admin approval.', 'warning')
            return redirect(url_for('logout'))
        return f(*args, **kwargs)
    return decorated_function


def cleanup_old_builds():
    """Remove expired kiosk builds from database and filesystem."""
    try:
        # Find expired builds
        expired = KioskBuild.query.filter(KioskBuild.expires_at < datetime.now()).all()
        
        for build in expired:
            # Delete the file
            try:
                if os.path.exists(build.filepath):
                    os.remove(build.filepath)
                    # Try to remove parent directory if empty
                    parent_dir = os.path.dirname(build.filepath)
                    if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                        shutil.rmtree(parent_dir, ignore_errors=True)
            except Exception as e:
                print(f"Error deleting file for build {build.id}: {e}")
            
            # Delete from database
            db.session.delete(build)
        
        db.session.commit()
    except Exception as e:
        print(f"Error in cleanup_old_builds: {e}")


import io
import zipfile
import urllib.request

def trigger_github_workflow(ip_address, port, unlock_key, unlock_count, build_id):
    """
    Trigger GitHub Actions workflow_dispatch to build the EXE on GitHub Windows VM.
    """
    github_token = os.environ.get('GITHUB_TOKEN')
    repo_owner = os.environ.get('GITHUB_REPO_OWNER')
    repo_name = os.environ.get('GITHUB_REPO_NAME')
    github_branch = os.environ.get('GITHUB_BRANCH', 'main')
    
    if not all([github_token, repo_owner, repo_name]):
        return False, "GitHub configuration missing. Please set GITHUB_TOKEN, GITHUB_REPO_OWNER, and GITHUB_REPO_NAME environment variables."
    
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/actions/workflows/build-kiosk.yml/dispatches"
    
    # Try preferred branch first, then fallback to master if main fails
    branches_to_try = [github_branch]
    if github_branch == 'main':
        branches_to_try.append('master')
    elif github_branch == 'master':
        branches_to_try.append('main')
        
    last_err = ""
    for branch in branches_to_try:
        payload = json.dumps({
            "ref": branch,
            "inputs": {
                "ip_address": str(ip_address),
                "port": str(port),
                "unlock_key": str(unlock_key),
                "unlock_count": str(unlock_count),
                "build_id": str(build_id)
            }
        }).encode('utf-8')
        
        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "UNITE-CBT-App"
        }, method="POST")
        
        try:
            with urllib.request.urlopen(req) as response:
                if response.status in (204, 201, 200):
                    print(f"✅ Dispatched GitHub Actions workflow on branch '{branch}'")
                    return True, f"GitHub Actions workflow dispatched successfully on branch '{branch}'!"
        except Exception as e:
            last_err = str(e)
            print(f"Branch '{branch}' dispatch attempt error: {e}")
            
    return False, f"Failed to dispatch GitHub workflow on branches {branches_to_try}: {last_err}"


def check_and_fetch_github_artifact(build_id, target_filepath):
    """
    Checks GitHub Actions for completed build run matching `build_id`.
    Downloads the artifact zip and extracts KioskLocker.exe to target_filepath.
    Returns: (status: 'ready' | 'building' | 'failed', message)
    """
    github_token = os.environ.get('GITHUB_TOKEN')
    repo_owner = os.environ.get('GITHUB_REPO_OWNER')
    repo_name = os.environ.get('GITHUB_REPO_NAME')
    
    if not all([github_token, repo_owner, repo_name]):
        return 'failed', 'GitHub credentials missing on server.'
    
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "UNITE-CBT-App"
    }
    
    # Fetch recent workflow runs
    runs_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/actions/workflows/build-kiosk.yml/runs?per_page=5"
    req = urllib.request.Request(runs_url, headers=headers)
    
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            runs = data.get('workflow_runs', [])
    except Exception as e:
        return 'building', f"Waiting for GitHub workflow to start... ({e})"
    
    if not runs:
        return 'building', 'Workflow run queued on GitHub Actions...'
        
    latest_run = runs[0]
    status = latest_run.get('status')
    conclusion = latest_run.get('conclusion')
    
    if status in ['queued', 'in_progress', 'requested', 'waiting']:
        return 'building', f'Building on GitHub Actions VM ({status})...'
        
    if status == 'completed' and conclusion != 'success':
        return 'failed', f'GitHub Actions build failed ({conclusion}).'
        
    if status == 'completed' and conclusion == 'success':
        artifacts_url = latest_run.get('artifacts_url')
        if not artifacts_url:
            return 'failed', 'No artifact URL found for completed run.'
            
        req_art = urllib.request.Request(artifacts_url, headers=headers)
        with urllib.request.urlopen(req_art) as resp_art:
            art_data = json.loads(resp_art.read().decode('utf-8'))
            artifacts = art_data.get('artifacts', [])
            
        if not artifacts:
            return 'building', 'Packaging build artifact...'
            
        download_url = artifacts[0].get('archive_download_url')
        
        req_dl = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req_dl) as resp_dl:
            zip_bytes = resp_dl.read()
            
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            exe_names = [f for f in z.namelist() if f.endswith('.exe')]
            if not exe_names:
                return 'failed', 'No .exe file found inside artifact.'
            
            os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
            with open(target_filepath, 'wb') as f_out:
                f_out.write(z.read(exe_names[0]))
                
        return 'ready', 'EXE artifact successfully fetched from GitHub Actions!'
        
    return 'building', 'Waiting for GitHub Actions...'


def create_kiosk_exe(ip_address, port, unlock_key, unlock_count, user_id):
    """
    Build the kiosk EXE with custom configuration.
    Returns the path to the built EXE file and the temp directory.
    """
    source_file = "kiosk_locker-3.py"
    
    # Check if source file exists
    if not os.path.exists(source_file):
        raise FileNotFoundError(f"Source file '{source_file}' not found!")
    
    # Create a temporary directory for this build
    build_id = f"{user_id}_{int(time.time())}"
    temp_dir = os.path.join(tempfile.gettempdir(), f"kiosk_build_{build_id}")
    os.makedirs(temp_dir, exist_ok=True)
    print(f"Created temp directory: {temp_dir}")
    
    try:
        # Copy source file to temp directory
        temp_source = os.path.join(temp_dir, "kiosk_locker-3.py")
        shutil.copy2(source_file, temp_source)
        print(f"Copied source to: {temp_source}")
        
        # Read and update the source file
        with open(temp_source, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Update URL
        new_url = f"http://{ip_address}:{port}"
        content = re.sub(
            r'(TARGET_URL:\s*str\s*=\s*)"[^"]*"',
            f'TARGET_URL: str = "{new_url}"',
            content
        )
        
        # Update unlock key
        content = re.sub(
            r'(UNLOCK_HOTKEY:\s*str\s*=\s*)"[^"]*"',
            f'UNLOCK_HOTKEY: str = "{unlock_key}"',
            content
        )
        
        # Update unlock count
        content = re.sub(
            r'(UNLOCK_PRESS_COUNT:\s*int\s*=\s*)\d+',
            f'UNLOCK_PRESS_COUNT: int = {unlock_count}',
            content
        )
        
        # Write updated content
        with open(temp_source, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated source with config: {new_url}")
        
        # Build EXE using PyInstaller
        output_dir = os.path.join(temp_dir, "dist")
        build_dir = os.path.join(temp_dir, "build")
        spec_dir = temp_dir
        
        # Ensure output directories exist
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(build_dir, exist_ok=True)
        
        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--noconsole",
            "--uac-admin",
            "--name", "KioskLocker",
            "--distpath", output_dir,
            "--workpath", build_dir,
            "--specpath", spec_dir,
            "--clean",
            "--log-level", "ERROR",
            temp_source
        ]
        
        print(f"Running PyInstaller: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            print(f"PyInstaller error: {result.stderr}")
            raise RuntimeError(f"Build failed: {result.stderr}")
        
        print(f"PyInstaller completed successfully")
        
        # Find the built EXE
        exe_path = os.path.join(output_dir, "KioskLocker.exe")
        print(f"Looking for EXE at: {exe_path}")
        
        if not os.path.exists(exe_path):
            # Try to find any exe in the dist folder
            if os.path.exists(output_dir):
                exe_files = [f for f in os.listdir(output_dir) if f.endswith('.exe')]
                if exe_files:
                    exe_path = os.path.join(output_dir, exe_files[0])
                    print(f"Found EXE: {exe_path}")
                else:
                    raise FileNotFoundError("EXE file not found after build")
            else:
                raise FileNotFoundError(f"Dist directory not found: {output_dir}")
        
        print(f"Build successful. EXE size: {os.path.getsize(exe_path)} bytes")
        return exe_path, temp_dir
        
    except Exception as e:
        # Clean up temp directory on error
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass
        raise e


# =============================================================================
#  ROUTES
# =============================================================================

@app.route('/')
def index():
    """Home page - redirect to login or dashboard."""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login page."""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = request.form.get('remember') == 'on'
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            if not user.is_approved and not user.is_admin:
                flash('Your account is pending admin approval. Please wait.', 'warning')
                return render_template('login.html')
            
            login_user(user, remember=remember)
            flash(f'Welcome back, {user.full_name}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password.', 'danger')
    
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration page."""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username').strip()
        email = request.form.get('email').strip()
        full_name = request.form.get('full_name').strip()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        # Validation
        errors = []
        if not username or len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        if not email or '@' not in email:
            errors.append('Please enter a valid email address.')
        if not full_name:
            errors.append('Please enter your full name.')
        if not password or len(password) < 6:
            errors.append('Password must be at least 6 characters.')
        if password != confirm_password:
            errors.append('Passwords do not match.')
        
        # Check if username or email already exists
        if User.query.filter_by(username=username).first():
            errors.append('Username already taken.')
        if User.query.filter_by(email=email).first():
            errors.append('Email already registered.')
        
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('register.html')
        
        # Create new user
        user = User(
            username=username,
            email=email,
            full_name=full_name,
            is_admin=False,
            is_approved=False
        )
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        flash('Registration successful! Your account is pending admin approval.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    """User logout."""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
@approved_required
def dashboard():
    """User dashboard."""
    # Clean up old builds
    cleanup_old_builds()
    
    # Get user's builds
    user_builds = KioskBuild.query.filter_by(user_id=current_user.id).order_by(
        KioskBuild.created_at.desc()
    ).limit(10).all()
    
    # Convert builds to dictionaries for JSON serialization
    builds_data = [build.to_dict() for build in user_builds]
    
    return render_template('dashboard.html', user=current_user, builds=builds_data)


@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    """Admin panel to manage users."""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin.html', users=users)


@app.route('/admin/approve/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def approve_user(user_id):
    """Approve a user account."""
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash('Cannot approve admin accounts.', 'warning')
        return redirect(url_for('admin_panel'))
    
    user.is_approved = True
    db.session.commit()
    flash(f'User {user.username} has been approved.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/revoke/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def revoke_user(user_id):
    """Revoke user approval."""
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash('Cannot revoke admin accounts.', 'warning')
        return redirect(url_for('admin_panel'))
    
    user.is_approved = False
    db.session.commit()
    flash(f'User {user.username} has been revoked.', 'warning')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    """Delete a user account."""
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash('Cannot delete admin accounts.', 'warning')
        return redirect(url_for('admin_panel'))
    
    if user.id == current_user.id:
        flash('Cannot delete your own account.', 'warning')
        return redirect(url_for('admin_panel'))
    
    db.session.delete(user)
    db.session.commit()
    flash(f'User {user.username} has been deleted.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/api/build_kiosk', methods=['POST'])
@login_required
@approved_required
def build_kiosk():
    """API endpoint to build kiosk EXE."""
    try:
        data = request.get_json()
        
        ip_address = data.get('ip_address', '192.168.0.141')
        port = data.get('port', '5000')
        unlock_key = data.get('unlock_key', 'u')
        unlock_count = int(data.get('unlock_count', 5))
        
        # Validate inputs
        if not ip_address:
            return jsonify({'success': False, 'error': 'IP address is required'}), 400
        
        # Validate port
        try:
            port_num = int(port)
            if not (1 <= port_num <= 65535):
                return jsonify({'success': False, 'error': 'Port must be between 1 and 65535'}), 400
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid port number'}), 400
        
        # Validate unlock key
        if len(unlock_key) != 1:
            return jsonify({'success': False, 'error': 'Unlock key must be a single character'}), 400
        
        # Validate unlock count
        if not (1 <= unlock_count <= 20):
            return jsonify({'success': False, 'error': 'Unlock count must be between 1 and 20'}), 400
        # Determine build engine: GitHub Actions vs Local PyInstaller
        is_linux = sys.platform != 'win32'
        has_github_token = bool(os.environ.get('GITHUB_TOKEN'))
        use_github = is_linux or has_github_token or os.environ.get('USE_GITHUB_ACTIONS', '').lower() == 'true'
        
        filename = f"KioskLocker_{current_user.username}_{int(time.time())}.exe"
        final_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        
        config = json.dumps({
            'ip_address': ip_address,
            'port': port,
            'unlock_key': unlock_key,
            'unlock_count': unlock_count
        })
        
        if use_github:
            if not has_github_token:
                return jsonify({
                    'success': False,
                    'error': 'Running on Linux VPS: Please set GITHUB_TOKEN, GITHUB_REPO_OWNER, and GITHUB_REPO_NAME environment variables to compile Windows EXEs via GitHub Actions.'
                }), 400
            
            # Create build record in DB
            expires_at = datetime.now() + timedelta(minutes=10)
            build = KioskBuild(
                user_id=current_user.id,
                filename=filename,
                filepath=final_path,
                config=config,
                expires_at=expires_at
            )
            db.session.add(build)
            db.session.commit()
            
            # Trigger GitHub Actions
            success, msg = trigger_github_workflow(ip_address, port, unlock_key, unlock_count, build.id)
            if not success:
                db.session.delete(build)
                db.session.commit()
                return jsonify({'success': False, 'error': msg}), 500
                
            return jsonify({
                'success': True,
                'building': True,
                'mode': 'github_actions',
                'build_id': build.id,
                'expires_at': expires_at.isoformat(),
                'filename': filename,
                'message': 'Build dispatched to GitHub Actions Windows VM! Waiting for completion...'
            })

        # Local PyInstaller build (Windows Dev Environment)
        exe_path, temp_dir = create_kiosk_exe(
            ip_address, port, unlock_key, unlock_count, current_user.id
        )
        
        expires_at = datetime.now() + timedelta(minutes=5)
        shutil.copy2(exe_path, final_path)
        
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            print(f"Error cleaning temp dir: {e}")
        
        build = KioskBuild(
            user_id=current_user.id,
            filename=filename,
            filepath=final_path,
            config=config,
            expires_at=expires_at
        )
        db.session.add(build)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'building': False,
            'build_id': build.id,
            'expires_at': expires_at.isoformat(),
            'filename': filename,
            'message': 'Kiosk built successfully! Download it within 5 minutes.'
        })
        
    except FileNotFoundError as e:
        print(f"FileNotFoundError: {e}")
        return jsonify({'success': False, 'error': f'File not found: {str(e)}'}), 404
    except subprocess.TimeoutExpired:
        print("Build timed out")
        return jsonify({'success': False, 'error': 'Build timed out. Please try again.'}), 500
    except Exception as e:
        print(f"Build error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/build_status/<int:build_id>', methods=['GET'])
@login_required
@approved_required
def build_status(build_id):
    """Check the status of a kiosk build (polls GitHub Actions if file pending)."""
    build = KioskBuild.query.filter_by(id=build_id, user_id=current_user.id).first()
    if not build:
        return jsonify({'success': False, 'error': 'Build not found'}), 404
    
    file_exists = os.path.exists(build.filepath)
    status_msg = "File ready"
    
    # If file doesn't exist yet, attempt to fetch from GitHub Actions
    if not file_exists and os.environ.get('GITHUB_TOKEN'):
        github_status, status_msg = check_and_fetch_github_artifact(build.id, build.filepath)
        file_exists = os.path.exists(build.filepath)
    
    return jsonify({
        'success': True,
        'build_id': build.id,
        'filename': build.filename,
        'expired': build.is_expired(),
        'seconds_remaining': build.time_remaining(),
        'downloads': build.downloads,
        'file_exists': file_exists,
        'status_msg': status_msg
    })


@app.route('/download/<int:build_id>')
@login_required
@approved_required
def download_kiosk(build_id):
    """Download the kiosk EXE file."""
    build = KioskBuild.query.filter_by(id=build_id, user_id=current_user.id).first()
    if not build:
        flash('Build not found.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Check if expired using helper method
    if build.is_expired():
        flash('This download link has expired (valid for 5 minutes). Please rebuild the kiosk.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Check if file exists
    if not os.path.exists(build.filepath):
        flash('File not found. Please rebuild the kiosk.', 'danger')
        # Delete the database entry since file is missing
        db.session.delete(build)
        db.session.commit()
        return redirect(url_for('dashboard'))
    
    # Increment download count
    build.downloads += 1
    db.session.commit()
    
    return send_file(
        build.filepath,
        as_attachment=True,
        download_name=build.filename
    )


@app.route('/api/delete_build/<int:build_id>', methods=['POST'])
@login_required
@approved_required
def delete_build(build_id):
    """Delete a kiosk build."""
    build = KioskBuild.query.filter_by(id=build_id, user_id=current_user.id).first()
    if not build:
        return jsonify({'success': False, 'error': 'Build not found'}), 404
    
    try:
        if os.path.exists(build.filepath):
            os.remove(build.filepath)
            # Try to remove parent directory if empty
            parent_dir = os.path.dirname(build.filepath)
            if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                shutil.rmtree(parent_dir, ignore_errors=True)
    except:
        pass
    
    db.session.delete(build)
    db.session.commit()
    
    return jsonify({'success': True})


# =============================================================================
#  CREATE DATABASE TABLES
# =============================================================================

def init_db():
    """Initialize the database with tables and admin user."""
    with app.app_context():
        db.create_all()
        
        # Check if admin exists
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(
                username='admin',
                email='admin@unitecbt.com',
                full_name='Administrator',
                is_admin=True,
                is_approved=True
            )
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print("✅ Admin user created: admin / admin123")
        else:
            print("✅ Admin user already exists.")


# =============================================================================
#  RUN APPLICATION
# =============================================================================

if __name__ == '__main__':
    # Ensure uploads folder exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    
    # Initialize database tables first
    init_db()
    
    # Clean up old builds on startup
    with app.app_context():
        cleanup_old_builds()
    
    print("\n" + "="*70)
    print("  UNITE CBT APPLICATION v2.2.0 (FIXED)")
    print("="*70)
    print("  Admin Login: admin / admin123")
    print("  URL: http://127.0.0.1:5000")
    print("  EXE Valid For: 5 minutes")
    print("  Upload Folder: " + os.path.abspath(app.config['UPLOAD_FOLDER']))
    print("="*70 + "\n")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
