#!/usr/bin/env python3
"""The emails Supabase sends for EminentTracker accounts: invitation, address confirmation, password reset,
sign-in link and change of address. Supabase's own wording ("You've been invited", nothing else) is replaced.

  scripts/account_emails.py --preview DIR     write each email as an HTML file to look at
  scripts/account_emails.py                   install them in the Supabase project

Installing needs a Supabase access token (Dashboard, Account, Access Tokens) in SUPABASE_ACCESS_TOKEN, in the
environment or in .env. Only the email subjects and bodies are changed; no other sign-in setting is touched.
{{ .ConfirmationURL }} is filled in by Supabase when it sends.
"""
import os
import sys
from pathlib import Path

PROJECT = "jsvwmwyczjhwlqkcrmhz"
SITE = "https://eminenttracker.com"
CONTACT = "info@eminenttracker.com"
WHAT = ("EminentTracker shows the parcels and owners in the path of planned public projects, "
        "such as road widenings, pipelines and transit lines, in all 50 states, often years before a notice of taking.")

# key: (subject, heading, paragraphs, button label, note under the button)
EMAILS = {
    "invite": (
        "Your EminentTracker account is ready to set up",
        "You have been given an EminentTracker account",
        [WHAT,
         "An account has been opened for this email address. To start using it, choose a password. "
         "The button below takes you to your account page on eminenttracker.com, where you set the password and add your firm's details."],
        "Set my password",
        "This link works once and expires after 24 hours. If it has expired, go to eminenttracker.com/login.html, "
        "enter this email address and choose \"Email me a reset link\"."),
    "confirmation": (
        "Confirm your email to finish creating your EminentTracker account",
        "Confirm your email address",
        ["Thank you for signing up for EminentTracker. " + WHAT,
         "Confirm that this address is yours and you will be taken to your account page to pay for your plan. "
         "Your account opens as soon as the payment goes through."],
        "Confirm my email",
        "If you did not sign up for EminentTracker, ignore this email and no account will be created."),
    "recovery": (
        "Reset your EminentTracker password",
        "Choose a new password",
        ["We received a request to reset the password for the EminentTracker account at this email address. "
         "The button below takes you to your account page, where you type a new password."],
        "Choose a new password",
        "This link works once and expires after an hour. If you did not ask for it, ignore this email; your password stays as it is."),
    "magic_link": (
        "Your EminentTracker sign-in link",
        "Sign in to EminentTracker",
        ["Use the button below to sign in to your EminentTracker account without typing your password."],
        "Sign in",
        "This link works once and expires after an hour. If you did not ask for it, ignore this email."),
    "email_change": (
        "Confirm the new email address for your EminentTracker account",
        "Confirm your new email address",
        ["You asked to change the email address on your EminentTracker account to this one. "
         "Confirm it and you will sign in with this address from now on."],
        "Confirm this address",
        "If you did not ask for this change, ignore this email and tell us at " + CONTACT + "."),
}

FONT = "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def body(heading, paragraphs, button, note):
    """Table layout with inline styles: the only thing every mail program draws the same way."""
    paras = "".join(f'<p style="margin:0 0 16px;{FONT};font-size:16px;line-height:1.6;color:#10202f">{p}</p>' for p in paragraphs)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EminentTracker</title></head>
<body style="margin:0;padding:0;background:#f3f5f4">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5f4"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid #e6e9e7;border-radius:6px">
<tr><td style="padding:24px 32px;border-bottom:1px solid #e6e9e7;{FONT};font-size:18px;font-weight:700;letter-spacing:-0.01em;color:#10202f">EminentTracker</td></tr>
<tr><td style="padding:32px">
<h1 style="margin:0 0 20px;{FONT};font-size:24px;line-height:1.25;font-weight:700;letter-spacing:-0.02em;color:#10202f">{heading}</h1>
{paras}
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0"><tr><td style="background:#17607a;border-radius:4px">
<a href="{{{{ .ConfirmationURL }}}}" style="display:inline-block;padding:14px 24px;{FONT};font-size:16px;font-weight:600;color:#ffffff;text-decoration:none">{button}</a></td></tr></table>
<p style="margin:0 0 16px;{FONT};font-size:14px;line-height:1.6;color:#4a5a66">{note}</p>
<p style="margin:0;{FONT};font-size:14px;line-height:1.6;color:#4a5a66">If the button does not work, copy this address into your browser:<br>
<a href="{{{{ .ConfirmationURL }}}}" style="color:#17607a;word-break:break-all">{{{{ .ConfirmationURL }}}}</a></p>
</td></tr>
<tr><td style="padding:20px 32px;border-top:1px solid #e6e9e7;{FONT};font-size:13px;line-height:1.6;color:#4a5a66">
Sent to {{{{ .Email }}}} because of an account at <a href="{SITE}" style="color:#17607a">eminenttracker.com</a>.
Questions: <a href="mailto:{CONTACT}" style="color:#17607a">{CONTACT}</a>.<br>EminentTracker is a service of Duerr Imports, LLC.</td></tr>
</table></td></tr></table></body></html>
"""


def settings():
    out = {}
    for key, (subject, heading, paragraphs, button, note) in EMAILS.items():
        out[f"mailer_subjects_{key}"] = subject
        out[f"mailer_templates_{key}_content"] = body(heading, paragraphs, button, note)
    return out


def token():
    if os.environ.get("SUPABASE_ACCESS_TOKEN"):
        return os.environ["SUPABASE_ACCESS_TOKEN"]
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SUPABASE_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def main(argv):
    if len(argv) == 2 and argv[0] == "--preview":
        out = Path(argv[1])
        out.mkdir(parents=True, exist_ok=True)
        for key in EMAILS:
            sample = settings()[f"mailer_templates_{key}_content"].replace("{{ .ConfirmationURL }}", SITE + "/account.html#sample-link").replace("{{ .Email }}", "you@yourfirm.com")
            (out / f"{key}.html").write_text(sample)
            print(f"{key}: {EMAILS[key][0]}")
        return 0
    if argv:
        print(__doc__)
        return 2
    t = token()
    if not t:
        print("No SUPABASE_ACCESS_TOKEN in the environment or .env. Nothing was changed.", file=sys.stderr)
        return 1
    import httpx
    r = httpx.patch(f"https://api.supabase.com/v1/projects/{PROJECT}/config/auth", headers={"Authorization": "Bearer " + t}, json=settings(), timeout=60)
    if r.status_code >= 300:
        print(f"Supabase refused the change ({r.status_code}): {r.text[:300]}", file=sys.stderr)
        return 1
    saved = r.json()
    for key, (subject, *_rest) in EMAILS.items():
        print(("installed " if saved.get(f"mailer_subjects_{key}") == subject else "NOT SAVED ") + key)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
