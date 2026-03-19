"""
Run this ONCE to fix existing citizen accounts:
moves all "Public" company users into the first real company (admin's company).

Usage: python fix_db.py
"""
from app import app
from models import db, User, Company, Ticket

with app.app_context():
    public = Company.query.filter_by(name="Public").first()
    if not public:
        print("No 'Public' company found — nothing to fix.")
    else:
        # Find the real company (not Public, lowest id = admin's company)
        real = Company.query.filter(Company.name != "Public").order_by(Company.id.asc()).first()
        if not real:
            print("No real company found.")
        else:
            print(f"Moving users from '{public.name}' → '{real.name}'")
            # Move users
            users = User.query.filter_by(company_id=public.id).all()
            for u in users:
                u.company_id = real.id
                print(f"  User moved: {u.username}")
            # Move tickets
            tickets = Ticket.query.filter_by(company_id=public.id).all()
            for t in tickets:
                t.company_id = real.id
                print(f"  Ticket moved: #{t.id} {t.title}")
            db.session.commit()
            print("Done! All citizen tickets are now visible to admin.")