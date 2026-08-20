# EZCommish Live MVP

This is the multi-user next-stage MVP for EZCommish. Unlike the original single HTML prototype, this version has a Flask backend, SQLite persistence, commissioner accounts, persistent leagues/managers, unique public league links, locked manager ballots, Round 1 and Round 2 result calculations, commissioner selections, final Draft Day pages, and .ics calendar export.

## Run locally

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Multi-device testing

Run on a computer connected to the same Wi-Fi and browse to the computer's LAN IP on port 5000 from multiple phones. For public internet testing, deploy the folder to a Python-capable host and set a strong `SECRET_KEY` environment variable.

## Production changes before public launch

1. Replace MVP name+phone login with verified phone OTP or another secure authentication method.
2. Replace SQLite with managed PostgreSQL.
3. Add CSRF protection, rate limiting, audit logging and stronger session configuration.
4. Use a real timezone identifier (IANA) and timezone-aware calendar events.
5. Add HTTPS and a custom `ezcommish.com` subdomain/route.
6. Add transactional SMS only if/when desired; the MVP intentionally uses native device sharing to avoid server SMS cost.
7. Add automated tests and accessibility QA.
8. Add analytics and error monitoring.

## Current V1 workflow

Commissioner: login → create league → add managers → choose 3 dates → share common link → view response dashboard/results → select Round 1 option → share same link for Round 2 → select exact start time → confirm Draft Day.

Manager: common link → select name → enter team name → vote all options → locked confirmation → revisit same link for Round 2/final Draft Day.

## Android contact picker
The Add Managers screen includes an `Add from Contacts` button when the browser exposes the Contact Picker API (for example, supported Chrome on Android). The user explicitly selects one contact, and EZCommish fills the manager name and phone number. Unsupported browsers retain manual entry.
