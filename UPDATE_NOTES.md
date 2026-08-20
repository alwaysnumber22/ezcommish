# EZCommish consolidated cleanup update

This build consolidates the current features and makes league deletion visible for every league state, including active/open draft polls.

## Replace in GitHub
For the safest update, replace the full repository contents with this package (preserving your Render environment variables).

At minimum replace:
- app.py
- templates/home.html
- templates/dashboard.html
- templates/managers.html
- static/style.css

## Cleanup controls
Every league card on the commissioner home screen has Delete League, regardless of status.
Every league dashboard has Draft Poll Management with Close, Clear/Reset, Cancel/Void, and Delete League.
