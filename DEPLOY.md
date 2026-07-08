# Deploying the Cellar ERP to Heroku

This scaffold makes the existing Django project deployable to Heroku on your current
Basic dynos + Postgres Essential-0 — no code changes to the `cellar` app, only the
project wiring (`config/`) and env-driven settings.

## What's in the scaffold
- `config/settings.py` — 12-factor settings: SQLite + DEBUG locally, Postgres + hardened
  security on Heroku, switched entirely by environment variables.
- `config/{urls,wsgi,asgi}.py`, `config/__init__.py` — project entrypoints.
- `Procfile` — `release:` runs migrations on every deploy; `web:` runs gunicorn.
- `requirements.txt`, `.python-version` — dependencies + Python version for Heroku's builder.
- `.gitignore`, `.env.example` — keep secrets and the local DB out of git.
- `manage.py` — points at `config.settings`.

## One-time setup
```bash
# from the project root (where manage.py lives)
git init && git add . && git commit -m "Heroku-ready cellar ERP"
heroku git:remote -a YOUR-APP-NAME          # your existing Heroku app

# config vars (never commit these)
heroku config:set SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(50))')"
heroku config:set DEBUG=False
heroku config:set ALLOWED_HOSTS="YOUR-APP-NAME.herokuapp.com"   # add a custom domain here too
# DATABASE_URL is already set by your Postgres Essential-0 add-on — nothing to do.
```

## Deploy
```bash
git push heroku main            # build → release (migrate) → web (gunicorn) start
heroku run python manage.py createsuperuser
heroku open                     # /admin/ is live over HTTPS
```
Static files are handled automatically by WhiteNoise during the build (`collectstatic`).

## Local development (unchanged)
Copy `.env.example` to `.env`, set `DEBUG=True`, and run `python manage.py runserver`.
With no `DATABASE_URL`, it uses SQLite exactly as before.

## Backups (do this now — compliance data)
Essential-0 has limited retention, so schedule captures and pull copies off-platform:
```bash
heroku pg:backups:schedule DATABASE_URL --at "02:00 America/Los_Angeles"
heroku pg:backups:capture        # on-demand
heroku pg:backups:download       # pull a copy off Heroku periodically
```

## Notes / deliberate choices
- **File outputs (filled 5120.17 / 5000.24 / Crush Report):** Heroku's filesystem is
  ephemeral, so the API will *stream these on demand* rather than write them to disk — no
  S3 needed yet. If you later want to keep generated files, add `django-storages` + S3.
- **Security is on in production only:** SSL redirect, secure cookies, HSTS, and the host
  allowlist activate when `DEBUG=False`; local dev stays convenient.
- **DB connections:** `conn_max_age=600` reuses connections to respect Essential-0's
  20-connection cap. Fine for you and a handful of users.
- **Ready for the API:** `rest_framework` and `corsheaders` are installed and configured,
  so the DRF API layer drops straight in next (mount it at `config/urls.py`).

## When to upgrade (reliability, not size)
- Postgres → a Standard tier before this is your official filing system of record (stronger
  point-in-time backups). Migration is a `heroku pg:copy`, not a rebuild.
- A second/Standard dyno only if you want zero-downtime deploys or more headroom — not yet.
