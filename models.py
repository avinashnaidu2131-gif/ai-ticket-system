from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# ── Company ───────────────────────────────────────────────────────────────────
class Company(db.Model):
    __tablename__ = "company"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    plan        = db.Column(db.String(20), default="free")  # free|starter|pro|enterprise
    stripe_id   = db.Column(db.String(100), nullable=True)

    users   = db.relationship("User",   backref="company", lazy=True)
    tickets = db.relationship("Ticket", backref="company", lazy=True)


# ── User ──────────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = "user"

    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(120), unique=True, nullable=False)
    email        = db.Column(db.String(200), unique=True, nullable=True)
    _password    = db.Column("password", db.String(256), nullable=False)
    role         = db.Column(db.String(20), default="customer")
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_login   = db.Column(db.DateTime, nullable=True)

    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)

    tickets_submitted = db.relationship(
        "Ticket", foreign_keys="Ticket.user_id", backref="submitter", lazy=True
    )
    tickets_assigned  = db.relationship(
        "Ticket", foreign_keys="Ticket.assigned_to", backref="agent", lazy=True
    )

    @property
    def password(self):
        raise AttributeError("password is write-only")

    @password.setter
    def password(self, raw: str):
        self._password = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self._password, raw)

    def __repr__(self):
        return f"<User {self.username} [{self.role}]>"


# ── Ticket ────────────────────────────────────────────────────────────────────
class Ticket(db.Model):
    __tablename__ = "ticket"

    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text,        nullable=False)

    category    = db.Column(db.String(50))
    priority    = db.Column(db.String(20),  default="Medium")
    confidence  = db.Column(db.Float,       default=0.0)
    tags        = db.Column(db.String(200))
    explanation = db.Column(db.Text)

    status      = db.Column(db.String(30),  default="Open")
    reply       = db.Column(db.Text)
    screenshot  = db.Column(db.String(200))

    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"),    nullable=False)
    company_id  = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"),    nullable=True)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)

    audit_logs  = db.relationship("AuditLog", backref="ticket", lazy=True, cascade="all, delete-orphan")

    def resolve(self):
        self.status = "Resolved"
        self.resolved_at = datetime.utcnow()

    def __repr__(self):
        return f"<Ticket #{self.id} [{self.status}] {self.title[:40]}>"


# ── Audit Log ─────────────────────────────────────────────────────────────────
class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id         = db.Column(db.Integer, primary_key=True)
    ticket_id  = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"),   nullable=False)
    action     = db.Column(db.String(100))
    detail     = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship("User", backref="audit_logs")


# ── Chat Message ──────────────────────────────────────────────────────────────
class ChatMessage(db.Model):
    __tablename__ = "chat_message"

    id         = db.Column(db.Integer, primary_key=True)
    ticket_id  = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"),   nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"),nullable=False)
    username   = db.Column(db.String(120))
    role       = db.Column(db.String(20))   # admin | agent | customer
    message    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)