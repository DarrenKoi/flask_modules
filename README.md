# flask_modules

Flask project skeleton for Python 3.11 with:

- root `index.py` entrypoint
- `api/` package for Blueprints
- root `wsgi.ini` for WSGI/uWSGI-style cloud deployments

## Run locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python index.py
```

## Routes

- `GET /`
- `GET /api/health`
- `GET /api/ping`

## Cloud entrypoint

Use `index:app` as the WSGI callable.
