# K&K Auctions

A timed online auction website (HiBid-style) for **K&K Bin Cleanup** — buy/sell
unwanted cars, catalytic converters, engines, transmissions, rims across Ontario.

The public registers for free, browses auction events, and bids on lots.
Admins create auctions/lots, watch bidding live, close auctions, and pull winner
reports. Bidding supports **soft close**: a bid in the final minutes extends the
auction so everyone gets a fair shot.

## Stack

- Python 3 + Flask (only dependencies: Flask, gunicorn)
- SQLite via stdlib `sqlite3` — no ORM
- Jinja2 templates (ships with Flask), vanilla CSS + JS — no build step
- Passwords hashed with `werkzeug.security`; sessions via Flask signed cookies
- Photo uploads stored in `uploads/` and served by the app

## Local setup

```bash
cd kk-auctions
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# First run seeds the DB: admin user + a sample "Grand Opening Auction"
python app.py
# or: flask run
```

Open http://127.0.0.1:5000

On first run you'll see a console warning with the seeded admin login:

- Email: `admin@kkauctions.local`
- Password: `changeme123`

## Change the admin password

There's no "change password" UI — do it once via the Python shell:

```bash
python3 - <<'EOF'
import sqlite3
from werkzeug.security import generate_password_hash
db = sqlite3.connect("kk_auctions.db")
db.execute("UPDATE users SET password_hash = ? WHERE email = ?",
           (generate_password_hash("YOUR-NEW-STRONG-PASSWORD"), "admin@kkauctions.local"))
db.commit()
EOF
```

(Install Flask in the environment first so `werkzeug` is importable.)

## Adding auctions & lots

1. Log in as the admin, open **Admin**.
2. **+ New auction** — title, description, start/end (UTC), soft-close minutes, status.
   - `draft` = hidden from the public list; `active` = visible/biddable in its window;
     `closed` = ended.
3. Open the auction → **+ Add lot** — lot number, title, description, category,
   photos (multiple upload allowed), starting bid, bid increment.
4. While live, **Bids** shows per-lot bid history with bidder contact info.
5. **Close** ends the auction: lots with bids → `sold` to the high bidder,
   lots without bids → `no_sale`.
6. **Winners** gives a per-auction winner report (lot, winner name/email/phone,
   winning bid) with a print button.

Categories are free text; the lot placeholder art recognizes:
`cars`, `catalytic converters`, `engines`, `transmissions`, `rims`,
`equipment`, `parts`.

## Project layout

```
app.py               # the whole app: routes, bidding logic, DB, seed data
templates/           # Jinja2 pages (base, index, auction, lot, auth, account, admin/*)
static/              # style.css, app.js (countdown timers)
uploads/             # user-uploaded lot photos (created at runtime)
kk_auctions.db       # SQLite database (created at runtime)
requirements.txt
README.md
```

## Deployment

Works anywhere Python runs. General options:

- **Render.com / Railway**: new Web Service from this repo, build `pip install -r requirements.txt`,
  start `gunicorn app:app`. Add a persistent disk mounted at the project dir so
  `kk_auctions.db` and `uploads/` survive deploys/restarts.
- **PythonAnywhere**: upload the project, create a virtualenv, install
  requirements, point a Flask web app at `app.py` (`app` object).
- **Any VPS**: `pip install -r requirements.txt`, run
  `gunicorn -w 3 -b 0.0.0.0:8000 app:app` behind nginx/Caddy.

**Production checklist:**

- Set `SECRET_KEY` env var to a long random value (sessions break otherwise).
- `uploads/` and `kk_auctions.db` need **persistent storage** — on ephemeral
  filesystems use a mounted volume/disk.
- Change the seeded admin password immediately.
- Serve behind HTTPS (Render/Railway/Caddy do this automatically).
- Back up `kk_auctions.db` regularly.

## Notes

- All times are stored and compared in UTC; the UI labels them as such.
- Bid minimums, auction windows, and soft-close extensions are enforced
  server-side — client input is never trusted.
- Email notifications are not built in; winners are contacted via the phone/email
  on the winner report.
