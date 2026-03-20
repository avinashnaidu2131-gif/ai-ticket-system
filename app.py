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
    if current_user.is_authenticated:
        return redirect("/dashboard")
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
    # Already logged in → go to dashboard
    if current_user.is_authenticated:
        return redirect("/dashboard")

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
    search_query    = request.args.get("q", "").strip()

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
    if search_query:
        from sqlalchemy import or_
        q = q.filter(
            or_(
                Ticket.title.ilike(f"%{search_query}%"),
                Ticket.description.ilike(f"%{search_query}%"),
                Ticket.tags.ilike(f"%{search_query}%"),
            )
        )

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
        notify_ticket_created(ticket, current_user)

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
    notify_status_changed(ticket, ticket.status)
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

# ── Auto-create DB tables on startup ─────────────────────────────────────────
with app.app_context():
    os.makedirs("static/uploads", exist_ok=True)
    db.create_all()

    # Safe migration - add missing columns without losing data
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.connect() as conn:
        # Add company.plan if missing
        try:
            cols = [c["name"] for c in insp.get_columns("company")]
            if "plan" not in cols:
                conn.execute(text("ALTER TABLE company ADD COLUMN plan VARCHAR(20) DEFAULT 'free'"))
                conn.commit()
                print("Migrated: company.plan added")
            if "stripe_id" not in cols:
                conn.execute(text("ALTER TABLE company ADD COLUMN stripe_id VARCHAR(100)"))
                conn.commit()
                print("Migrated: company.stripe_id added")
        except Exception as e:
            print(f"Migration note: {e}")
    print("DB ready!")


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


# ── Email Notifications ───────────────────────────────────────────────────────
def send_email(to, subject, body):
    """Send email via Gmail SMTP. Set MAIL_USER and MAIL_PASS env vars."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    mail_user = os.environ.get("MAIL_USER", "")
    mail_pass = os.environ.get("MAIL_PASS", "")
    if not mail_user or not mail_pass:
        return  # silently skip if not configured

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = mail_user
        msg["To"]      = to
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(mail_user, mail_pass)
            server.sendmail(mail_user, to, msg.as_string())
    except Exception as e:
        print(f"Email error: {e}")


def notify_ticket_created(ticket, user):
    """Email admin when new ticket submitted."""
    admins = User.query.filter_by(
        company_id=user.company_id, role="admin"
    ).all()
    for admin in admins:
        if admin.email:
            send_email(
                to=admin.email,
                subject=f"[SupportAI] New Ticket #{ticket.id}: {ticket.title}",
                body=f"""
                <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0a0a0f;color:#e8e8f0;padding:2rem;border-radius:12px;">
                  <h2 style="color:#e8ff47;">New Support Ticket</h2>
                  <p><strong>Ticket #:</strong> {ticket.id}</p>
                  <p><strong>Title:</strong> {ticket.title}</p>
                  <p><strong>Category:</strong> {ticket.category}</p>
                  <p><strong>Priority:</strong> {ticket.priority}</p>
                  <p><strong>From:</strong> {user.username}</p>
                  <p><strong>Description:</strong><br>{ticket.description}</p>
                  <a href="{os.environ.get('APP_URL','')}/dashboard" 
                     style="display:inline-block;background:#e8ff47;color:#0a0a0f;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:bold;margin-top:1rem;">
                    View Dashboard →
                  </a>
                </div>
                """
            )


def notify_status_changed(ticket, new_status):
    """Email customer when ticket status changes."""
    submitter = User.query.get(ticket.user_id)
    if submitter and submitter.email:
        send_email(
            to=submitter.email,
            subject=f"[SupportAI] Ticket #{ticket.id} is now {new_status}",
            body=f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0a0a0f;color:#e8e8f0;padding:2rem;border-radius:12px;">
              <h2 style="color:#e8ff47;">Ticket Status Updated</h2>
              <p>Your ticket <strong>#{ticket.id}: {ticket.title}</strong> status changed to:</p>
              <p style="font-size:1.4rem;font-weight:bold;color:#47ffe8;">{new_status}</p>
              {"<p><strong>Agent Reply:</strong><br>" + ticket.reply + "</p>" if ticket.reply else ""}
              <a href="{os.environ.get('APP_URL','')}/dashboard"
                 style="display:inline-block;background:#e8ff47;color:#0a0a0f;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:bold;margin-top:1rem;">
                View Ticket →
              </a>
            </div>
            """
        )


# ── AI Auto-Reply Suggestions ─────────────────────────────────────────────────
@app.route("/api/ai_reply/<int:ticket_id>")
@login_required
def ai_reply_suggestion(ticket_id):
    """Generate AI reply suggestion for a ticket."""
    if current_user.role not in ("admin", "agent"):
        return jsonify({"error": "forbidden"}), 403

    ticket = Ticket.query.get_or_404(ticket_id)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        # Fallback template replies based on category
        templates = {
            "Billing":        "Thank you for contacting us about your billing issue. We have reviewed your account and our team is looking into this. We will resolve it within 24-48 hours and keep you updated.",
            "Authentication": "Thank you for reaching out. We understand you are having trouble accessing your account. Please try resetting your password. If the issue persists, our team will assist you within a few hours.",
            "Technical":      "Thank you for reporting this technical issue. Our engineering team has been notified and is investigating the problem. We expect to have this resolved shortly.",
            "Feature Request":"Thank you for your valuable suggestion! We have logged this feature request and our product team will review it. We appreciate your feedback.",
            "General":        "Thank you for contacting SupportAI support. We have received your message and a team member will get back to you within 24 hours.",
        }
        reply = templates.get(ticket.category, templates["General"])
        return jsonify({"reply": reply, "source": "template"})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": f"""You are a helpful customer support agent. Write a professional, empathetic reply to this support ticket.
Keep it under 100 words. Be specific to the issue. Do not use placeholders.

Ticket Title: {ticket.title}
Category: {ticket.category}
Priority: {ticket.priority}
Description: {ticket.description}

Reply:"""
                }]
            },
            timeout=15,
        )
        reply = resp.json()["content"][0]["text"].strip()
        return jsonify({"reply": reply, "source": "claude"})
    except Exception as e:
        return jsonify({"reply": "Thank you for contacting us. Our team will review your ticket and respond shortly.", "source": "fallback"})

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)