#!/usr/bin/env python3
"""
K&K Auctions — timed online auction site for K&K Bin Cleanup.
Stack: Flask + sqlite3 (stdlib) + Werkzeug. No ORM, no build step.
"""
import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask, g, render_template, request, redirect, url_for,
    flash, session, abort, send_from_directory,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KK_DB", os.path.join(BASE_DIR, "kk_auctions.db"))
UPLOAD_DIR = os.environ.get("KK_UPLOADS", os.path.join(BASE_DIR, "uploads"))
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB uploads

BUSINESS_PHONE = "6476793420"


# ---------------------------------------------------------------- DB helpers
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def utcnow():
    return datetime.now(timezone.utc)


def utcnow_iso():
    return utcnow().isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    soft_close_minutes INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    lot_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT,
    starting_bid_cents INTEGER NOT NULL DEFAULT 0,
    bid_increment_cents INTEGER NOT NULL DEFAULT 100,
    current_bid_cents INTEGER,
    current_bidder_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, lot_id)
);
CREATE INDEX IF NOT EXISTS idx_bids_lot ON bids(lot_id);
CREATE INDEX IF NOT EXISTS idx_lots_auction ON lots(auction_id);
"""


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    db.commit()
    # Seed on first run (empty users table)
    if db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        seed_db(db)
    db.close()


# ------------------------------------------------------- placeholder images
CATEGORY_ART = {
    "cars": ("🚗", "Vehicle"),
    "catalytic converters": ("🔧", "Catalytic Converter"),
    "engines": ("⚙️", "Engine"),
    "transmissions": ("🔩", "Transmission"),
    "rims": ("🛞", "Rims"),
    "equipment": ("🚜", "Equipment"),
    "parts": ("🧰", "Auto Parts"),
}


def placeholder_svg(title, category):
    """Tasteful inline-SVG placeholder (data URI) so lots look complete w/o uploads."""
    emoji, label = CATEGORY_ART.get((category or "").lower(), ("📦", "Lot"))
    short = (title or label)[:34]
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='600' viewBox='0 0 800 600'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0' stop-color='#123524'/><stop offset='1' stop-color='#0b1f16'/>"
        "</linearGradient></defs>"
        "<rect width='800' height='600' fill='url(#g)'/>"
        "<rect x='24' y='24' width='752' height='552' fill='none' stroke='#c9a227' stroke-width='3' opacity='0.55' rx='10'/>"
        f"<text x='400' y='265' font-size='110' text-anchor='middle'>{emoji}</text>"
        f"<text x='400' y='360' font-size='34' text-anchor='middle' fill='#e8c547' font-family='sans-serif' font-weight='bold'>{label}</text>"
        f"<text x='400' y='410' font-size='24' text-anchor='middle' fill='#cfd8cc' font-family='sans-serif'>{short}</text>"
        "<text x='400' y='520' font-size='20' text-anchor='middle' fill='#8fa393' font-family='sans-serif'>K&amp;K AUCTIONS</text>"
        "</svg>"
    )
    return "data:image/svg+xml," + urllib.parse.quote(svg)


def lot_image(lot):
    """First uploaded photo URL, else an SVG placeholder data URI."""
    db = get_db()
    row = db.execute(
        "SELECT filename FROM lot_photos WHERE lot_id = ? ORDER BY id LIMIT 1",
        (lot["id"],),
    ).fetchone()
    if row:
        return url_for("uploaded_file", filename=row["filename"])
    category = lot["category"] if "category" in lot.keys() else None
    return placeholder_svg(lot["title"], category)


def lot_images(lot_id, title, category):
    db = get_db()
    rows = db.execute(
        "SELECT filename FROM lot_photos WHERE lot_id = ? ORDER BY id", (lot_id,)
    ).fetchall()
    if rows:
        return [url_for("uploaded_file", filename=r["filename"]) for r in rows]
    return [placeholder_svg(title, category)]


# ------------------------------------------------------------------ seeding
def seed_db(db):
    now = utcnow_iso()
    db.execute(
        "INSERT INTO users (name, email, phone, password_hash, is_admin, created_at)"
        " VALUES (?, ?, ?, ?, 1, ?)",
        ("Site Admin", "admin@kkauctions.local", BUSINESS_PHONE,
         generate_password_hash("changeme123"), now),
    )
    starts = utcnow() - timedelta(hours=1)
    ends = utcnow() + timedelta(days=7)
    cur = db.execute(
        "INSERT INTO auctions (title, description, starts_at, ends_at,"
        " soft_close_minutes, status, created_at) VALUES (?, ?, ?, ?, 5, 'active', ?)",
        ("Grand Opening Auction",
         "Welcome to K&K Auctions! Our grand opening timed auction features quality "
         "used vehicles, engines, transmissions, catalytic converters and rims — "
         "all sold to the highest bidder. Pickup in the Greater Toronto Area. "
         "Call/text " + BUSINESS_PHONE + " with any questions.",
         starts.isoformat(), ends.isoformat(), now),
    )
    auction_id = cur.lastrowid

    sample_lots = [
        (1, "2012 Honda Civic LX Sedan — Runs & Drives",
         "Clean-title 2012 Honda Civic LX, automatic, ~185,000 km. Starts, runs and "
         "drives. Some cosmetic wear consistent with age. Sold as-is, where-is. "
         "Great parts car or budget daily driver with a little TLC.",
         "cars", 50000, 2500),
        (2, "Lot of 5 OEM Catalytic Converters",
         "Mixed lot of five original-equipment catalytic converters removed from "
         "scrapped vehicles (Honda, Toyota, Ford applications). Sold as a single lot, "
         "as-is. Buyer responsible for any environmental handling requirements.",
         "catalytic converters", 20000, 1000),
        (3, "4.6L V8 Engine — Ford F-150 (Used, Turns Over)",
         "Used 4.6L Triton V8 pulled from a 2008 Ford F-150. Engine turns over by "
         "hand; sold as-is for rebuild or parts. Pickup only — bring help, it's heavy.",
         "engines", 30000, 1500),
        (4, "Set of 4 — DAI Barrett 17x8 Gloss Black Rims (New in Box)",
         "Brand new in box DAI Barrett gloss black rims. 17x8, 8x165.1 bolt pattern, "
         "+20 offset. Fits heavy duty trucks and full-size SUVs. Retail over $900.",
         "rims", 58500, 2000),
        (5, "4-Speed Automatic Transmission — Chevrolet Silverado",
         "Used 4L60E automatic transmission from a 2010 Chevrolet Silverado 1500. "
         "Was driving when removed. Sold as-is, where-is.",
         "transmissions", 25000, 1000),
        (6, "Scrap Engine Lot — 3 Aluminum Blocks",
         "Lot of three aluminum engine blocks for scrap/recycling value. Various "
         "4-cylinder applications. Sold as one lot, as-is.",
         "engines", 10000, 500),
    ]
    for num, title, desc, cat, start_cents, incr_cents in sample_lots:
        db.execute(
            "INSERT INTO lots (auction_id, lot_number, title, description, category,"
            " starting_bid_cents, bid_increment_cents, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (auction_id, num, title, desc, cat, start_cents, incr_cents, now),
        )
    db.commit()
    print("\n" + "=" * 70)
    print("  K&K Auctions seeded.")
    print("  ADMIN LOGIN:  admin@kkauctions.local / changeme123")
    print("  >>> CHANGE THIS PASSWORD IMMEDIATELY AFTER FIRST LOGIN <<<")
    print("=" * 70 + "\n")


# ------------------------------------------------------------ auth helpers
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in or register to continue.", "warn")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or not user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_common():
    return {"current_user": current_user(), "business_phone": BUSINESS_PHONE}


def cents_to_dollars(cents):
    if cents is None:
        return None
    return f"${cents / 100:,.2f}"


app.jinja_env.filters["money"] = cents_to_dollars


def auction_time_state(auction):
    """Return 'upcoming', 'live', or 'ended' for an auction row."""
    now = utcnow()
    try:
        starts = datetime.fromisoformat(auction["starts_at"])
        ends = datetime.fromisoformat(auction["ends_at"])
    except (ValueError, TypeError):
        return "ended"
    if auction["status"] != "active":
        return "ended" if auction["status"] == "closed" else auction["status"]
    if now < starts:
        return "upcoming"
    if now > ends:
        return "ended"
    return "live"


# ------------------------------------------------------------ public pages
@app.route("/")
def index():
    db = get_db()
    auctions = db.execute(
        "SELECT * FROM auctions ORDER BY ends_at DESC"
    ).fetchall()
    live, upcoming = [], []
    for a in auctions:
        state = auction_time_state(a)
        if state == "live":
            live.append(a)
        elif state == "upcoming":
            upcoming.append(a)
    featured = []
    if live:
        ids = ",".join("?" for _ in live)
        featured = db.execute(
            f"SELECT l.*, a.title AS auction_title, a.ends_at FROM lots l"
            f" JOIN auctions a ON a.id = l.auction_id"
            f" WHERE l.auction_id IN ({ids}) AND l.status = 'active'"
            " ORDER BY l.current_bid_cents DESC NULLS LAST, l.id LIMIT 8",
            [a["id"] for a in live],
        ).fetchall()
    return render_template("index.html", live=live, upcoming=upcoming,
                           featured=featured, lot_image=lot_image,
                           time_state=auction_time_state)


@app.route("/auction/<int:auction_id>")
def auction_detail(auction_id):
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id = ?",
                         (auction_id,)).fetchone()
    if not auction:
        abort(404)
    category = request.args.get("category", "").strip()
    q = request.args.get("q", "").strip()
    sql = ("SELECT l.*, (SELECT COUNT(*) FROM bids b WHERE b.lot_id = l.id) AS bid_count"
           " FROM lots l WHERE l.auction_id = ?")
    params = [auction_id]
    if category:
        sql += " AND l.category = ?"
        params.append(category)
    if q:
        sql += " AND (l.title LIKE ? OR l.description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY l.lot_number"
    lots = db.execute(sql, params).fetchall()
    categories = db.execute(
        "SELECT DISTINCT category FROM lots WHERE auction_id = ? AND category IS NOT NULL"
        " ORDER BY category", (auction_id,)).fetchall()
    watched = set()
    user = current_user()
    if user:
        watched = {r["lot_id"] for r in db.execute(
            "SELECT lot_id FROM watchlist WHERE user_id = ?", (user["id"],))}
    return render_template("auction.html", auction=auction, lots=lots,
                           categories=[c["category"] for c in categories],
                           category=category, q=q, watched=watched,
                           lot_image=lot_image,
                           state=auction_time_state(auction))


@app.route("/lot/<int:lot_id>")
def lot_detail(lot_id):
    db = get_db()
    lot = db.execute(
        "SELECT l.*, a.title AS auction_title, a.ends_at, a.starts_at,"
        " a.status AS auction_status, a.soft_close_minutes"
        " FROM lots l JOIN auctions a ON a.id = l.auction_id"
        " WHERE l.id = ?", (lot_id,)).fetchone()
    if not lot:
        abort(404)
    bids = db.execute(
        "SELECT b.*, u.name AS bidder_name FROM bids b JOIN users u ON u.id = b.user_id"
        " WHERE b.lot_id = ? ORDER BY b.amount_cents DESC, b.id DESC",
        (lot_id,)).fetchall()
    bid_count = len(bids)
    user = current_user()
    watched = False
    user_bid = None
    if user:
        watched = db.execute(
            "SELECT 1 FROM watchlist WHERE user_id = ? AND lot_id = ?",
            (user["id"], lot_id)).fetchone() is not None
        row = db.execute(
            "SELECT MAX(amount_cents) AS m FROM bids WHERE lot_id = ? AND user_id = ?",
            (lot_id, user["id"])).fetchone()
        user_bid = row["m"]
    min_bid = lot["current_bid_cents"] + lot["bid_increment_cents"] \
        if lot["current_bid_cents"] else lot["starting_bid_cents"]
    state = auction_time_state(
        {"starts_at": lot["starts_at"], "ends_at": lot["ends_at"],
         "status": lot["auction_status"]})
    can_bid = bool(user) and state == "live" and lot["status"] == "active"
    return render_template(
        "lot.html", lot=lot, images=lot_images(lot_id, lot["title"], lot["category"]),
        bids=bids, bid_count=bid_count, watched=watched, min_bid=min_bid,
        state=state, can_bid=can_bid, user_bid=user_bid, user=user)


@app.route("/lot/<int:lot_id>/bid", methods=["POST"])
@login_required
def place_bid(lot_id):
    db = get_db()
    user = current_user()
    lot = db.execute(
        "SELECT l.*, a.starts_at, a.ends_at, a.status AS auction_status,"
        " a.soft_close_minutes FROM lots l JOIN auctions a ON a.id = l.auction_id"
        " WHERE l.id = ?", (lot_id,)).fetchone()
    if not lot:
        abort(404)

    # --- server-side validation (never trust the client) ---
    state = auction_time_state(
        {"starts_at": lot["starts_at"], "ends_at": lot["ends_at"],
         "status": lot["auction_status"]})
    if state != "live" or lot["status"] != "active":
        flash("Bidding is closed for this lot.", "error")
        return redirect(url_for("lot_detail", lot_id=lot_id))

    try:
        amount_dollars = float(request.form.get("amount", "0"))
    except (ValueError, TypeError):
        amount_dollars = 0
    amount_cents = int(round(amount_dollars * 100))
    min_bid = lot["current_bid_cents"] + lot["bid_increment_cents"] \
        if lot["current_bid_cents"] else lot["starting_bid_cents"]
    if amount_cents < min_bid:
        flash(f"Your bid must be at least {cents_to_dollars(min_bid)}.", "error")
        return redirect(url_for("lot_detail", lot_id=lot_id))

    now = utcnow()
    # --- soft close: extend the auction if bid lands inside the window ---
    ends = datetime.fromisoformat(lot["ends_at"])
    new_ends_iso = lot["ends_at"]
    extended = False
    window = timedelta(minutes=lot["soft_close_minutes"] or 5)
    if ends - now <= window:
        new_ends = ends + window
        new_ends_iso = new_ends.isoformat()
        extended = True

    db.execute(
        "INSERT INTO bids (lot_id, user_id, amount_cents, created_at)"
        " VALUES (?, ?, ?, ?)", (lot_id, user["id"], amount_cents, utcnow_iso()))
    db.execute(
        "UPDATE lots SET current_bid_cents = ?, current_bidder_id = ? WHERE id = ?",
        (amount_cents, user["id"], lot_id))
    if extended:
        db.execute("UPDATE auctions SET ends_at = ? WHERE id = ?",
                   (new_ends_iso, lot["auction_id"]))
    db.commit()
    if extended:
        flash(f"Bid placed at {cents_to_dollars(amount_cents)}! Soft close extended"
              f" the auction by {lot['soft_close_minutes']} minutes.", "ok")
    else:
        flash(f"Bid placed at {cents_to_dollars(amount_cents)} — you're the high bidder!",
              "ok")
    return redirect(url_for("lot_detail", lot_id=lot_id))


@app.route("/lot/<int:lot_id>/watch", methods=["POST"])
@login_required
def toggle_watch(lot_id):
    db = get_db()
    user = current_user()
    exists = db.execute(
        "SELECT 1 FROM watchlist WHERE user_id = ? AND lot_id = ?",
        (user["id"], lot_id)).fetchone()
    if exists:
        db.execute("DELETE FROM watchlist WHERE user_id = ? AND lot_id = ?",
                   (user["id"], lot_id))
        flash("Removed from your watchlist.", "ok")
    else:
        db.execute(
            "INSERT INTO watchlist (user_id, lot_id, created_at) VALUES (?, ?, ?)",
            (user["id"], lot_id, utcnow_iso()))
        flash("Added to your watchlist.", "ok")
    db.commit()
    return redirect(request.form.get("next") or url_for("lot_detail", lot_id=lot_id))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ------------------------------------------------------------------ auth
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        errors = []
        if len(name) < 2:
            errors.append("Please enter your name.")
        if "@" not in email or "." not in email:
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE email = ?",
                      (email,)).fetchone():
            errors.append("That email is already registered — try logging in.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db.execute(
                "INSERT INTO users (name, email, phone, password_hash, is_admin, created_at)"
                " VALUES (?, ?, ?, ?, 0, ?)",
                (name, email, phone or None, generate_password_hash(password),
                 utcnow_iso()))
            db.commit()
            user = db.execute("SELECT * FROM users WHERE email = ?",
                             (email,)).fetchone()
            session["user_id"] = user["id"]
            flash(f"Welcome, {name}! Your account is ready — happy bidding.", "ok")
            return redirect(request.args.get("next") or url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?",
                                (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['name']}!", "ok")
            return redirect(request.args.get("next") or url_for("index"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    flash("You've been logged out.", "ok")
    return redirect(url_for("index"))


# ---------------------------------------------------------------- account
@app.route("/account")
@login_required
def account():
    db = get_db()
    user = current_user()
    my_bids = db.execute(
        """SELECT b.amount_cents, b.created_at, l.id AS lot_id, l.title,
                  l.lot_number, l.current_bid_cents, l.current_bidder_id,
                  l.status AS lot_status, a.title AS auction_title, a.ends_at,
                  a.status AS auction_status
           FROM bids b JOIN lots l ON l.id = b.lot_id
           JOIN auctions a ON a.id = l.auction_id
           WHERE b.user_id = ?
           ORDER BY b.created_at DESC""", (user["id"],)).fetchall()
    # highest bid per lot for status display
    best = {}
    for b in my_bids:
        if b["lot_id"] not in best or b["amount_cents"] > best[b["lot_id"]]["amount_cents"]:
            best[b["lot_id"]] = b
    watchlist = db.execute(
        """SELECT l.*, a.title AS auction_title, a.ends_at,
                  (SELECT COUNT(*) FROM bids WHERE lot_id = l.id) AS bid_count
           FROM watchlist w JOIN lots l ON l.id = w.lot_id
           JOIN auctions a ON a.id = l.auction_id
           WHERE w.user_id = ? ORDER BY w.created_at DESC""", (user["id"],)).fetchall()
    return render_template("account.html", my_bids=list(best.values()),
                           watchlist=watchlist, lot_image=lot_image)


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(user["password_hash"], current):
            flash("Your current password is incorrect.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != confirm:
            flash("New passwords do not match.", "error")
        else:
            get_db().execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(new), user["id"]))
            get_db().commit()
            flash("Password changed successfully.", "ok")
            return redirect(url_for("account"))
    return render_template("account_password.html")


# ------------------------------------------------------------------ admin
@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    auctions = db.execute(
        """SELECT a.*,
                  (SELECT COUNT(*) FROM lots WHERE auction_id = a.id) AS lot_count,
                  (SELECT COUNT(*) FROM bids b JOIN lots l ON l.id = b.lot_id
                    WHERE l.auction_id = a.id) AS bid_count,
                  (SELECT COALESCE(SUM(current_bid_cents), 0) FROM lots
                    WHERE auction_id = a.id AND status = 'sold') AS revenue_cents
           FROM auctions a ORDER BY a.ends_at DESC""").fetchall()
    return render_template("admin/dashboard.html", auctions=auctions,
                           time_state=auction_time_state)


def _parse_dt(value):
    """Parse an HTML datetime-local value as UTC. Returns ISO string or None."""
    if not value:
        return None
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _dt_local(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return ""


@app.route("/admin/auction/new", methods=["GET", "POST"])
@admin_required
def admin_auction_new():
    return _auction_form(None)


@app.route("/admin/auction/<int:auction_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_auction_edit(auction_id):
    auction = get_db().execute("SELECT * FROM auctions WHERE id = ?",
                               (auction_id,)).fetchone()
    if not auction:
        abort(404)
    return _auction_form(auction)


def _auction_form(auction):
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        starts_at = _parse_dt(request.form.get("starts_at", ""))
        ends_at = _parse_dt(request.form.get("ends_at", ""))
        try:
            soft = max(0, int(request.form.get("soft_close_minutes", 5)))
        except ValueError:
            soft = 5
        status = request.form.get("status", "draft")
        if status not in ("draft", "active", "closed"):
            status = "draft"
        errors = []
        if not title:
            errors.append("Title is required.")
        if not starts_at or not ends_at:
            errors.append("Valid start and end dates are required.")
        elif starts_at >= ends_at:
            errors.append("End time must be after start time.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            if auction:
                db.execute(
                    "UPDATE auctions SET title=?, description=?, starts_at=?,"
                    " ends_at=?, soft_close_minutes=?, status=? WHERE id=?",
                    (title, description, starts_at, ends_at, soft, status,
                     auction["id"]))
                flash("Auction updated.", "ok")
            else:
                cur = db.execute(
                    "INSERT INTO auctions (title, description, starts_at, ends_at,"
                    " soft_close_minutes, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, description, starts_at, ends_at, soft, status,
                     utcnow_iso()))
                auction_id = cur.lastrowid
                flash("Auction created.", "ok")
                db.commit()
                return redirect(url_for("admin_auction_edit", auction_id=auction_id))
            db.commit()
            return redirect(url_for("admin_dashboard"))
    form = {
        "title": auction["title"] if auction else "",
        "description": auction["description"] if auction else "",
        "starts_at": _dt_local(auction["starts_at"]) if auction else "",
        "ends_at": _dt_local(auction["ends_at"]) if auction else "",
        "soft_close_minutes": auction["soft_close_minutes"] if auction else 5,
        "status": auction["status"] if auction else "draft",
    }
    lots = []
    if auction:
        lots = db.execute(
            """SELECT l.*, (SELECT COUNT(*) FROM bids b WHERE b.lot_id = l.id) AS bid_count
               FROM lots l WHERE l.auction_id = ? ORDER BY l.lot_number""",
            (auction["id"],)).fetchall()
    return render_template("admin/auction_form.html", auction=auction, form=form,
                           lots=lots)


@app.route("/admin/auction/<int:auction_id>/close", methods=["POST"])
@admin_required
def admin_auction_close(auction_id):
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id = ?",
                         (auction_id,)).fetchone()
    if not auction:
        abort(404)
    lots = db.execute("SELECT * FROM lots WHERE auction_id = ?",
                      (auction_id,)).fetchall()
    for lot in lots:
        if lot["status"] != "active":
            continue
        new_status = "sold" if lot["current_bid_cents"] else "no_sale"
        db.execute("UPDATE lots SET status = ? WHERE id = ?",
                   (new_status, lot["id"]))
    db.execute("UPDATE auctions SET status = 'closed' WHERE id = ?", (auction_id,))
    db.commit()
    flash(f"Auction closed: {sum(1 for l in lots if l['current_bid_cents'])} lot(s) sold.",
          "ok")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/auction/<int:auction_id>/winners")
@admin_required
def admin_winners(auction_id):
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id = ?",
                         (auction_id,)).fetchone()
    if not auction:
        abort(404)
    lots = db.execute(
        """SELECT l.*, u.name AS winner_name, u.email AS winner_email,
                  u.phone AS winner_phone
           FROM lots l LEFT JOIN users u ON u.id = l.current_bidder_id
           WHERE l.auction_id = ? ORDER BY l.lot_number""", (auction_id,)).fetchall()
    return render_template("admin/winners.html", auction=auction, lots=lots)


# ------------------------------------------------------------ admin: lots
def _save_uploads(files):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            continue
        name = f"{utcnow().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(f.filename)}"
        f.save(os.path.join(UPLOAD_DIR, name))
        saved.append(name)
    return saved


@app.route("/admin/auction/<int:auction_id>/lots/new", methods=["GET", "POST"])
@admin_required
def admin_lot_new(auction_id):
    auction = get_db().execute("SELECT * FROM auctions WHERE id = ?",
                               (auction_id,)).fetchone()
    if not auction:
        abort(404)
    return _lot_form(auction, None)


@app.route("/admin/lot/<int:lot_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_lot_edit(lot_id):
    db = get_db()
    lot = db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if not lot:
        abort(404)
    auction = db.execute("SELECT * FROM auctions WHERE id = ?",
                         (lot["auction_id"],)).fetchone()
    return _lot_form(auction, lot)


def _lot_form(auction, lot):
    db = get_db()
    if request.method == "POST":
        try:
            lot_number = int(request.form.get("lot_number", 0))
        except ValueError:
            lot_number = 0
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip().lower() or None
        try:
            start_cents = int(round(float(request.form.get("starting_bid", "0")) * 100))
            incr_cents = int(round(float(request.form.get("bid_increment", "1")) * 100))
        except ValueError:
            start_cents, incr_cents = 0, 100
        status = request.form.get("status", "active")
        if status not in ("active", "sold", "no_sale"):
            status = "active"
        errors = []
        if lot_number < 1:
            errors.append("Lot number must be 1 or higher.")
        if not title:
            errors.append("Title is required.")
        if start_cents < 0 or incr_cents < 1:
            errors.append("Starting bid must be >= 0 and increment >= $0.01.")
        dup = db.execute(
            "SELECT id FROM lots WHERE auction_id = ? AND lot_number = ?"
            + (" AND id != ?" if lot else ""),
            ([auction["id"], lot_number] + ([lot["id"]] if lot else []))).fetchone()
        if dup:
            errors.append(f"Lot number {lot_number} already exists in this auction.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            if lot:
                db.execute(
                    "UPDATE lots SET lot_number=?, title=?, description=?, category=?,"
                    " starting_bid_cents=?, bid_increment_cents=?, status=? WHERE id=?",
                    (lot_number, title, description, category, start_cents,
                     incr_cents, status, lot["id"]))
                lot_id = lot["id"]
                flash("Lot updated.", "ok")
            else:
                cur = db.execute(
                    "INSERT INTO lots (auction_id, lot_number, title, description, category,"
                    " starting_bid_cents, bid_increment_cents, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                    (auction["id"], lot_number, title, description, category,
                     start_cents, incr_cents, utcnow_iso()))
                lot_id = cur.lastrowid
                flash("Lot created.", "ok")
            for name in _save_uploads(request.files.getlist("photos")):
                db.execute(
                    "INSERT INTO lot_photos (lot_id, filename, created_at)"
                    " VALUES (?, ?, ?)", (lot_id, name, utcnow_iso()))
            # photo deletion
            for pid in request.form.getlist("delete_photo"):
                row = db.execute(
                    "SELECT filename FROM lot_photos WHERE id = ? AND lot_id = ?",
                    (pid, lot_id)).fetchone()
                if row:
                    try:
                        os.remove(os.path.join(UPLOAD_DIR, row["filename"]))
                    except OSError:
                        pass
                    db.execute("DELETE FROM lot_photos WHERE id = ?", (pid,))
            db.commit()
            return redirect(url_for("admin_auction_edit", auction_id=auction["id"]))
    if lot:
        photos = db.execute("SELECT * FROM lot_photos WHERE lot_id = ? ORDER BY id",
                            (lot["id"],)).fetchall()
        form = {
            "lot_number": lot["lot_number"], "title": lot["title"],
            "description": lot["description"] or "", "category": lot["category"] or "",
            "starting_bid": f"{lot['starting_bid_cents'] / 100:.2f}",
            "bid_increment": f"{lot['bid_increment_cents'] / 100:.2f}",
            "status": lot["status"],
        }
    else:
        existing = db.execute(
            "SELECT COUNT(*) AS c FROM lots WHERE auction_id = ?", (auction["id"],)
        ).fetchone()["c"]
        photos = []
        form = {"lot_number": existing + 1, "title": "", "description": "",
                "category": "", "starting_bid": "1.00", "bid_increment": "5.00",
                "status": "active"}
    return render_template("admin/lot_form.html", auction=auction, lot=lot,
                           form=form, photos=photos)


@app.route("/admin/lot/<int:lot_id>/delete", methods=["POST"])
@admin_required
def admin_lot_delete(lot_id):
    db = get_db()
    lot = db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if not lot:
        abort(404)
    photos = db.execute("SELECT filename FROM lot_photos WHERE lot_id = ?",
                        (lot_id,)).fetchall()
    for p in photos:
        try:
            os.remove(os.path.join(UPLOAD_DIR, p["filename"]))
        except OSError:
            pass
    db.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
    db.commit()
    flash("Lot deleted.", "ok")
    return redirect(url_for("admin_auction_edit", auction_id=lot["auction_id"]))


@app.route("/admin/lot/<int:lot_id>/bids")
@admin_required
def admin_lot_bids(lot_id):
    db = get_db()
    lot = db.execute(
        "SELECT l.*, a.title AS auction_title FROM lots l"
        " JOIN auctions a ON a.id = l.auction_id WHERE l.id = ?",
        (lot_id,)).fetchone()
    if not lot:
        abort(404)
    bids = db.execute(
        "SELECT b.*, u.name AS bidder_name, u.email AS bidder_email,"
        " u.phone AS bidder_phone FROM bids b JOIN users u ON u.id = b.user_id"
        " WHERE b.lot_id = ? ORDER BY b.amount_cents DESC, b.id DESC",
        (lot_id,)).fetchall()
    return render_template("admin/lot_bids.html", lot=lot, bids=bids)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("403.html"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


# ------------------------------------------------------------------ entry
# Initialize the database at import time so `flask run` works too.
os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db()

if __name__ == "__main__":
    if app.secret_key == "dev-secret-change-me":
        print("WARNING: using default SECRET_KEY — set the SECRET_KEY env var!")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
