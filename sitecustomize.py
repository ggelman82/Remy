"""Cloud Run compatibility for Google authentication.

Render keeps using google-service-account.json when that file exists.
Cloud Run has no key file; it uses the service account attached to the
running service via Application Default Credentials instead.
"""

import os


if not os.path.exists("google-service-account.json"):
    import google.auth
    import gspread

    def _cloud_service_account(filename="google-service-account.json", *args, **kwargs):
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials, _ = google.auth.default(scopes=scopes)
        return gspread.authorize(credentials)

    gspread.service_account = _cloud_service_account
