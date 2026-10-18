# Test report

**Date:** 2026-10-18
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 22 of 22 checks pass across 14 scenarios

## How it was tested

No real WhatsApp number, Claude account, GoHighLevel account or Slack workspace was used. The test script:

1. starts mock WhatsApp Cloud, Anthropic, GoHighLevel and Slack APIs on the test machine,
2. imports a temporary copy of the workflow with every API host pointed at the mocks and fake keys,
3. sends signed webhooks exactly like Meta does (`X-Hub-Signature-256`, HMAC SHA256 of the raw body),
4. waits for each n8n run to finish, then checks every request the workflow made and the node each run ended on,
5. deletes the temporary copy.

The mock Claude returns scripted answers keyed by the lead's message, so the test checks the workflow, not the model. It still rejects requests the real Messages API would reject: a missing key or version header, no system prompt, roles that do not alternate, a conversation that does not start and end with the lead, or a tool that is not forced.

### Check against the real Claude API

The mock cannot prove the real API accepts the request, so one captured request (the third message of the test chat) was also sent to Anthropic's token counting endpoint. That endpoint validates the full request (system prompt, messages, tool schema and forced tool choice) and the model name, without generating a reply or using credits.

| Model | Result | Input tokens |
|---|---|---|
| `claude-sonnet-5` (default) | 200 OK | 1,650 |
| `claude-haiku-4-5-20251001` | 200 OK | 1,486 |

## v1, before the rebuild

The original workflow was run against the same mocks first.

| Run | What happened |
|---|---|
| v1 as built, one text message | 0 calls to Claude, 0 replies. The run still showed "success" |
| v1 with only the IF operators corrected, six message chat | Six correct replies, and the stage moved 0 to 5 as expected. But after the lead completed: **0 GoHighLevel calls and 0 Slack alerts**. The lead was lost |
| Meta verification GET on the callback URL | `404 This webhook is not registered for GET requests` |
| The same GET on v1's separate verify path | `200`, but Meta never calls that path |

The causes are in [CHANGELOG.md](../CHANGELOG.md).

## v2 results

```
Meta verification, right token
  PASS  returns the challenge with 200  (200 '1158201444')

Meta verification, wrong token
  PASS  refused with 403  (403 'Verification failed')

Forged webhook, wrong signature
  PASS  stopped at the signature check, nothing sent  (ended at Stop: Bad Signature)

Forged webhook, no signature
  PASS  stopped at the signature check, nothing sent  (ended at Stop: Bad Signature)

Delivery receipt
  PASS  ignored, nothing sent  (success)

Photo instead of text
  PASS  polite ask for text, Claude not called

Full chat (6 messages)
  PASS  one reply per message, in order  (6 replies)
  PASS  Claude gets the growing chat history  (messages sent to Claude per turn: [1, 3, 5, 7, 9, 11])
  PASS  a detail is kept when a later answer leaves it empty  (turn 4 prompt knows name and company)
  PASS  one GoHighLevel upsert with the right fields
  PASS  tags added with the score (9/10, hot lead)
  PASS  note with every detail and the AI summary
  PASS  Slack alert with score and contact link

Same message delivered twice
  PASS  second copy ignored  (no Claude call, no reply)

Qualified lead writes again
  PASS  friendly reply, no second lead in GoHighLevel  (reply sent, 0 GoHighLevel calls)

Answers everything at once
  PASS  qualified in one step, score 10/10

Two messages in one webhook
  PASS  joined into one turn, one reply  (1 Claude call)

Claude is down (529)
  PASS  retried 3 times, holding reply sent, run flagged for the alert  (3 attempts, ended at Stop: AI Failed)

Claude is back
  PASS  the chat continues normally  (reply sent)

GoHighLevel is down
  PASS  retried, Slack says NOT saved, lead still got a reply

GoHighLevel is back
  PASS  the save is retried on the next message

WhatsApp rejects the reply
  PASS  retried 3 times, run flagged for the alert with the phone number  (ended at Stop: Reply Failed)

22/22 checks passed
```

## What the workflow sent (full chat scenario)

**GoHighLevel: upsert contact** (`POST /contacts/upsert`)

```json
{"locationId": "mock-location", "firstName": "Jamie", "lastName": "Rivera", "phone": "+15553334444", "companyName": "Rivera Roofing", "source": "WhatsApp Lead Bot"}
```

**GoHighLevel: add tags** (`POST /contacts/{id}/tags`)

```json
{"tags": ["whatsapp-lead", "ai-qualified", "score-9", "hot-lead"]}
```

**GoHighLevel: add note** (`POST /contacts/{id}/notes`)

```
Qualified on WhatsApp by the AI assistant. Score 9/10.
Jamie owns Rivera Roofing in Austin and is losing jobs to missed calls. Budget around $1,500 a month, wants to start ASAP.

Name: Jamie Rivera
Company: Rivera Roofing
Challenge: Missed calls, leads going cold
Budget: Around $1,500 a month
Timeline: ASAP
WhatsApp: +15553334444
```

**Slack: lead alert**

```
*New WhatsApp lead* (score 9/10, hot lead)

Name: Jamie Rivera
Company: Rivera Roofing
Challenge: Missed calls, leads going cold
Budget: Around $1,500 a month
Timeline: ASAP
WhatsApp: +15553334444
Open in GoHighLevel: https://app.gohighlevel.com/v2/location/mock-location/contacts/detail/contact-15553334444
```

**Slack: when GoHighLevel is down** (then retried and saved on the lead's next message)

```
*WhatsApp lead NOT saved to GoHighLevel* (score 6/10)
Error: 401 - "{\"statusCode\": 401, \"message\": \"Invalid JWT\"}"
The save is retried on their next message. To be safe, add them by hand.

Name: Lee Park
Company: Park Plumbing
Challenge: More Google reviews
Budget: Under $500 a month
Timeline: 1 to 3 months
WhatsApp: +15556667777
```

All names, companies and phone numbers above are made up.
