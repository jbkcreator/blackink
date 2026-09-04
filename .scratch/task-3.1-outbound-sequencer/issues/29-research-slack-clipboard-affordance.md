# RESEARCH: what copy-to-clipboard affordance does Slack Block Kit actually offer?

Label: `wayfinder:research`
Status: closed
Assignee:
Blocked by: —

## Resolution (2026-09-03) — resolved by /research subagent

**There is no native copy-to-clipboard button in Block Kit — confirmed.** A
button click is a backend round-trip over Socket Mode; nothing in the button /
overflow / select spec can execute client-side JS or write the user's OS
clipboard. The 3.1.2 requirement "copy URL and note to the setter's clipboard
via a Slack interactive button" is **not literally buildable** and must be
satisfied by Slack's own affordances instead.

**What Slack renders natively (client feature, not Block-Kit config):**

- **Code blocks** (triple-backtick / `rich_text_preformatted`) and **inline
  code** show a **copy icon on hover** in the **desktop** client — a real
  one-click copy, for free.
- **Mobile**: no hover icon; copy is long-press → "Copy" on the selection.
- A **`url`-type button** opens a link directly in the browser with **no
  backend round-trip** required (Slack still POSTs a `block_actions` payload,
  but you may ignore it — no server ack needed to open the link).
- A **modal with a `plain_text_input` prefilled** via `initial_value` gives
  fully selectable/editable text on every client — the reliable cross-client
  fallback for long text.

**Recommended shape for the Touch 4 card (feeds ticket 26's prototype):**

- **LinkedIn URL** → a `url`-type button ("Open LinkedIn search"): one click to
  browser, no round-trip.
- **Connection note** → render inside a `rich_text_preformatted` / triple-
  backtick code block so desktop setters get the native hover-copy icon.
- **Mobile fallback** → a "Copy note" button that opens a modal with a
  `plain_text_input` prefilled with the note (`initial_value`), selectable
  everywhere.

No custom clipboard hack. This **unblocks ticket 26**; the prototype is now
choosing between these known affordances, not discovering whether any exist.

Sources: docs.slack.dev Block Kit button element (`url` field), Formatting
with rich text, rich_text changelog (2023-09-29).

## Question

Does Slack Block Kit / Bolt (Socket Mode) offer any native affordance that
writes text to the clicking user's clipboard? If not — as suspected — what are
the realistic alternatives (code-block hover-copy, prefilled modal, `url`
button), and which work on desktop vs mobile? Resolve before ticket 26's
Touch 4 prototype, so the prototype picks among real options rather than
proving a negative.

## Why it matters

The whole Touch 4 deliverable hinges on how a setter gets a URL + note out of
Slack and into LinkedIn. If the "clipboard button" is a myth, the DoD line
must be reinterpreted, not faked.
