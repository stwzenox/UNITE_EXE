"""
Database helper for UNITE CBT
Run this to initialize the database: python database.py
"""

from app import app, db, User

def init_database():
    """Initialize database with tables and admin user."""
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
        
        print("✅ Database initialized successfully!")

if __name__ == '__main__':
    init_database()
