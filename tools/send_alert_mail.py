#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Send an alert e-mail when the weekly update reports a problem (used by the GitHub Action).

Reads _update/update_summary.json (written by tools/hf_update.py) and sends a plain-text mail
through Gmail's SMTP server. Only the Python standard library is used.

Environment variables (set them as GitHub repository secrets, never in a file):
  MAIL_USERNAME   the Gmail address that sends the mail
  MAIL_PASSWORD   a Google "app password" for that account (2-step verification must be on)
  MAIL_TO         optional: the receiving address (default: MAIL_USERNAME)
Optional, filled in automatically by GitHub Actions: GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID

Usage
  python tools/send_alert_mail.py --summary _update/update_summary.json
  python tools/send_alert_mail.py --print      # show the mail instead of sending it
"""
import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage


def build_body(summary):
    lines = ["The weekly update of the Korean party press-release dataset reported a problem.", ""]
    if summary is None:
        lines += ["The job stopped before it could write a summary (for example, a failed installation",
                  "or a missing token). Please open the run log below."]
    else:
        lines.append(f"Period collected: since {summary.get('since')} (run at {summary.get('run_at')})")
        lines.append("")
        problems = summary.get("problems") or []
        if problems:
            lines.append("Problems:")
            lines += [f"  - {p}" for p in problems]
        else:
            lines.append("The run was marked as failed but no specific problem was recorded.")
        new = summary.get("new_rows") or {}
        if new:
            lines += ["", "New rows added per party:"]
            lines += [f"  {k}: +{v}" for k, v in new.items()]
        lines += ["", "What to do: a party whose website was redesigned needs its crawler (crawlers/*.py) updated.",
                  "Parties without problems were still updated; the others will be retried at the next run."]
    run = None
    if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
        run = "{}/{}/actions/runs/{}".format(os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                             os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"])
    if run:
        lines += ["", f"Run log: {run}"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", default="_update/update_summary.json")
    ap.add_argument("--print", action="store_true", help="print the mail instead of sending it")
    args = ap.parse_args()

    summary = None
    if os.path.exists(args.summary):
        with open(args.summary, encoding="utf-8") as f:
            summary = json.load(f)

    user = os.environ.get("MAIL_USERNAME", "")
    to = os.environ.get("MAIL_TO") or user
    msg = EmailMessage()
    msg["Subject"] = "[K-party-press-crawler] Weekly update problem"
    msg["From"] = user or "sender@example.com"
    msg["To"] = to or "receiver@example.com"
    msg.set_content(build_body(summary))

    if args.print:
        print(msg["Subject"], "\n", msg.get_content())
        return 0
    pw = os.environ.get("MAIL_PASSWORD", "")
    if not (user and pw and to):
        print("MAIL_USERNAME / MAIL_PASSWORD are not set; no mail was sent. "
              "See the setup notes in README section 9.", file=sys.stderr)
        return 0  # do not hide the original failure behind a second one
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"alert mail sent to {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
