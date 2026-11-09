# Polar site fixes (local branch `polar-fixes-local`) — NOT pushed / NOT live

Prepared 9 Nov 2026 (Europe/Amsterdam). Branch only. Do **not** push to `main` / GitHub Pages until Nino approves.

## What changed on this branch (site HTML only)

1. **All `buy.polar.sh` anchors** across the site:
   - `rel="nofollow noopener"` so crawlers / link-preview bots are asked not to follow (each visit still creates a Polar checkout session if they ignore it, but we stop inviting them).
   - `?utm_source=site&utm_medium=web&utm_content=<page>` so Polar session/order metadata can show which page the click came from.
2. **Homepage (`index.html`)** focused on **Filing Alerts** as the hero product, with a concrete example alert message. Other products moved under **Meer**.
3. **`filing-alerts.html`** copy updated for **7-day free trial** + **Telegram link after payment** (no `@userinfobot` / chat-id field in the happy path). Honest note that Polar/bot backend is not updated yet — the button still hits the existing checkout link.
4. **`robots.txt`**: commented placeholder for future `/buy/` or `/go/` redirect paths. Buy URLs are off-site on `buy.polar.sh`, so there is nothing local to Disallow today.
5. **GoatCounter snippet** ready in `index.html` and `filing-alerts.html`, **commented out**. Needs a free [GoatCounter](https://www.goatcounter.com/) account + site code (`GOATCOUNTER_CODE`). Cookieless; mention in Privacy once enabled.

## Required Polar / bot changes (DESCRIBED ONLY — do not execute without Nino OK)

### A. 7-day free trial on Filing Alerts
- Polar Checkout Links / product API (docs ~2026-10) support `trial_interval` + `trial_interval_count`.
- Set trial to **7 days**, then **$12/month**.
- Card is collected at checkout; first charge after trial unless cancelled.
- Update refund copy if trial-then-charge changes the 14-day window (page currently says "14-day refund after first charge").

### B. Remove Telegram chat-id custom field from checkout
- Delete the checkout custom field that asks for Telegram chat id.
- Keep the tickers field (or collect tickers post-link in the bot — product decision).

### C. Success URL → bot deep link
- Set Polar success / return URL to:
  `https://t.me/FilingAlertsNV_bot?start={CHECKOUT_ID}`
  (or Polar's documented checkout-id template variable — confirm exact placeholder in Polar dashboard).
- Bot (`@FilingAlertsNV_bot`) must handle `/start <CHECKOUT_ID>`:
  1. Look up the Polar checkout/order by id (Polar API).
  2. Store `chat_id` ↔ subscription / tickers.
  3. Confirm in chat: "Linked. Watching: …"
- Until this ships, do **not** advertise "no chat id" as live fact without the note already on the page.

### D. Analytics account
- Create GoatCounter site, paste code into the commented snippets, uncomment, update Privacy.
- Alternative cookieless options (Plausible self-host, etc.) also fine — GoatCounter is the default suggestion because it is free and cookie-free.

### E. Optional hardening
- Replace raw `buy.polar.sh` hrefs with same-origin `/go/<product>` redirects that 302 to Polar (then `Disallow: /go/` in robots.txt). Bigger change; not done on this branch.

## What must NOT happen without Nino's approval
- `git push` of this branch or merge to `main` (GitHub Pages would go live).
- Editing the live Polar product (trial, custom fields, success URL).
- Deploying bot code that handles `/start <CHECKOUT_ID>`.
- Enabling GoatCounter (needs an account under Nino / Ventures).

## Preview
- Before: `/workspace/research/polar-preview/index.before.html`, `filing-alerts.before.html`
- After: this branch's `index.html`, `filing-alerts.html`
- Open locally or via a static server; do not push.
