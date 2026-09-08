# Sendspark Integration Setup

Blackink uses Sendspark to embed a personalised video in each campaign email.
The video template is recorded once; the API personalises it per prospect using
three dynamic variables at send time.

---

## 1. Dynamic Variables

These are the exact placeholder names to use when building the template in
Sendspark's UI. **The names must match exactly** — the integration passes them
verbatim to the API.

| Variable Name    | What it Contains              | Example Value                     |
|------------------|-------------------------------|-----------------------------------|
| `company_name`   | Prospect's company name       | `Sunshine Property Management LLC` |
| `response_time`  | Detected form-reply latency   | `14 hr 23 min`                    |
| `loss_estimate`  | Annual revenue at risk        | `$23,344`                         |

These values are pulled directly from the Ghost Shopper audit. A prospect who
never replied receives `response_time = "No Reply"` and the corresponding loss
estimate based on a 24-hour gap.

---

## 2. Video Script Guide

The sales rep recording the template should speak to these variables naturally.
Suggested script structure (60-90 seconds):

**Opening (0-10 s)**
> "Hi [company_name] -- I'm [name] from Blackink."

**The finding (10-35 s)**
> "We ran a response-time audit on your contact form and measured how long it
> took to get a reply. Your current response time came in at [response_time]."

**The impact (35-60 s)**
> "Based on industry benchmarks for PM firms in your market, that delay is
> estimated to cost you [loss_estimate] per year in owner leads that move on
> before you reply."

**The CTA (60-90 s)**
> "I'd love to show you how Blackink clients respond in under five minutes,
> 24/7. I've attached a full audit report -- hit reply or grab a slot on my
> calendar."

**Recording tips:**
- Leave a natural pause after each `[variable]` mention -- Sendspark overlays
  the dynamic text here; awkward timing is obvious.
- Record in a quiet room; Sendspark's AI voice cloning only activates on the
  Growth plan and above.
- A plain background or branded backdrop works best.

---

## 3. Sendspark Account Setup

### Step 1 -- Create account
Go to [sendspark.com](https://sendspark.com) and sign up.
Solo plan ($49/month) is sufficient for early outreach (100 personalised
video minutes/month).

### Step 2 -- Record the template video
- In the Sendspark dashboard, create a new **Dynamic Video**.
- Record using the script above, inserting the three variables
  (`company_name`, `response_time`, `loss_estimate`) where shown.
- Publish the template.

### Step 3 -- Get the Template ID
- Open the published template in the dashboard.
- Copy the template ID from the URL or template settings panel.
  It typically looks like `tpl_xxxxxxxxxxxxxxxx`.

### Step 4 -- Get the API Key
- Go to **Settings > API** in the Sendspark dashboard.
- Generate a new API key and copy it immediately (shown once).

### Step 5 -- Set environment variables
Add to the server's environment (or `.env.local` for local dev):

```
SENDSPARK_API_KEY=sk-your-api-key-here
SENDSPARK_TEMPLATE_ID=tpl-your-template-id-here
```

Once both are set, `node_sendspark` activates automatically on the next
worker restart. No code changes needed.

---

## 4. Verifying the API Contract

Before going live, confirm these against [docs.sendspark.com/api](https://docs.sendspark.com/api)
and update `src/agents/ink/subagents/sendspark/client.py` if anything differs:

| Item                     | Assumed value                         | Where to update        |
|--------------------------|---------------------------------------|------------------------|
| Base URL                 | `https://api.sendspark.com`           | `_BASE_URL`            |
| Render endpoint          | `POST /v1/dynamic-videos`             | `_RENDER_ENDPOINT`     |
| Auth header              | `Authorization: Bearer <api_key>`     | `render()` headers     |
| Variable body key        | `variables: { company_name, ... }`    | `render()` payload     |
| Response: video id key   | `id` or `video_id`                    | `_parse_response()`    |
| Response: page URL key   | `url` or `share_url`                  | `_parse_response()`    |

The `_parse_response()` function tries several common key names. If the actual
response uses a different shape, add it there.

---

## 5. Failure Behaviour

The node is **fail-open on video** -- if Sendspark is unconfigured or returns
an error, the campaign continues without a video link. The email still goes out
with the PDF and GIF. Only the video thumbnail/CTA button is omitted.

| Condition                              | Log message                          | Campaign continues? |
|----------------------------------------|--------------------------------------|---------------------|
| API key / template ID not set          | `SENDSPARK_SKIPPED -- not configured` | Yes                 |
| `latency_sec` or `loss_est` missing   | `SENDSPARK_SKIPPED -- missing audit data` | Yes             |
| API returns non-2xx                    | `SENDSPARK_FAILED -- continuing without video` | Yes        |
| Network timeout                        | `SENDSPARK_FAILED -- continuing without video` | Yes        |
