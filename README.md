# Mebel Ombori — Furniture Workshop WMS

A mobile-first warehouse system for a furniture workshop (*mebel sexi*). Workers take
materials off the shelf from their phones; the administrator books arrivals in USD, sees
what the stock is worth and what has to be bought, and reads the monthly history.

- **Two inventories, kept apart:** whole materials (*Butun* — sheets, litres, metres,
  pieces) and leftover offcuts (*Qoldiq* — reusable pieces with length × width).
- **Role-based isolation:** a painter sees paints, a sawyer sees boards. Every list, form
  and API endpoint goes through one visibility layer.
- **Approval gate:** workers register themselves and pick their trades; nobody gets in
  until an administrator approves them (and can change their roles first).
- **Append-only ledger:** every movement is an `InventoryTransaction` that cannot be
  edited or deleted — enforced in the model, the queryset, the admin and (on PostgreSQL)
  by a database trigger.
- **USD only,** with weighted-average cost so the valuation stays right when prices move.

Stack: Python 3.12 · Django 5.2 · Django REST Framework · PostgreSQL 16 · server-rendered
templates with Alpine.js · Tailwind CSS v4 · WhiteNoise · gunicorn. The UI is in Uzbek
(Latin script).

---

## Quick start (local)

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env            # macOS/Linux: cp .env.example .env
#   set DJANGO_DEBUG=True and any DJANGO_SECRET_KEY for local work;
#   leave DATABASE_URL empty to use a local SQLite file, or point it at PostgreSQL

python manage.py migrate
python manage.py seed_demo        # optional: demo workshop with 6 months of history
python manage.py runserver
```

Open http://127.0.0.1:8000.

`seed_demo` creates an administrator **admin**, the workers **arrachi**, **roverchi**,
**kraskachi** and **sborkachi**, and a pending registration **yangi_ishchi** so the
approval queue has something in it. They all share the demo password defined as
`DEFAULT_DEMO_PASSWORD` in `apps/inventory/management/commands/seed_demo.py`; choose your own with
`seed_demo --password "..."`. The command refuses to run unless `DJANGO_DEBUG=True` (or
`--force`) and only loads into an empty database. Without demo data, create the first
administrator with `python manage.py createsuperuser`; superusers are approved and get the
ADMIN role automatically.

### PostgreSQL

Production uses PostgreSQL. Set `DATABASE_URL=postgres://USER:PASSWORD@HOST:5432/DBNAME`
(URL-encode special characters in the password) and run `migrate`. Migration
`inventory/0002` installs the trigger `inventory_ledger_append_only`, which rejects any
`UPDATE` or `DELETE` on the ledger table, even from raw SQL. On SQLite the trigger is
skipped and immutability is enforced by the ORM only.

### Docker

```bash
cp .env.example .env    # set DJANGO_SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

The image builds the CSS in a Node stage, installs Python 3.12 dependencies, runs
`collectstatic` (hashed, compressed files served by WhiteNoise) and starts gunicorn as a
non-root user. The compose file runs `migrate` on start and keeps PostgreSQL data in a
named volume.

### Front-end assets

The built stylesheet (`static/css/app.css`), Alpine.js and the DM Sans font files are
committed, so running the app needs no Node toolchain. Only when you change templates or
styles:

```bash
npm install
npm run build:css      # or: npm run watch:css
npm run vendor         # re-copy Alpine.js and DM Sans from node_modules
```

The Tailwind source is `frontend/app.css`. It is kept outside `static/` on purpose:
`collectstatic` with the manifest storage would otherwise try to resolve its
`@import "tailwindcss"` and fail.

---

## Roles and what they see

| Role | Uzbek | Sees and withdraws by default |
|---|---|---|
| ADMIN | Administrator | Everything; Kirim, materials, reports, approvals, users |
| ARRACHI | Arrachi (sawyer) | Sheet materials (MDF, LDSP, fanera, HDF) and their offcuts |
| ROVERCHI | Roverchi (CNC operator) | Sheet materials and their offcuts |
| KRASKACHI | Kraskachi (painter) | Paints, lacquers, primers |
| SHKURKACHI | Shkurkachi (sander) | Linear materials (edge tape, sandpaper rolls) |
| SBORKACHI | Sborkachi (assembler) | Linear materials and hardware (hinges, screws, handles, legs) |

A material's category gives it a default set of trades. The administrator can override
`allowed_roles` per material (e.g. give veneer sheets to the painters). A worker with
several trades sees the union. Archived materials disappear for everyone but keep their
history.

Purchase prices and valuations are admin-only: workers see quantities, never dollar
figures, in both the HTML pages and the API.

## Business rules

**Kirim (arrival, admin only).** Enter the quantity and *either* the total batch price
*or* the unit price; the other is derived (`unit = total / quantity`, 4 decimal places).
For liquids with a container size the quantity can be entered as a number of
buckets/cans. The material's unit price becomes the **weighted average cost** of stock on
hand plus the new batch; when the shelf was empty it restarts at the batch price.

**Chiqim (withdrawal).**
- *Whole material — "Oldim":* deducts from `WholeMaterial.quantity` under a row lock
  (`select_for_update`), valued at the current average price. Sheets and pieces move in
  whole numbers only; liquids and lengths accept two decimals. You cannot take more than
  is on the shelf.
- *Offcut:* take one or more pieces of a logged offcut; the record is deleted when none
  are left. The ledger keeps the piece's dimensions.

**Qoldiq (offcut logging) — "Qoldiq qo'shish".** Material, length mm, width mm, number of
pieces. Only sheet and linear categories have offcuts. A piece cannot be larger than the
material's standard sheet (rotation allowed). The form shows the piece drawn to scale
inside the sheet as you type.

**Money.** Offcut movements are booked at **$0**: the cost of the parent sheet was
already booked when the sheet was withdrawn, so pricing the offcut again would count it
twice. Offcuts are therefore not part of the valuation; the dashboard shows their
estimated recoverable value (area share × sheet price) separately.

**Valuation** = Σ `WholeMaterial.quantity × BaseMaterial.unit_price_usd`, each line rounded
to cents before summing so the total always equals the table it summarises.

**Low-stock alert:** an active material whose quantity is *strictly below* its
`minimum_threshold` appears in the purchase list with its shortfall.

**Ledger sign convention:** `quantity_changed` is signed (+ Kirim and offcut deposits,
− Chiqim); `total_price_usd` is always ≥ 0. Database check constraints enforce both.

## Pages

| URL | Who | What |
|---|---|---|
| `/` | everyone | Tabbed inventory: whole materials, then offcuts; search, category filter, "Oldim" |
| `/inventory/whole/<id>/withdraw/` | trade | Take whole units: stepper, quick chips, stock after withdrawal vs minimum |
| `/inventory/offcuts/add/` | trade | Log an offcut with a live to-scale preview |
| `/inventory/offcuts/<id>/withdraw/` | trade | Take offcut pieces |
| `/inventory/history/` | everyone | My own movements |
| `/admin-dashboard/` | admin | Valuation, this month's Kirim vs Chiqim, 6-month chart, purchase list, approvals, recent activity |
| `/admin-dashboard/kirim/` | admin | Book an arrival with a live preview of the new average price |
| `/admin-dashboard/materials/` | admin | Material catalogue (create, edit, archive) |
| `/admin-dashboard/reports/` | admin | Monthly history filtered by month, type, category, worker and material; CSV export |
| `/admin-dashboard/pending-users/` | admin | Approve registrations (adjust roles first) or reject them |
| `/admin-dashboard/users/` | admin | Edit roles, revoke approval, deactivate, reset a password |
| `/accounts/register/` | public | Worker self-registration with trade checkboxes |
| `/django-admin/` | superuser | Django admin (ledger is read-only there) |

## REST API

Session-authenticated (log in through the site first), JSON, paginated by 50. Every
endpoint except registration requires an approved account; admin endpoints return 403
for workers. Service-layer validation errors come back as 400 with field keys.

| Method & path | Who | Purpose |
|---|---|---|
| `POST /api/auth/register/` | public (10/hour per IP) | `{username, full_name, password, roles: [...]}` creates a pending worker |
| `GET /api/me/` | approved | Current user and roles |
| `GET /api/whole-materials/?q=&category=` | approved | Whole stock visible to the caller |
| `POST /api/whole-materials/<id>/withdraw/` | trade | `{quantity, notes}` |
| `GET /api/offcuts/` · `POST /api/offcuts/` | trade | List / log an offcut `{material_id, length_mm, width_mm, quantity, notes}` |
| `POST /api/offcuts/<id>/withdraw/` | trade | `{quantity, notes}` |
| `GET /api/transactions/?month=YYYY-MM&type=&category=&material=&user=` | approved | Ledger (workers: own entries only). Read-only: no PUT/PATCH/DELETE |
| `POST /api/kirim/` | admin | `{material_id, quantity, unit_price_usd \| total_price_usd, notes}` |
| `GET /api/dashboard/` | admin | Valuation, current month, last 6 months, low-stock list |
| `GET/POST/PATCH /api/materials/` | admin | Material catalogue (no DELETE; archive with `is_active=false`) |
| `GET /api/pending-users/` · `POST …/<id>/approve/` · `POST …/<id>/reject/` | admin | Approval queue; `approve` accepts optional `{roles: [...]}` |

## Tests

```bash
python manage.py test                                               # SQLite
DATABASE_URL=postgres://... python manage.py test                   # PostgreSQL
```

189 tests, in `apps/users/tests/` and `apps/inventory/tests/`:

1. **Registration and approval:** pending accounts, role checkboxes, no self-granted
   ADMIN, login blocked with a clear message, revoking approval ends live sessions,
   approve-with-role-change, reject, admin self-lockout guard, API equivalents.
2. **Role-based queryset filtering:** each trade sees only its materials and offcuts,
   multi-role union, admin overrides, archived materials, exact role-token matching. Run on
   both backends: PostgreSQL uses JSONB containment and SQLite a quoted-token match.
3. **Dual inventory and offcut logging:** Kirim in both price modes, weighted average,
   integer units, insufficient stock, whole and offcut stock never touching each other,
   partial and final offcut withdrawal, sheet-size validation, ledger immutability
   (ORM, constraints, and the PostgreSQL trigger via raw SQL — skipped on SQLite).
4. **Valuation and alerts:** Σ qty × price with per-line rounding, offcuts excluded,
   strict below-minimum rule, monthly Kirim/Chiqim across a time-zone month boundary, the
   report filters and aggregates, CSV export (with spreadsheet-formula neutralisation).

## Project layout

```
config/            settings (env-driven), urls, API router, wsgi/asgi
apps/users/        custom User (roles JSON list, approval fields), auth backend that blocks
                   unapproved users, registration/approval views, forms, API, services
apps/inventory/    models, services.py (all writes), selectors.py (all reads, role
                   visibility, valuation, reports), views, forms, API, templatetags, seed_demo
templates/         base shell, partials, users/, inventory/, 403/404/500
frontend/app.css   Tailwind v4 source  ->  static/css/app.css (built, committed)
static/            built CSS, vendored Alpine.js, charts.js, DM Sans, logo mark
```

All writes go through `apps/inventory/services.py` (atomic, row-locked, validated); all
role filtering goes through `apps/inventory/selectors.py`. HTML views, the API and
management commands share both, so a rule only lives in one place.

## Design

The interface is a working tool used one-handed on a shop floor, so the phone layout
comes first. It has a bottom bar (Ombor, Qoldiq +, Tarix, Menyu), touch targets of at least 40px
(56px list rows, 44px "Oldim" buttons, 48px primary actions), and number steppers with
quick-pick chips.
On desktop it becomes a sidebar and top bar, with the valuation always visible to the
administrator.

The visual system is an "accent ladder": a neutral grey ground and white panels with
1px borders. One hue appears at full strength only on small marks, and its pale tints
carry the areas. That hue is oak (`#9a5b22`), taken from the material on the shelves. It
replaces the magenta that the design tool first dealt. The one typeface is DM Sans,
served locally. Kirim and Chiqim share one diverging chart: arrivals above the line,
withdrawals below it, with the unfinished current month drawn lighter. Two elements are
specific to this workshop: the offcut-to-scale figure, and a restock strip for each
low item (a stock track with a tick at the minimum).

## Decisions and deviations from the brief

- **`User` lives in `apps/users`** with `roles` as a JSON list (checkbox roles, several
  per worker) plus `is_approved`, `approved_at`, `approved_by`. Unapproved users are
  refused by the authentication backend on every request, not only at login.
- **Weighted-average cost** instead of overwriting the unit price on each Kirim, so the
  valuation stays correct when the same material is bought at different prices.
- **Offcut movements at $0** (see *Money* above) to avoid double counting.
- **`BaseMaterial.is_active`** (archive) instead of deleting; ledger foreign keys are
  `PROTECT`, so history can never be erased by deleting a material or user.
- **Prices hidden from workers** in pages and API, beyond the brief's role isolation.
- **Uzbek UI copy**; model choice labels stay bilingual as given in the spec.
- **Python:** the Docker image uses 3.12 as specified. Development and the test runs
  for this delivery used Python 3.12 locally, where all tests pass on both PostgreSQL 16
  and SQLite.
- Out of scope, as the brief says: native apps and QR codes. The API uses session
  authentication; add token auth if a separate client needs it.

## Deployment (EC2 + CI/CD)

Production runs on an Ubuntu EC2 host next to other apps: PostgreSQL (database
`wms_db`, role `wms_user`), gunicorn as the `wms` systemd service on `127.0.0.1:8001`,
and an nginx site `wms` that serves **https://profur.shamsiyev.uz** (Let's Encrypt
certificate, renewed by certbot's timer). Plain HTTP and the bare IP `3.27.91.152`
redirect there. Static files are served by WhiteNoise.

| Path on the server | What |
|---|---|
| `/var/www/wms/app` | Symlink to the active release |
| `/var/www/wms/releases/` | Versioned copies uploaded by CI |
| `/var/www/wms/venv` | Python virtualenv (created by `deploy/deploy.sh`) |
| `/var/www/wms/.env` | Production settings and secrets (mode 600, never in git) |
| `/usr/local/bin/wms-manage` | `manage.py` with the production env, e.g. `wms-manage createsuperuser` |

**CI/CD** (`.github/workflows/ci-cd.yml`) runs Ruff, Django and translation checks, and
the full test suite against PostgreSQL 16 for pull requests and pushes to `main`. A
passing revision at the head of `main` is uploaded to a new release directory over
SSH. The deploy script installs requirements, migrates the database, collects static
files, and activates the release. If the service fails to start or its health and
login checks fail, the script reactivates the previous code release when available.

Repository secrets used by the deploy job: `DEPLOY_HOST`, `DEPLOY_USER`,
`DEPLOY_SSH_KEY` (a deploy-only key whose public half is in the server's
`authorized_keys`) and `DEPLOY_KNOWN_HOSTS` (the server's pinned host key).

**New server:** `ssh ubuntu@HOST 'PUBLIC_HOST=HOST bash -s' < deploy/server-setup.sh`
installs anything missing, creates the database, role and `.env` (secrets are
generated on the server), the systemd unit, the nginx site and `wms-manage`. It is safe
to re-run and keeps an existing `.env`.

**Moving to a domain with HTTPS** (already done for profur.shamsiyev.uz; the setup
script leaves an HTTPS nginx site alone): point DNS at the server, add the name to the
nginx site and run `sudo certbot --nginx -d NAME`, then in `/var/www/wms/.env` add the name
to `DJANGO_ALLOWED_HOSTS` and `https://NAME` to `DJANGO_CSRF_TRUSTED_ORIGINS`, and set
`DJANGO_SECURE_COOKIES=True`, `DJANGO_SECURE_SSL_REDIRECT=True` and
`DJANGO_BEHIND_PROXY=True`; then `sudo systemctl restart wms`.

## Production checklist

- `DJANGO_DEBUG=False`, a long random `DJANGO_SECRET_KEY`, real `DJANGO_ALLOWED_HOSTS`
  and `DJANGO_CSRF_TRUSTED_ORIGINS`.
- PostgreSQL via `DATABASE_URL`; run `migrate` on deploy (installs the ledger trigger).
- TLS in front; `DJANGO_BEHIND_PROXY=True` if the proxy terminates HTTPS; raise
  `DJANGO_HSTS_SECONDS` once HTTPS is confirmed. `python manage.py check --deploy` passes
  with these set.
- Back up the database: the ledger is the audit trail.
