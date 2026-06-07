import os
import json
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
FORMSPREE_API_KEY   = os.environ["FORMSPREE_API_KEY"]
FORMSPREE_FORM_ID   = "mvznzopq"
GEMINI_API_KEY      = os.environ["GEMINI_API_KEY"]
RESEND_API_KEY      = os.environ["RESEND_API_KEY"]
BUSINESS_EMAIL      = "hello@eeekb.com"
BRAND_NAME          = "EEE Korean Beauty Ltd"
SEEN_FILE           = "seen_submissions.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def fetch_submissions():
    url = f"https://api.formspree.io/api/0/forms/{FORMSPREE_FORM_ID}/submissions"
    headers = {"Authorization": f"Bearer {FORMSPREE_API_KEY}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json().get("submissions", [])

def ask_gemini(submission_text):
    """
    Ask Gemini to triage the submission and return structured JSON.
    Categories:
      - auto_reply   : simple complaint or refund request AI can handle
      - flag_only    : bug report, partnership, anything needing human
      - general      : general feedback / compliment / other
    """
    prompt = f"""You are the customer support AI for {BRAND_NAME}, a Korean beauty brand.
A customer has submitted the following message via the website contact form:

---
{submission_text}
---

Respond ONLY with a valid JSON object — no preamble, no markdown, no backticks. Use this exact structure:

{{
  "category": "auto_reply" | "flag_only" | "general",
  "reason": "one sentence explaining your categorisation",
  "priority": "high" | "medium" | "low",
  "customer_reply": "your friendly, casual reply to the customer (null if category is flag_only)",
  "internal_summary": "a short summary for the business owner flagging what this is about"
}}

Rules:
- auto_reply: simple complaints, refund requests, order issues, basic product questions — AI handles it, but still flag to business
- flag_only: bugs, partnership enquiries, legal issues, media/press, anything requiring a human decision — do NOT auto-reply
- general: compliments, general feedback, surveys — log and flag lightly
- Tone for customer replies: friendly, warm, casual — represent {BRAND_NAME} well
- Always sign off customer replies as "The {BRAND_NAME} Team"
- Mark priority HIGH if it involves money, legal, urgent complaints, bugs affecting purchases, or partnership opportunities
"""

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1000},
        },
    )
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Strip any accidental markdown fences
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def send_email(to, subject, html_body, reply_to=None):
    payload = {
        "from": f"{BRAND_NAME} <onboarding@resend.dev>",
        "to": [to],
        "subject": subject,
        "html": html_body,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    r.raise_for_status()
    return r.json()

def extract_text(submission):
    """Pull readable text from a Formspree submission dict."""
    skip = {"_id", "_date", "_replyto", "ip", "_ip", "_gotcha", "_language"}
    lines = []
    for k, v in submission.items():
        if k not in skip and v:
            lines.append(f"{k.replace('_', ' ').title()}: {v}")
    return "\n".join(lines)

def customer_email(submission):
    """Best-guess customer email from a submission."""
    for key in ("email", "_replyto", "Email"):
        if submission.get(key):
            return submission[key]
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.utcnow().isoformat()}] Checking Formspree submissions...")
    seen = load_seen()
    submissions = fetch_submissions()
    new_count = 0

    for sub in submissions:
        sub_id = sub.get("_id") or sub.get("id")
        if not sub_id or sub_id in seen:
            continue

        new_count += 1
        print(f"  Processing submission {sub_id}...")

        text = extract_text(sub)
        customer_email_addr = customer_email(sub)
        submitted_at = sub.get("_date", "unknown time")

        try:
            result = ask_gemini(text)
        except Exception as e:
            print(f"  Gemini error for {sub_id}: {e}")
            seen.add(sub_id)
            continue

        category  = result.get("category", "general")
        priority  = result.get("priority", "low")
        reason    = result.get("reason", "")
        summary   = result.get("internal_summary", "")
        reply_txt = result.get("customer_reply")

        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "⚪")

        # ── 1. Send customer reply (auto_reply category only) ──────────────
        if category == "auto_reply" and customer_email_addr and reply_txt:
            customer_html = f"""
<p>Hi there,</p>
{('<p>' + '</p><p>'.join(reply_txt.split(chr(10))) + '</p>')}
<br>
<p style="color:#888;font-size:12px;">
  This message was sent in response to your enquiry submitted on {submitted_at}.
</p>
"""
            try:
                send_email(
                    to=customer_email_addr,
                    subject=f"Re: Your message to {BRAND_NAME}",
                    html_body=customer_html,
                )
                print(f"  ✅ Auto-reply sent to {customer_email_addr}")
            except Exception as e:
                print(f"  ❌ Failed to send customer reply: {e}")

        # ── 2. Flag to business (all categories) ──────────────────────────
        action_label = {
            "auto_reply": "Auto-replied to customer + flagged for your records",
            "flag_only":  "Needs your attention — no auto-reply sent",
            "general":    "Logged for your records",
        }.get(category, "Logged")

        reply_block = ""
        if reply_txt:
            reply_block = f"""
<hr>
<p><strong>Auto-reply sent to customer:</strong></p>
<blockquote style="border-left:3px solid #ccc;padding-left:12px;color:#555;">
  {reply_txt.replace(chr(10), '<br>')}
</blockquote>
"""

        internal_html = f"""
<h2 style="color:#333">{priority_emoji} New Feedback — {priority.upper()} priority</h2>

<table style="border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px">
  <tr><td style="padding:6px 12px;font-weight:bold;width:160px">Category</td><td style="padding:6px 12px">{category.replace('_',' ').title()}</td></tr>
  <tr style="background:#f9f9f9"><td style="padding:6px 12px;font-weight:bold">Priority</td><td style="padding:6px 12px">{priority_emoji} {priority.upper()}</td></tr>
  <tr><td style="padding:6px 12px;font-weight:bold">Action</td><td style="padding:6px 12px">{action_label}</td></tr>
  <tr style="background:#f9f9f9"><td style="padding:6px 12px;font-weight:bold">Customer email</td><td style="padding:6px 12px">{customer_email_addr or 'not provided'}</td></tr>
  <tr><td style="padding:6px 12px;font-weight:bold">Submitted</td><td style="padding:6px 12px">{submitted_at}</td></tr>
  <tr style="background:#f9f9f9"><td style="padding:6px 12px;font-weight:bold">AI summary</td><td style="padding:6px 12px">{summary}</td></tr>
  <tr><td style="padding:6px 12px;font-weight:bold">AI reason</td><td style="padding:6px 12px">{reason}</td></tr>
</table>

<hr>
<p><strong>Original message:</strong></p>
<blockquote style="border-left:3px solid #ccc;padding-left:12px;color:#555;">
  {text.replace(chr(10), '<br>')}
</blockquote>

{reply_block}

<p style="color:#aaa;font-size:11px">Processed by EEE Korean Beauty AI feedback system</p>
"""

        subject_prefix = f"{priority_emoji} [{priority.upper()}]"
        subject = f"{subject_prefix} {BRAND_NAME} Feedback — {category.replace('_',' ').title()}"

        try:
            send_email(
                to=BUSINESS_EMAIL,
                subject=subject,
                html_body=internal_html,
                reply_to=customer_email_addr,
            )
            print(f"  ✅ Internal flag sent to {BUSINESS_EMAIL}")
        except Exception as e:
            print(f"  ❌ Failed to send internal flag: {e}")

        seen.add(sub_id)

    save_seen(seen)
    print(f"  Done. Processed {new_count} new submission(s).")

if __name__ == "__main__":
    main()
