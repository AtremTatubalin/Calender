import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "app.db")
ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE", "SUPER-ADMIN-123")
SLOT_HOURS = (10, 12, 14)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")
app.config["DB_READY"] = False


@app.template_filter("ru_datetime")
def ru_datetime(value: str):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    return dt.strftime("%d.%m.%Y %H:%M")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            address TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_datetime TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('free', 'blocked', 'booked')) DEFAULT 'free',
            booked_by INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(booked_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    db.execute(
        """
        INSERT OR IGNORE INTO settings (key, value, updated_at)
        VALUES ('admin_secret_code', ?, ?)
        """,
        (ADMIN_SECRET_CODE, datetime.utcnow().isoformat()),
    )
    db.commit()

    refresh_slots(db)
    db.commit()
    app.config["DB_READY"] = True


def refresh_slots(db):
    now = datetime.utcnow()
    db.execute("DELETE FROM slots WHERE slot_datetime < ?", (now.isoformat(),))

    seed_default_slots(db, now)


def seed_default_slots(db, now: datetime):
    now = now.replace(minute=0, second=0, microsecond=0)
    start = now
    slots_to_add = []

    for day in range(0, 14):
        day_time = start + timedelta(days=day)
        for hour in SLOT_HOURS:
            dt = day_time.replace(hour=hour)
            slots_to_add.append((dt.isoformat(), "free", None, datetime.utcnow().isoformat()))

    db.executemany(
        """
        INSERT OR IGNORE INTO slots (slot_datetime, status, booked_by, created_at)
        VALUES (?, ?, ?, ?)
        """,
        slots_to_add,
    )


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        if not session.get("is_admin"):
            flash("Доступ только для администратора.", "error")
            return redirect(url_for("dashboard"))
        return view(**kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user():
    if not app.config.get("DB_READY"):
        init_db()
    else:
        refresh_slots(get_db())
        get_db().commit()

    user_id = session.get("user_id")
    g.user = None

    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        address = request.form.get("address", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        make_admin = request.form.get("is_admin") == "on"
        admin_code = request.form.get("admin_code", "").strip()

        error = None

        db = get_db()
        valid_admin_secret = get_admin_secret_code(db)

        if not first_name or not last_name or not address or not phone or not password:
            error = "Заполните все обязательные поля."
        elif make_admin and admin_code != valid_admin_secret:
            error = "Неверный секретный код для создания админ-аккаунта."

        if error is None:
            try:
                db.execute(
                    """
                    INSERT INTO users (first_name, last_name, address, phone, password_hash, is_admin, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        first_name,
                        last_name,
                        address,
                        phone,
                        generate_password_hash(password),
                        1 if make_admin else 0,
                        datetime.utcnow().isoformat(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                error = "Пользователь с таким номером телефона уже существует."
            else:
                flash("Регистрация успешна. Теперь войдите в аккаунт.", "success")
                return redirect(url_for("login"))

        flash(error, "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Неверный номер телефона или пароль.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["is_admin"] = bool(user["is_admin"])
            flash("Вы успешно вошли.", "success")
            return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли из аккаунта.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    today = datetime.utcnow().date().isoformat()

    free_slots = db.execute(
        """
        SELECT * FROM slots
        WHERE status = 'free'
          AND date(slot_datetime) > date(?)
        ORDER BY slot_datetime
        """,
        (today,),
    ).fetchall()
    calendar_days = build_calendar_days(db, today)

    blocked_slots = []
    users = []
    if session.get("is_admin"):
        my_bookings = db.execute(
            """
            SELECT s.*, u.first_name, u.last_name, u.phone
            FROM slots s
            LEFT JOIN users u ON s.booked_by = u.id
            WHERE s.status = 'booked'
            ORDER BY s.slot_datetime
            """
        ).fetchall()
    else:
        my_bookings = db.execute(
            """
            SELECT * FROM slots
            WHERE status = 'booked' AND booked_by = ?
            ORDER BY slot_datetime
            """,
            (session["user_id"],),
        ).fetchall()

    if session.get("is_admin"):
        blocked_slots = db.execute(
            "SELECT * FROM slots WHERE status = 'blocked' ORDER BY slot_datetime"
        ).fetchall()
        users = db.execute(
            """
            SELECT id, first_name, last_name, phone, address, is_admin, created_at
            FROM users
            ORDER BY created_at DESC
            """
        ).fetchall()

    return render_template(
        "dashboard.html",
        free_slots=free_slots,
        calendar_days=calendar_days,
        my_bookings=my_bookings,
        blocked_slots=blocked_slots,
        users=users,
    )


@app.route("/book/<int:slot_id>", methods=["POST"])
@login_required
def book_slot(slot_id):
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
    today = datetime.utcnow().date().isoformat()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["slot_datetime"][:10] == today:
        flash("Запись на занятия в текущий день недоступна.", "error")
    elif slot["status"] != "free":
        flash("Этот слот уже недоступен.", "error")
    else:
        db.execute(
            "UPDATE slots SET status = 'booked', booked_by = ? WHERE id = ?",
            (session["user_id"], slot_id),
        )
        db.commit()
        flash("Вы успешно записались.", "success")

    return redirect(url_for("dashboard"))


@app.route("/cancel/<int:slot_id>", methods=["POST"])
@login_required
def cancel_slot(slot_id):
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["status"] != "booked" or (
        slot["booked_by"] != session["user_id"] and not session.get("is_admin")
    ):
        flash("Нельзя отменить эту запись.", "error")
    else:
        db.execute("UPDATE slots SET status = 'free', booked_by = NULL WHERE id = ?", (slot_id,))
        db.commit()
        flash("Запись отменена.", "success")

    return redirect(url_for("dashboard"))


@app.route("/delete/<int:slot_id>", methods=["POST"])
@login_required
def delete_slot(slot_id):
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["status"] != "booked" or slot["booked_by"] != session["user_id"]:
        flash("Нельзя удалить эту запись.", "error")
    else:
        db.execute("UPDATE slots SET status = 'free', booked_by = NULL WHERE id = ?", (slot_id,))
        db.commit()
        flash("Ваша запись удалена из списка — слот снова свободен.", "success")

    return redirect(url_for("dashboard"))


@app.route("/admin/block/<int:slot_id>", methods=["POST"])
@admin_required
def block_slot(slot_id):
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["status"] == "booked":
        flash("Нельзя блокировать уже занятую запись.", "error")
    else:
        db.execute("UPDATE slots SET status = 'blocked', booked_by = NULL WHERE id = ?", (slot_id,))
        db.commit()
        flash("Слот заблокирован.", "success")

    return redirect(url_for("dashboard"))


@app.route("/admin/unblock/<int:slot_id>", methods=["POST"])
@admin_required
def unblock_slot(slot_id):
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["status"] != "blocked":
        flash("Этот слот не заблокирован.", "error")
    else:
        db.execute("UPDATE slots SET status = 'free' WHERE id = ?", (slot_id,))
        db.commit()
        flash("Слот разблокирован.", "success")

    return redirect(url_for("dashboard"))


@app.route("/admin/add-slot", methods=["POST"])
@admin_required
def add_slot():
    slot_datetime = request.form.get("slot_datetime", "").strip()
    if not slot_datetime:
        flash("Укажите дату и время.", "error")
        return redirect(url_for("dashboard"))

    try:
        dt = datetime.fromisoformat(slot_datetime)
    except ValueError:
        flash("Неверный формат даты и времени.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO slots (slot_datetime, status, booked_by, created_at)
            VALUES (?, 'free', NULL, ?)
            """,
            (dt.isoformat(), datetime.utcnow().isoformat()),
        )
        db.commit()
        flash("Новый слот добавлен.", "success")
    except sqlite3.IntegrityError:
        flash("Такой слот уже существует.", "error")

    return redirect(url_for("dashboard"))


@app.route("/admin/update-secret-code", methods=["POST"])
@admin_required
def update_admin_secret_code():
    new_code = request.form.get("new_admin_code", "").strip()
    if not new_code:
        flash("Новый секретный код не может быть пустым.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    db.execute(
        """
        UPDATE settings
        SET value = ?, updated_at = ?
        WHERE key = 'admin_secret_code'
        """,
        (new_code, datetime.utcnow().isoformat()),
    )
    db.commit()
    flash("Секретный код для регистрации админа обновлен.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
@admin_required
def delete_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if user is None:
        flash("Пользователь не найден.", "error")
        return redirect(url_for("dashboard"))

    if user["id"] == session["user_id"]:
        flash("Нельзя удалить свою учетную запись администратора.", "error")
        return redirect(url_for("dashboard"))

    db.execute(
        "UPDATE slots SET status = 'free', booked_by = NULL WHERE booked_by = ?",
        (user_id,),
    )
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash("Пользователь удален.", "success")
    return redirect(url_for("dashboard"))


def get_admin_secret_code(db):
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'admin_secret_code'"
    ).fetchone()
    if row is None:
        return ADMIN_SECRET_CODE
    return row["value"]


def build_calendar_days(db, today_iso):
    rows = db.execute(
        """
        SELECT id, slot_datetime, status
        FROM slots
        WHERE date(slot_datetime) >= date(?)
        ORDER BY slot_datetime
        """,
        (today_iso,),
    ).fetchall()

    grouped = {}
    for row in rows:
        date_key = row["slot_datetime"][:10]
        grouped.setdefault(date_key, []).append(row)

    calendar_days = []
    for date_key, day_slots in grouped.items():
        free_count = sum(1 for slot in day_slots if slot["status"] == "free")
        blocked_count = sum(1 for slot in day_slots if slot["status"] == "blocked")
        booked_count = sum(1 for slot in day_slots if slot["status"] == "booked")

        if date_key == today_iso:
            day_status = "today-locked"
        elif free_count > 0:
            day_status = "free"
        elif booked_count == len(day_slots):
            day_status = "full"
        elif blocked_count == len(day_slots):
            day_status = "blocked"
        else:
            day_status = "mixed"

        calendar_days.append(
            {
                "date": date_key,
                "status": day_status,
                "slots": day_slots,
            }
        )
    return calendar_days


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
