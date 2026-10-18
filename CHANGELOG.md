# Changelog

## v2 (2026-10-18)

Rebuilt after testing v1 end to end against mock WhatsApp, Claude, GoHighLevel and Slack APIs.

### Fixed: found by testing v1

- **The bot never replied.** Both IF nodes used an operator called `equal`, which n8n does not have. n8n treats an unknown operator as false, so every message ended at the first IF node and the run still showed as "success".
- **Qualified leads were lost.** With the operators corrected, a full six message chat worked, but no GoHighLevel contact and no Slack alert were ever created. "Lead Qualified?" sat after the WhatsApp send node and read the WhatsApp API's response instead of the lead data.
- **The Meta webhook could not be verified.** Meta sends its verification GET to the same callback URL that receives messages. v1 listened for it on a different path, so Meta got a 404.

### Fixed: found in code review

- The GoHighLevel note used the AI summary from a parallel branch that had not run yet, so the note would have failed.
- Phone numbers went to GoHighLevel without the leading `+` (WhatsApp sends `15551234567`), which can be read with the wrong country code.
- Contacts were created rather than upserted, so a lead who came back later became a duplicate.
- If Claude's answer could not be parsed, the stage was reset to -1 and the lead's progress was lost.
- The chat history was saved but never sent to Claude.
- With no verify token configured, an empty token passed verification.
- The default Graph API version was v18.0 from 2023. It now defaults to v23.0 and can be set with `WHATSAPP_API_VERSION`.

### Added

- A signature check on every webhook: `X-Hub-Signature-256`, HMAC SHA256 of the raw body with the app secret. Anything else stops and raises an alert.
- Protection against repeat deliveries, by WhatsApp message ID.
- A polite reply for photos, voice notes and documents. Receipts and reactions are ignored.
- Several messages in one webhook are joined into one turn.
- Claude answers through a forced tool call, so the reply and the extracted details always come back as JSON.
- Retries on every API call, a holding reply when Claude is down, and alerts through the error workflow.
- A 0 to 10 lead score, calculated in code from budget, timeline and need.
- A GoHighLevel upsert, tags added through the add tags endpoint so a returning contact keeps its existing tags, and a note with every detail.
- A failed GoHighLevel save is announced in Slack and retried on the lead's next message.
- One workflow now handles both the Meta verification GET and the message POST.
- Sticky notes on the canvas, an error alert workflow, and an end to end mock test (14 scenarios, 22 checks).

### Changed

- Slack alerts use an incoming webhook (`SLACK_WEBHOOK_URL`) instead of a bot token and channel ID, and Slack is optional.
- Budget, timeline and challenge go into the contact note instead of custom fields, so nothing has to be set up in GoHighLevel first.
- The separate AI summary call is gone. The qualifying call writes the CRM note when the lead completes, which saves one Claude call per lead.
- The model is set with `ANTHROPIC_MODEL` (default `claude-sonnet-5`), and the business name and description with `BUSINESS_NAME` and `BUSINESS_ABOUT`.
- Node names use a colon instead of a dash.

## v1 (2026-05-19)

First build: webhook, a five stage Claude qualification prompt, conversation memory in static data, a GoHighLevel contact, a Slack alert and a separate verification workflow.
