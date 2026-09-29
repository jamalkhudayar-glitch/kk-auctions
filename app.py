#!/usr/bin/env python3
"""
K&K Auctions — timed online auction site for K&K Bin Cleanup.
Stack: Flask + sqlite3 (stdlib) + Werkzeug. No ORM, no build step.
"""
import os
import csv
import io
import re
import shutil
import sqlite3
import tempfile
import zipfile
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask, g, render_template, request, redirect, url_for,
    flash, session, abort, send_from_directory,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:
    _PIL_OK = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KK_DB", os.path.join(BASE_DIR, "kk_auctions.db"))
UPLOAD_DIR = os.environ.get("KK_UPLOADS", os.path.join(BASE_DIR, "uploads"))
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB uploads

BUSINESS_PHONE = "6476793420"
BUSINESS_PHONE2 = "6478343544"

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")


# ------------------------------------------------------- Stripe payments
class StripeError(Exception):
    pass


def stripe_configured():
    return bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY)


def stripe_request(method, path, params=None):
    """Minimal Stripe API client (form-encoded, stdlib only). Raises StripeError."""
    if not STRIPE_SECRET_KEY:
        raise StripeError("Stripe is not configured on this server.")
    data = None
    headers = {"Stripe-Version": "2024-06-20"}
    if params is not None:
        data = urllib.parse.urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request("https://api.stripe.com" + path, data=data,
                                 headers=headers, method=method)
    req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:200]
        raise StripeError(msg)
    except Exception as e:
        raise StripeError(f"Could not reach Stripe: {e}")


def get_or_create_stripe_customer(db, user):
    """Return the Stripe customer id for a user, creating one if needed."""
    if user["stripe_customer_id"]:
        return user["stripe_customer_id"]
    customer = stripe_request("POST", "/v1/customers",
                              {"email": user["email"], "name": user["name"],
                               "phone": user["phone"] or "",
                               "metadata[user_id]": str(user["id"])})
    db.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?",
               (customer["id"], user["id"]))
    db.commit()
    return customer["id"]


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
    buyers_premium_pct REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    auction_id INTEGER NOT NULL REFERENCES auctions(id),
    lot_ids TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    premium_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'cad',
    stripe_payment_intent_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bids_lot ON bids(lot_id);
CREATE INDEX IF NOT EXISTS idx_lots_auction ON lots(auction_id);
CREATE INDEX IF NOT EXISTS idx_payments_auction ON payments(auction_id);
"""


def migrate_db(db):
    """Add newer columns to existing databases (fresh installs get them via SCHEMA)."""
    cols = {r["name"] for r in
            db.execute("PRAGMA table_info(users)").fetchall()}
    for name, ddl in (
        ("stripe_customer_id", "TEXT"),
        ("stripe_pm_id", "TEXT"),
        ("card_brand", "TEXT"),
        ("card_last4", "TEXT"),
    ):
        if name not in cols:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    acols = {r["name"] for r in
             db.execute("PRAGMA table_info(auctions)").fetchall()}
    if "buyers_premium_pct" not in acols:
        db.execute("ALTER TABLE auctions ADD COLUMN buyers_premium_pct REAL NOT NULL DEFAULT 0")
    pcols = {r["name"] for r in
             db.execute("PRAGMA table_info(payments)").fetchall()}
    if "premium_cents" not in pcols:
        db.execute("ALTER TABLE payments ADD COLUMN premium_cents INTEGER NOT NULL DEFAULT 0")
    if "method" not in pcols:
        db.execute("ALTER TABLE payments ADD COLUMN method TEXT NOT NULL DEFAULT 'card'")
    db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    db.commit()
    migrate_db(db)
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
    return {"current_user": current_user(), "business_phone": BUSINESS_PHONE,
            "business_phone2": BUSINESS_PHONE2}


@app.template_filter("hibid_dt")
def hibid_dt(iso):
    """Display-only: 'Saturday October 3rd @ 8:00am' like Bryan's HiBid listings."""
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return iso
    d = dt.day
    suf = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    ampm = "am" if dt.hour < 12 else "pm"
    h = dt.hour % 12 or 12
    return f"{dt:%A} {dt:%B} {d}{suf} @ {h}:{dt:%M}{ampm}"


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

    # HiBid-style: winners are charged automatically, so a card on file is required
    if stripe_configured() and not user["stripe_pm_id"]:
        flash("Add your card on file before bidding — auction winners are"
              " charged automatically.", "warn")
        return redirect(url_for("payment_method",
                                next=url_for("lot_detail", lot_id=lot_id)))

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


@app.route("/healthz")
def healthz():
    return {"ok": True, "version": "2026.09.28-photo-fix-2", "pil": _PIL_OK}


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/terms")
def terms():
    return render_template("terms.html")


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
        if not request.form.get("agree_terms"):
            errors.append("You must agree to the Terms & Conditions to register.")
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
            if stripe_configured():
                try:
                    get_or_create_stripe_customer(db, user)
                except StripeError as e:
                    flash(f"Account created, but card setup hit a snag: {e}."
                          " You can add your card from My Account.", "warn")
                    return redirect(request.args.get("next") or url_for("index"))
                flash(f"Welcome, {name}! One last step: add your card on file"
                      " to activate bidding.", "ok")
                return redirect(url_for("payment_method",
                                        next=request.args.get("next") or url_for("index")))
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


# ------------------------------------------------- card on file (Stripe)
@app.route("/account/payment-method")
@login_required
def payment_method():
    if not stripe_configured():
        flash("Card payments are not enabled yet — please check back soon.",
              "warn")
        return redirect(url_for("account"))
    user = current_user()
    return render_template(
        "account/payment_method.html",
        publishable_key=STRIPE_PUBLISHABLE_KEY,
        card_brand=user["card_brand"], card_last4=user["card_last4"],
        next_url=request.args.get("next") or url_for("account"))


@app.route("/account/payment-method/intent")
@login_required
def payment_method_intent():
    """Create a SetupIntent so the browser can collect card details securely."""
    if not stripe_configured():
        return {"error": "Card payments are not enabled."}, 503
    db = get_db()
    user = current_user()
    try:
        customer_id = get_or_create_stripe_customer(db, user)
        intent = stripe_request(
            "POST", "/v1/setup_intents",
            {"customer": customer_id, "usage": "off_session",
             "payment_method_types[]": "card"})
    except StripeError as e:
        return {"error": str(e)}, 502
    return {"client_secret": intent["client_secret"]}


@app.route("/account/payment-method/confirm", methods=["POST"])
@login_required
def payment_method_confirm():
    """Save the card Stripe.js just tokenized as the user's card on file."""
    if not stripe_configured():
        return {"error": "Card payments are not enabled."}, 503
    db = get_db()
    user = current_user()
    pm_id = (request.get_json(silent=True) or {}).get("payment_method_id", "")
    if not pm_id.startswith("pm_"):
        return {"error": "Missing payment method."}, 400
    try:
        customer_id = get_or_create_stripe_customer(db, user)
        stripe_request("POST", f"/v1/payment_methods/{pm_id}/attach",
                       {"customer": customer_id})
        stripe_request("POST", f"/v1/customers/{customer_id}",
                       {"invoice_settings[default_payment_method]": pm_id})
        pm = stripe_request("GET", f"/v1/payment_methods/{pm_id}")
        card = pm.get("card", {})
        db.execute(
            "UPDATE users SET stripe_pm_id = ?, card_brand = ?, card_last4 = ?"
            " WHERE id = ?",
            (pm_id, card.get("brand"), card.get("last4"), user["id"]))
        db.commit()
    except StripeError as e:
        return {"error": str(e)}, 502
    return {"ok": True, "brand": card.get("brand"), "last4": card.get("last4")}


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
        try:
            premium = max(0.0, min(100.0, float(request.form.get("buyers_premium_pct") or 9)))
        except ValueError:
            premium = 0.0
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
                    " ends_at=?, soft_close_minutes=?, status=?,"
                    " buyers_premium_pct=? WHERE id=?",
                    (title, description, starts_at, ends_at, soft, status,
                     premium, auction["id"]))
                flash("Auction updated.", "ok")
            else:
                cur = db.execute(
                    "INSERT INTO auctions (title, description, starts_at, ends_at,"
                    " soft_close_minutes, status, buyers_premium_pct, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, description, starts_at, ends_at, soft, status,
                     premium, utcnow_iso()))
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
        "buyers_premium_pct": auction["buyers_premium_pct"] if auction and "buyers_premium_pct" in auction.keys() else 9,
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
    sold = []
    for lot in lots:
        if lot["status"] != "active":
            continue
        new_status = "sold" if lot["current_bid_cents"] else "no_sale"
        db.execute("UPDATE lots SET status = ? WHERE id = ?",
                   (new_status, lot["id"]))
        if new_status == "sold":
            sold.append(lot)
    db.execute("UPDATE auctions SET status = 'closed' WHERE id = ?", (auction_id,))
    db.commit()

    # --- automatic winner charging (HiBid-style) ---
    charged, failed = 0, 0
    if stripe_configured() and sold:
        by_winner = {}
        for lot in sold:
            by_winner.setdefault(lot["current_bidder_id"], []).append(lot)
        premium_pct = auction["buyers_premium_pct"] or 0
        for user_id, won_lots in by_winner.items():
            winner = db.execute("SELECT * FROM users WHERE id = ?",
                                (user_id,)).fetchone()
            subtotal = sum(l["current_bid_cents"] for l in won_lots)
            premium_cents = int(round(subtotal * premium_pct / 100)) if premium_pct else 0
            # Safety cap: the premium never exceeds 50% of the invoice (hammer total).
            max_premium = subtotal // 2
            if premium_cents > max_premium:
                premium_cents = max_premium
            total = subtotal + premium_cents
            lot_ids = ",".join(str(l["id"]) for l in won_lots)
            status, pi_id, err = "failed", None, None
            if winner and winner["stripe_customer_id"] and winner["stripe_pm_id"]:
                try:
                    desc = (f"K&K Auctions: {auction['title']} —"
                            f" {len(won_lots)} lot(s)")
                    if premium_cents:
                        desc += f" (incl. {premium_pct:g}% buyer's premium)"
                    pi = stripe_request(
                        "POST", "/v1/payment_intents",
                        {"amount": total, "currency": "cad",
                         "customer": winner["stripe_customer_id"],
                         "payment_method": winner["stripe_pm_id"],
                         "off_session": "true", "confirm": "true",
                         "receipt_email": winner["email"],
                         "description": desc,
                         "metadata[auction_id]": str(auction_id),
                         "metadata[user_id]": str(user_id)})
                    pi_id = pi["id"]
                    status = pi["status"]  # "succeeded" when captured
                    if status == "succeeded":
                        charged += 1
                    else:
                        failed += 1
                        err = f"Payment status: {status}"
                except StripeError as e:
                    failed += 1
                    err = str(e)
            else:
                failed += 1
                err = "Winner has no card on file."
            db.execute(
                "INSERT INTO payments (user_id, auction_id, lot_ids, amount_cents,"
                " premium_cents, currency, stripe_payment_intent_id, status,"
                " error_message, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'cad', ?, ?, ?, ?)",
                (user_id, auction_id, lot_ids, total, premium_cents, pi_id,
                 status, err, utcnow_iso()))
            db.commit()

    msg = f"Auction closed: {len(sold)} lot(s) sold."
    if stripe_configured() and sold:
        msg += f" Auto-charged {charged} winner(s)."
        if failed:
            msg += f" {failed} payment(s) need attention — see Winners."
    flash(msg, "ok" if not failed else "warn")
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
    payments = {p["user_id"]: p for p in db.execute(
        "SELECT * FROM payments WHERE auction_id = ?", (auction_id,)).fetchall()}
    pay_summary = db.execute(
        """SELECT p.*, u.name AS winner_name FROM payments p
           JOIN users u ON u.id = p.user_id WHERE p.auction_id = ?
           ORDER BY p.amount_cents DESC""", (auction_id,)).fetchall()
    # Winners with sold lots and what they owe (for manual cash/e-transfer entry)
    pct = auction["buyers_premium_pct"] or 0
    owed_rows = db.execute(
        """SELECT u.id AS user_id, u.name AS winner_name,
                  SUM(l.current_bid_cents) AS subtotal
           FROM lots l JOIN users u ON u.id = l.current_bidder_id
           WHERE l.auction_id = ? AND l.status = 'sold'
           GROUP BY u.id, u.name ORDER BY u.name""", (auction_id,)).fetchall()
    paid_ids = {p["user_id"] for p in payments.values()}
    winners_owed = [
        {"user_id": r["user_id"], "winner_name": r["winner_name"],
         "total_cents": int(r["subtotal"] + round(r["subtotal"] * pct / 100))}
        for r in owed_rows if r["user_id"] not in paid_ids
    ]
    return render_template("admin/winners.html", auction=auction, lots=lots,
                           payments=payments, pay_summary=pay_summary,
                           winners_owed=winners_owed,
                           stripe_configured=stripe_configured())


@app.route("/admin/auction/<int:auction_id>/payment/record", methods=["POST"])
@admin_required
def admin_record_payment(auction_id):
    """Record a manual (cash / e-transfer) payment from a winner."""
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id = ?",
                         (auction_id,)).fetchone()
    if not auction:
        abort(404)
    user_id = request.form.get("user_id", type=int)
    method = request.form.get("method", "")
    amount_cents = request.form.get("amount_cents", type=int)
    if method not in ("cash", "etransfer", "bank"):
        flash("Choose cash, e-transfer or bank transfer.", "error")
        return redirect(url_for("admin_winners", auction_id=auction_id))
    winner_lots = db.execute(
        """SELECT id, current_bid_cents FROM lots
           WHERE auction_id = ? AND status = 'sold' AND current_bidder_id = ?""",
        (auction_id, user_id)).fetchall()
    if not winner_lots:
        flash("That bidder has no sold lots in this auction.", "error")
        return redirect(url_for("admin_winners", auction_id=auction_id))
    pct = auction["buyers_premium_pct"] or 0
    subtotal = sum(l["current_bid_cents"] for l in winner_lots)
    premium = int(round(subtotal * pct / 100))
    total = subtotal + premium
    if not amount_cents or amount_cents <= 0:
        amount_cents = total
    db.execute(
        """INSERT INTO payments (user_id, auction_id, lot_ids, amount_cents,
                                 premium_cents, currency, status, method, created_at)
           VALUES (?, ?, ?, ?, ?, 'cad', 'succeeded', ?, ?)""",
        (user_id, auction_id, ",".join(str(l["id"]) for l in winner_lots),
         amount_cents, premium, method, utcnow().isoformat()))
    db.commit()
    flash(f"Recorded {method} payment of "
          f"${amount_cents/100:,.2f}.", "success")
    return redirect(url_for("admin_winners", auction_id=auction_id))


# ------------------------------------------------------------ admin: lots
def _store_image(src_path, orig_filename):
    """Copy an image into UPLOAD_DIR, shrinking it. Returns stored name or None."""
    ext = orig_filename.rsplit(".", 1)[-1].lower() if "." in orig_filename else ""
    if ext not in ALLOWED_EXT:
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"{utcnow().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(orig_filename)}"
    dest = os.path.join(UPLOAD_DIR, name)
    shutil.copyfile(src_path, dest)
    # Shrink phone photos so uploads stay fast and small.
    if _PIL_OK and ext in {"jpg", "jpeg", "png", "webp"}:
        try:
            im = Image.open(dest)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1600, 1600))
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.save(dest, "JPEG", quality=82, optimize=True)
            base = dest.rsplit(".", 1)[0] + ".jpg"
            if base != dest:
                os.replace(dest, base)
                name = os.path.basename(base)
        except Exception:
            pass
    return name


def _save_uploads(files):
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
        try:
            f.save(tmp.name)
            tmp.close()
            name = _store_image(tmp.name, f.filename)
            if name:
                saved.append(name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    return saved


@app.route("/admin/lots/sample-csv")
@admin_required
def admin_sample_csv():
    """Downloadable CSV template for bulk lot import."""
    from flask import Response
    sample = ("lot_number,title,description,category,starting_bid,bid_increment\n"
              '1,2012 Honda Civic EX,"Sedan, 180k km, runs well",vehicles,500,25\n'
              '2,Catalytic converter - Honda,"OEM, code 5K12",converters,40,5\n')
    return Response(sample, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lots_template.csv"})


@app.route("/admin/auction/<int:auction_id>/lots/import", methods=["GET", "POST"])
@admin_required
def admin_lot_import(auction_id):
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
    if not auction:
        abort(404)
    if request.method == "POST":
        f = request.files.get("csv_file")
        if not f or not f.filename:
            flash("Choose a CSV file first.", "error")
            return redirect(url_for("admin_lot_import", auction_id=auction_id))
        try:
            text = f.read().decode("utf-8-sig")
        except Exception:
            flash("Could not read the file — make sure it is a CSV.", "error")
            return redirect(url_for("admin_lot_import", auction_id=auction_id))
        reader = csv.DictReader(io.StringIO(text))
        created, skipped, errors = 0, 0, []
        existing = {r["lot_number"] for r in db.execute(
            "SELECT lot_number FROM lots WHERE auction_id = ?", (auction_id,))}
        for i, row in enumerate(reader, start=2):
            try:
                lot_number = int((row.get("lot_number") or "0").strip())
            except ValueError:
                lot_number = 0
            title = (row.get("title") or "").strip()
            if lot_number < 1 or not title:
                errors.append(f"Row {i}: needs a lot_number (1+) and a title.")
                continue
            if lot_number in existing:
                skipped += 1
                continue
            description = (row.get("description") or "").strip()
            category = (row.get("category") or "").strip().lower() or None
            try:
                start_cents = int(round(float(row.get("starting_bid") or 0) * 100))
                incr_cents = int(round(float(row.get("bid_increment") or 5) * 100))
            except ValueError:
                errors.append(f"Row {i}: starting_bid / bid_increment must be numbers.")
                continue
            if start_cents < 0 or incr_cents < 1:
                errors.append(f"Row {i}: starting bid must be >= 0 and increment >= 0.01.")
                continue
            db.execute(
                "INSERT INTO lots (auction_id, lot_number, title, description, category,"
                " starting_bid_cents, bid_increment_cents, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (auction_id, lot_number, title, description, category,
                 start_cents, incr_cents, utcnow_iso()))
            existing.add(lot_number)
            created += 1
        db.commit()
        flash(f"Imported {created} lots." + (f" Skipped {skipped} duplicates." if skipped else ""), "ok")
        for e in errors[:5]:
            flash(e, "error")
        if len(errors) > 5:
            flash(f"...and {len(errors) - 5} more rows had problems.", "error")
        return redirect(url_for("admin_auction_edit", auction_id=auction_id))
    return render_template("admin/lot_import.html", auction=auction)


@app.route("/admin/auction/<int:auction_id>/lots/import-photos", methods=["POST"])
@admin_required
def admin_lot_import_photos(auction_id):
    """ZIP upload: photo filenames like 12_1.jpg attach to lot number 12."""
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
    if not auction:
        abort(404)
    f = request.files.get("zip_file")
    if not f or not f.filename:
        flash("Choose a ZIP file first.", "error")
        return redirect(url_for("admin_lot_import", auction_id=auction_id))
    lot_ids = {r["lot_number"]: r["id"] for r in db.execute(
        "SELECT id, lot_number FROM lots WHERE auction_id = ?", (auction_id,))}
    attached, skipped = 0, 0
    tmpdir = tempfile.mkdtemp(prefix="kkzip_")
    try:
        zpath = os.path.join(tmpdir, "upload.zip")
        f.save(zpath)
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                base = os.path.basename(info.filename)
                m = re.match(r"^(\d+)[-_].*\.(png|jpe?g|gif|webp)$", base, re.I)
                if not m:
                    skipped += 1
                    continue
                lot_number = int(m.group(1))
                lot_id = lot_ids.get(lot_number)
                if not lot_id:
                    skipped += 1
                    continue
                src_path = os.path.join(tmpdir, base)
                with z.open(info) as zi, open(src_path, "wb") as out:
                    shutil.copyfileobj(zi, out)
                name = _store_image(src_path, base)
                if name:
                    db.execute(
                        "INSERT INTO lot_photos (lot_id, filename, created_at)"
                        " VALUES (?, ?, ?)", (lot_id, name, utcnow_iso()))
                    attached += 1
                else:
                    skipped += 1
        db.commit()
    except zipfile.BadZipFile:
        flash("That file is not a valid ZIP.", "error")
        return redirect(url_for("admin_lot_import", auction_id=auction_id))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    flash(f"Attached {attached} photos." + (f" Skipped {skipped} files." if skipped else ""), "ok")
    return redirect(url_for("admin_auction_edit", auction_id=auction_id))


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
