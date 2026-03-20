import os
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.utils import secure_filename

from models import db, User, Ticket, Company, AuditLog, ChatMessage
from classifier import predict_ticket

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
app.config["SECRET_KEY"]                = os.environ.get("SECRET_KEY", "change-me-in-production")
# Render uses postgres:// but SQLAlchemy needs postgresql://
_db_url = os.environ.get("DATABASE_URL", "sqlite:///database.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"]            = "static/uploads"
app.config["MAX_CONTENT_LENGTH"]       = 5 * 1024 * 1024  # 5 MB upload limit

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# ── Helpers ───────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def log_action(ticket_id, action, detail=""):
    entry = AuditLog(
        ticket_id=ticket_id,
        user_id=current_user.id,
        action=action,
        detail=detail,
    )
    db.session.add(entry)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return redirect("/dashboard" if current_user.is_authenticated else "/login")


@app.route("/register", methods=["GET"])
def register():
    return render_template("register.html")


@app.route("/register/company", methods=["POST"])
def register_company():
    """Create a new company workspace + admin account."""
    company_name = request.form["company"].strip()
    username     = request.form["username"].strip()
    email        = request.form.get("email", "").strip() or None
    raw_password = request.form["password"]

    if User.query.filter_by(username=username).first():
        flash("Username already taken.", "danger")
        return redirect("/register")

    company = Company(name=company_name)
    db.session.add(company)
    db.session.flush()

    user = User(username=username, email=email, role="admin", company_id=company.id)
    user.password = raw_password
    db.session.add(user)
    db.session.commit()

    flash("Workspace created! Please log in.", "success")
    return redirect("/login")


@app.route("/register/citizen", methods=["POST"])
def register_citizen():
    """Self-register as a citizen under the main (first) company so admin can see their tickets."""
    username     = request.form["username"].strip()
    email        = request.form.get("email", "").strip() or None
    raw_password = request.form["password"]

    if User.query.filter_by(username=username).first():
        flash("Username already taken.", "danger")
        return redirect("/register")

    # Assign citizen to the first company (admin company) so tickets are visible to admin
    main_company = Company.query.order_by(Company.id.asc()).first()
    if not main_company:
        main_company = Company(name="Default")
        db.session.add(main_company)
        db.session.flush()

    user = User(
        username=username,
        email=email,
        role="customer",
        company_id=main_company.id,
    )
    user.password = raw_password
    db.session.add(user)
    db.session.commit()

    flash("Account created! Please log in.", "success")
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()
            return redirect("/dashboard")

        flash("Invalid credentials.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect("/login")


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    status_filter   = request.args.get("status")
    priority_filter = request.args.get("priority")
    category_filter = request.args.get("category")

    # Admins/agents see ALL tickets in their company
    # Customers only see their own tickets
    if current_user.role in ("admin", "agent"):
        q = Ticket.query.filter_by(company_id=current_user.company_id)
    else:
        q = Ticket.query.filter_by(
            company_id=current_user.company_id,
            user_id=current_user.id
        )

    if status_filter:
        q = q.filter_by(status=status_filter)
    if priority_filter:
        q = q.filter_by(priority=priority_filter)
    if category_filter:
        q = q.filter_by(category=category_filter)

    tickets = q.order_by(Ticket.created_at.desc()).all()

    # Agents list for assignment dropdown
    agents = User.query.filter_by(
        company_id=current_user.company_id
    ).filter(User.role.in_(["admin", "agent"])).all()

    return render_template("dashboard.html", tickets=tickets, agents=agents)


# ── Submit ticket ─────────────────────────────────────────────────────────────
@app.route("/submit", methods=["GET", "POST"])
@login_required
def submit():
    if request.method == "POST":
        title       = request.form["title"].strip()
        description = request.form["description"].strip()

        filename = None
        file = request.files.get("screenshot")
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        category, confidence, priority, tags, explanation = predict_ticket(description)

        ticket = Ticket(
            title=title,
            description=description,
            category=category,
            priority=priority,
            confidence=confidence,
            tags=tags,
            explanation=explanation,
            screenshot=filename,
            user_id=current_user.id,
            company_id=current_user.company_id,
        )
        db.session.add(ticket)
        db.session.flush()
        log_action(ticket.id, "ticket_created", f"Category={category}, Priority={priority}")
        db.session.commit()

        flash("Ticket submitted successfully!", "success")
        return redirect("/dashboard")

    return render_template("submit_ticket.html")


# ── Update status ─────────────────────────────────────────────────────────────
@app.route("/update_status/<int:id>", methods=["POST"])
@login_required
def update_status(id):
    if current_user.role not in ("admin", "agent"):
        return "Access denied", 403

    ticket = Ticket.query.get_or_404(id)
    old_status    = ticket.status
    ticket.status = request.form["status"]
    ticket.updated_at = datetime.utcnow()

    if ticket.status == "Resolved":
        ticket.resolved_at = datetime.utcnow()

    log_action(id, "status_changed", f"{old_status} → {ticket.status}")
    db.session.commit()
    return redirect("/dashboard")


# ── Reply ─────────────────────────────────────────────────────────────────────
@app.route("/reply/<int:id>", methods=["POST"])
@login_required
def reply(id):
    if current_user.role not in ("admin", "agent"):
        return "Access denied", 403

    ticket       = Ticket.query.get_or_404(id)
    ticket.reply = request.form["reply"]
    ticket.updated_at = datetime.utcnow()

    log_action(id, "reply_added")
    db.session.commit()
    return redirect("/dashboard")


# ── Assign agent ──────────────────────────────────────────────────────────────
@app.route("/assign/<int:id>", methods=["POST"])
@login_required
def assign(id):
    if current_user.role != "admin":
        return "Access denied", 403

    ticket             = Ticket.query.get_or_404(id)
    agent_id           = request.form.get("agent_id")
    ticket.assigned_to = int(agent_id) if agent_id else None
    ticket.updated_at  = datetime.utcnow()

    log_action(id, "agent_assigned", f"agent_id={agent_id}")
    db.session.commit()
    return redirect("/dashboard")


# ── Invite agent (admin only) ─────────────────────────────────────────────────
@app.route("/invite_agent", methods=["POST"])
@login_required
def invite_agent():
    if current_user.role != "admin":
        return "Access denied", 403

    username = request.form["username"].strip()
    password = request.form["password"]

    if User.query.filter_by(username=username).first():
        flash("Username already exists.", "danger")
        return redirect("/dashboard")

    agent = User(username=username, role="agent", company_id=current_user.company_id)
    agent.password = password
    db.session.add(agent)
    db.session.commit()

    flash(f"Agent '{username}' created.", "success")
    return redirect("/dashboard")


# ── Analytics API ─────────────────────────────────────────────────────────────
@app.route("/api/analytics")
@login_required
def analytics():
    cid = current_user.company_id

    total      = Ticket.query.filter_by(company_id=cid).count()
    open_count = Ticket.query.filter_by(company_id=cid, status="Open").count()
    resolved   = Ticket.query.filter_by(company_id=cid, status="Resolved").count()

    by_category = db.session.execute(
        db.text("SELECT category, COUNT(*) as cnt FROM ticket WHERE company_id=:c GROUP BY category"),
        {"c": cid}
    ).fetchall()

    by_priority = db.session.execute(
        db.text("SELECT priority, COUNT(*) as cnt FROM ticket WHERE company_id=:c GROUP BY priority"),
        {"c": cid}
    ).fetchall()

    # Average resolution time (hours) for resolved tickets
    resolved_tickets = Ticket.query.filter_by(company_id=cid, status="Resolved").all()
    avg_hours = None
    if resolved_tickets:
        deltas = [
            (t.resolved_at - t.created_at).total_seconds() / 3600
            for t in resolved_tickets if t.resolved_at
        ]
        avg_hours = round(sum(deltas) / len(deltas), 1) if deltas else None

    return jsonify({
        "total": total,
        "open": open_count,
        "resolved": resolved,
        "avg_resolution_hours": avg_hours,
        "by_category": {r[0]: r[1] for r in by_category},
        "by_priority": {r[0]: r[1] for r in by_priority},
    })


# ── Ticket detail (JSON) ──────────────────────────────────────────────────────
@app.route("/api/ticket/<int:id>")
@login_required
def ticket_detail(id):
    ticket = Ticket.query.get_or_404(id)
    # Customers can only view their own tickets; admins/agents see all in company
    if ticket.company_id != current_user.company_id:
        return jsonify({"error": "forbidden"}), 403
    if current_user.role == "customer" and ticket.user_id != current_user.id:
        return jsonify({"error": "forbidden"}), 403

    logs = [
        {"action": l.action, "detail": l.detail,
         "by": l.actor.username, "at": l.created_at.isoformat()}
        for l in ticket.audit_logs
    ]

    return jsonify({
        "id": ticket.id, "title": ticket.title,
        "description": ticket.description,
        "category": ticket.category, "priority": ticket.priority,
        "confidence": ticket.confidence, "tags": ticket.tags,
        "explanation": ticket.explanation, "status": ticket.status,
        "reply": ticket.reply,
        "created_at": ticket.created_at.isoformat(),
        "audit_logs": logs,
    })



# ── Live Chat ─────────────────────────────────────────────────────────────────
@app.route("/api/chat/send", methods=["POST"])
@login_required
def chat_send():
    data      = request.get_json()
    msg = ChatMessage(
        ticket_id  = data.get("ticket_id"),
        user_id    = current_user.id,
        username   = current_user.username,
        role       = current_user.role,
        message    = data.get("message", "").strip(),
        company_id = current_user.company_id,
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"ok": True, "id": msg.id, "at": msg.created_at.isoformat()})


@app.route("/api/chat/messages")
@login_required
def chat_messages():
    ticket_id = request.args.get("ticket_id")
    since     = request.args.get("since", "0")

    q = ChatMessage.query.filter_by(company_id=current_user.company_id)
    if ticket_id:
        q = q.filter_by(ticket_id=int(ticket_id))
    if since and since != "0":
        dt = datetime.utcfromtimestamp(float(since) / 1000)
        q = q.filter(ChatMessage.created_at > dt)

    msgs = q.order_by(ChatMessage.created_at.asc()).limit(200).all()
    return jsonify([{
        "id":       m.id,
        "username": m.username,
        "role":     m.role,
        "message":  m.message,
        "at":       m.created_at.isoformat(),
        "mine":     m.user_id == current_user.id,
    } for m in msgs])



@app.route("/api/chat/tickets")
@login_required
def chat_tickets():
    """Returns ticket list for the chat panel dropdown (admin/agent only)."""
    if current_user.role not in ("admin", "agent"):
        return jsonify([])
    tickets = Ticket.query.filter_by(company_id=current_user.company_id)                          .order_by(Ticket.created_at.desc()).limit(50).all()
    return jsonify([{"id": t.id, "title": t.title} for t in tickets])

# ── Auto-create DB tables on startup (needed for Render/production) ──────────
with app.app_context():
    os.makedirs("static/uploads", exist_ok=True)
    if os.environ.get("RESET_DB") == "true":
        db.drop_all()
        print("DB dropped!")
    db.create_all()
    print("DB tables created!")


# ── Pricing & Plans ───────────────────────────────────────────────────────────
PLANS = {
    "starter":    {"name": "Starter",    "price": 749,  "agents": 5,   "tickets": 100},
    "pro":        {"name": "Pro",        "price": 2499, "agents": 20,  "tickets": 1000},
    "enterprise": {"name": "Enterprise", "price": 8999, "agents": 999, "tickets": 99999},
}

@app.route("/pricing")
def pricing():
    current_plan = current_user.company.plan if current_user.is_authenticated else "free"
    return render_template("pricing.html", plans=PLANS, current_plan=current_plan)

@app.route("/subscribe/<plan>", methods=["POST"])
@login_required
def subscribe(plan):
    if plan not in PLANS:
        return "Invalid plan", 400
    # Store plan selection — payment handled via UPI/bank transfer
    return render_template("payment.html", plan=plan, details=PLANS[plan])

@app.route("/confirm_plan/<plan>", methods=["POST"])
@login_required
def confirm_plan(plan):
    if plan not in PLANS:
        return "Invalid plan", 400
    company = Company.query.get(current_user.company_id)
    company.plan = plan
    db.session.commit()
    flash(f"🎉 Plan upgraded to {plan.title()}! Welcome aboard.", "success")
    return redirect("/dashboard")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)