import os
import re
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "app.db")
ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE", "SUPER-ADMIN-123")
SLOT_HOURS = (10, 12, 14)
PHONE_REGEX = re.compile(r"^\+7\d{10}$")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")
app.config["DB_READY"] = False

def parse_iso_datetime(value):
    if not value:
        return None
    normalized = value.replace("Z", "+00:00") if isinstance(value, str) else value
    try:
        return datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None


@app.template_filter("ru_datetime")
def ru_datetime(value: str):
    dt = parse_iso_datetime(value)
    if not dt:
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

        CREATE TABLE IF NOT EXISTS lesson_statistics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_slot_id INTEGER NOT NULL,
            slot_datetime TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            added_at TEXT NOT NULL,
            FOREIGN KEY(source_slot_id) REFERENCES slots(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
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
    db.execute(
        "DELETE FROM slots WHERE slot_datetime < ? AND status != 'booked'",
        (now.isoformat(),),
    )
    db.execute(
        """
        DELETE FROM slots
        WHERE status != 'booked'
          AND (
            CAST(strftime('%H', slot_datetime) AS INTEGER) NOT IN (10, 12, 14)
            OR strftime('%M', slot_datetime) != '00'
          )
        """
    )

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


def is_valid_phone(phone: str) -> bool:
    return bool(PHONE_REGEX.fullmatch(phone))


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
        elif not is_valid_phone(phone):
            error = "Номер телефона должен быть в формате +7XXXXXXXXXX."
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

        if not is_valid_phone(phone):
            flash("Номер телефона должен быть в формате +7XXXXXXXXXX.", "error")
            return render_template("login.html")

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


@app.route("/pwa-guide")
def pwa_guide():
    return render_template("pwa_guide.html")


@app.route("/account")
@login_required
def account():
    return render_template("account.html")


@app.route("/account/update-profile", methods=["POST"])
@login_required
def update_profile():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    address = request.form.get("address", "").strip()

    if not first_name or not last_name or not address:
        flash("Имя, фамилия и адрес не могут быть пустыми.", "error")
        return redirect(url_for("account"))

    db = get_db()
    db.execute(
        """
        UPDATE users
        SET first_name = ?, last_name = ?, address = ?
        WHERE id = ?
        """,
        (first_name, last_name, address, g.user["id"]),
    )
    db.commit()
    flash("Данные профиля обновлены.", "success")
    return redirect(url_for("account"))


@app.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_delete", "") == "yes"

    if not confirm:
        flash("Подтвердите удаление учетной записи.", "error")
        return redirect(url_for("account"))

    if g.user is None or not check_password_hash(g.user["password_hash"], password):
        flash("Неверный пароль. Удаление отменено.", "error")
        return redirect(url_for("account"))

    db = get_db()
    db.execute(
        "UPDATE slots SET status = 'free', booked_by = NULL WHERE booked_by = ?",
        (g.user["id"],),
    )
    db.execute("DELETE FROM users WHERE id = ?", (g.user["id"],))
    db.commit()
    session.clear()
    flash("Учетная запись удалена.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    today = datetime.utcnow().date().isoformat()
    calendar_days = build_calendar_days(db, today)
    now_iso = datetime.utcnow().isoformat()

    blocked_slots = []
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
        my_bookings = [
            {**dict(slot), "is_past": slot["slot_datetime"] < now_iso} for slot in my_bookings
        ]
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
        current_secret_code = get_admin_secret_code(db)
    else:
        current_secret_code = ""

    return render_template(
        "dashboard.html",
        calendar_days=calendar_days,
        my_bookings=my_bookings,
        blocked_slots=blocked_slots,
        current_secret_code=current_secret_code,
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


@app.route("/admin/add-to-statistics/<int:slot_id>", methods=["POST"])
@admin_required
def add_to_statistics(slot_id):
    db = get_db()
    slot = db.execute(
        """
        SELECT s.*, u.first_name, u.last_name, u.phone, u.address
        FROM slots s
        LEFT JOIN users u ON s.booked_by = u.id
        WHERE s.id = ?
        """,
        (slot_id,),
    ).fetchone()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["status"] != "booked" or slot["booked_by"] is None:
        flash("Этот слот нельзя добавить в статистику.", "error")
    elif slot["slot_datetime"] >= datetime.utcnow().isoformat():
        flash("Добавлять в статистику можно только прошедшие занятия.", "error")
    else:
        db.execute(
            """
            INSERT INTO lesson_statistics
            (source_slot_id, slot_datetime, user_id, first_name, last_name, phone, address, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot["id"],
                slot["slot_datetime"],
                slot["booked_by"],
                slot["first_name"],
                slot["last_name"],
                slot["phone"],
                slot["address"],
                datetime.utcnow().isoformat(),
            ),
        )
        db.execute("UPDATE slots SET status = 'free', booked_by = NULL WHERE id = ?", (slot_id,))
        db.commit()
        flash("Занятие добавлено в статистику.", "success")

    return redirect(url_for("dashboard"))


@app.route("/admin/skip-statistics/<int:slot_id>", methods=["POST"])
@admin_required
def skip_statistics(slot_id):
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()

    if slot is None:
        flash("Слот не найден.", "error")
    elif slot["status"] != "booked":
        flash("Этот слот уже обработан.", "error")
    elif slot["slot_datetime"] >= datetime.utcnow().isoformat():
        flash("Эта запись еще не завершена.", "error")
    else:
        db.execute("UPDATE slots SET status = 'free', booked_by = NULL WHERE id = ?", (slot_id,))
        db.commit()
        flash("Запись удалена без добавления в статистику.", "success")

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
    old_code = request.form.get("old_admin_code", "").strip()
    new_code = request.form.get("new_admin_code", "").strip()
    db = get_db()
    current_code = get_admin_secret_code(db)

    if old_code != current_code:
        flash("Старый секретный код введен неверно.", "error")
        return redirect(url_for("dashboard"))

    if not new_code:
        flash("Новый секретный код не может быть пустым.", "error")
        return redirect(url_for("dashboard"))

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


@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    users = db.execute(
        """
        SELECT id, first_name, last_name, phone, address, is_admin, created_at
        FROM users
        ORDER BY created_at DESC
        """
    ).fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/statistics")
@admin_required
def admin_statistics():
    db = get_db()
    rows = db.execute(
        """
        SELECT slot_datetime, first_name, last_name, phone, address
        FROM lesson_statistics
        ORDER BY slot_datetime DESC
        """
    ).fetchall()

    months = {}
    for row in rows:
        dt = parse_iso_datetime(row["slot_datetime"])
        if not dt:
            continue

        month_key = dt.strftime("%Y-%m")
        month_data = months.setdefault(
            month_key,
            {
                "month_key": month_key,
                "month_label": month_label_ru(dt),
                "count": 0,
                "items": [],
            },
        )
        month_data["count"] += 1
        month_data["items"].append(
            {
                "date": dt.strftime("%d.%m.%Y"),
                "time": dt.strftime("%H:%M"),
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "phone": row["phone"],
                "address": row["address"],
            }
        )

    month_stats = sorted(months.values(), key=lambda item: item["month_key"], reverse=True)
    return render_template("admin_statistics.html", month_stats=month_stats)


@app.route("/admin/block-day", methods=["POST"])
@admin_required
def block_day():
    date_str = request.form.get("date", "").strip()
    if not date_str:
        flash("Дата для блокировки не передана.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    db.execute(
        """
        UPDATE slots
        SET status = 'blocked', booked_by = NULL
        WHERE date(slot_datetime) = date(?) AND status != 'booked'
        """,
        (date_str,),
    )
    db.commit()
    flash(f"Все свободные слоты на {date_str} заблокированы.", "success")
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


@app.route("/admin/users/update/<int:user_id>", methods=["POST"])
@admin_required
def admin_update_user(user_id):
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    address = request.form.get("address", "").strip()

    if not first_name or not last_name or not address:
        flash("Имя, фамилия и адрес не могут быть пустыми.", "error")
        return redirect(url_for("admin_users"))

    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        flash("Пользователь не найден.", "error")
        return redirect(url_for("admin_users"))

    db.execute(
        """
        UPDATE users
        SET first_name = ?, last_name = ?, address = ?
        WHERE id = ?
        """,
        (first_name, last_name, address, user_id),
    )
    db.commit()
    flash("Данные пользователя обновлены.", "success")
    return redirect(url_for("admin_users"))


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
                "date_label": format_day_label(date_key),
                "weekday_label": weekday_label(date_key),
                "status": day_status,
                "slots": day_slots,
            }
        )
    return calendar_days


def format_day_label(date_iso):
    dt = datetime.fromisoformat(date_iso)
    return dt.strftime("%d.%m")


def weekday_label(date_iso):
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    dt = datetime.fromisoformat(date_iso)
    return weekdays[dt.weekday()]


def month_label_ru(dt: datetime):
    months = [
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    ]
    return f"{months[dt.month - 1].capitalize()} {dt.year}"


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
