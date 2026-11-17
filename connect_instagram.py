"""One-time: connect the Instagram account and store its key in GitHub Secrets.

Run it yourself in a terminal. It asks for the app secret, opens the Instagram/Facebook
login page, and saves the resulting (never-expiring) key as the IG_TOKEN and IG_USER_ID
secrets of the repo. The key is never printed.
"""
import getpass
import os
import subprocess
import sys
import urllib.parse
import webbrowser

import requests

APP_ID = "40582727728039620"
CONFIG_ID = "1478415454228126"
REDIRECT = "https://hillelgf-stack.github.io/trial-bot/callback.html"
REPO = "hillelgf-stack/trial-bot"
GRAPH = "https://graph.facebook.com/v23.0"
GH = os.path.expanduser(r"~\tools\gh\bin\gh.exe")


def fail(msg):
    print(f"\n❌ {msg}")
    input("Enter לסגירה...")
    sys.exit(1)


def set_secret(name, value):
    r = subprocess.run([GH, "secret", "set", name, "--repo", REPO], input=value, text=True,
                       capture_output=True)
    if r.returncode:
        fail(f"שמירה ב-GitHub נכשלה ({name}): {r.stderr.strip()}")


def main():
    print("=== חיבור אינסטגרם לבוט ===\n")
    print("1) באתר המפתחים: App settings > Basic > App secret > Show, והעתק.")
    secret = getpass.getpass("   הדבק כאן את ה-App secret (לא יוצג על המסך) ולחץ Enter: ").strip()
    if not secret:
        fail("לא הודבק App secret")

    login = "https://www.facebook.com/dialog/oauth?" + urllib.parse.urlencode({
        "client_id": APP_ID, "config_id": CONFIG_ID, "redirect_uri": REDIRECT,
        "response_type": "code", "override_default_response_type": "true",
    })
    print("\n2) עכשיו נפתח דף התחברות. בחר 'Continue with Instagram' (או פייסבוק),")
    print("   התחבר ל-hillelgf, סמן את חשבון האינסטגרם ואשר.")
    print("   בסוף יופיע דף 'כמעט סיימנו' - לחץ 'העתק'.")
    webbrowser.open(login)
    back = getpass.getpass("\n3) חזור לכאן, הדבק (Ctrl+V) ולחץ Enter: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(back).query).get("code", [""])[0]
    if not code:
        fail("לא נמצא קוד בכתובת שהודבקה. נסה שוב.")

    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "client_id": APP_ID, "client_secret": secret, "redirect_uri": REDIRECT, "code": code,
    }, timeout=30).json()
    token = r.get("access_token")
    if not token:
        fail(f"קבלת המפתח נכשלה: {r.get('error', r)}")

    accounts = []
    for path, fields in (("me/assigned_instagram_accounts", "id,username"),
                         ("me/accounts", "instagram_business_account{id,username}")):
        d = requests.get(f"{GRAPH}/{path}", params={"access_token": token, "fields": fields},
                         timeout=30).json()
        for item in d.get("data", []):
            acc = item.get("instagram_business_account") or item
            if acc.get("id") and acc.get("username"):
                accounts.append(acc)
        if accounts:
            break
    if not accounts:
        fail("המפתח התקבל אבל לא נמצא חשבון אינסטגרם משויך. ודא שסימנת את hillelgf בהתחברות.")
    acc = next((a for a in accounts if a["username"].lower() == "hillelgf"), accounts[0])

    test = requests.get(f"{GRAPH}/{acc['id']}/media", params={"access_token": token, "limit": 1},
                        timeout=30).json()
    if "error" in test:
        fail(f"המפתח לא מצליח לקרוא את הרילז: {test['error'].get('message')}")

    set_secret("IG_TOKEN", token)
    set_secret("IG_USER_ID", acc["id"])
    print(f"\n✅ מחובר ל-@{acc['username']}. המפתח נשמר בכספת של GitHub.")
    input("אפשר לסגור את החלון (Enter).")


if __name__ == "__main__":
    main()
