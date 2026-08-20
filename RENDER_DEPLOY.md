# Deploy EZCommish on Render

## What this package is prepared for

EZCommish now supports:
- Local development with SQLite when `DATABASE_URL` is not set.
- Render production deployment with PostgreSQL when `DATABASE_URL` is set.
- Gunicorn as the production web server.
- Render proxy/HTTPS awareness.
- Secure session cookies when the `RENDER` environment variable is present.
- Infrastructure-as-code through `render.yaml`.

## Recommended deployment: Render Blueprint

1. Create a new GitHub repository named `ezcommish`.
2. Upload the contents of this folder to the repository root.
3. In Render, choose **New > Blueprint**.
4. Connect your GitHub account/repository.
5. Select the `ezcommish` repository. Render will detect `render.yaml`.
6. Review the proposed resources:
   - Web service: `ezcommish`
   - PostgreSQL database: `ezcommish-db`
7. Create/deploy the Blueprint.
8. Render will automatically create and inject `DATABASE_URL` and generate `SECRET_KEY`.
9. When deployment finishes, open the temporary `onrender.com` URL and test the full commissioner/manager flow before connecting the GoDaddy domain.

## Manual Render settings (if you do not use Blueprint)

Web Service:
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app`
- Health check: `/`

Environment variables:
- `SECRET_KEY`: generate a long random value
- `DATABASE_URL`: use the Render PostgreSQL internal connection string
- `PYTHON_VERSION`: `3.12.8`

## Important

Do not use the local SQLite database for the hosted production beta. Render services can be restarted/redeployed, so production data should live in PostgreSQL.

The current commissioner login is still MVP-level name + phone lookup. Before a public launch, replace it with verified phone OTP or another secure authentication mechanism.
