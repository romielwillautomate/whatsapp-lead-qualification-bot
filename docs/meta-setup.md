# Meta and WhatsApp setup

The bot uses the WhatsApp Cloud API, which Meta hosts. You need a Meta developer app with the WhatsApp product added. On Meta's pricing at the time of writing, replies sent within 24 hours of a customer's message (which is all this bot sends) are free; you only pay for template messages you start yourself.

## 1. Create the app

1. Go to [developers.facebook.com](https://developers.facebook.com), open **My Apps** and create an app of type **Business**.
2. Add the **WhatsApp** product to the app.
3. Open **WhatsApp → API Setup**. Meta gives you a free test number. Copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`.
4. While testing, add your own phone under **To** as an allowed recipient, so the test number can message you.

## 2. Get the keys

- **Access token** (`WHATSAPP_TOKEN`): the token on the API Setup page works for testing but expires after 24 hours. For production, go to **Business Settings → Users → System users**, create a system user, assign it the app and your WhatsApp account, and generate a token with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions. That token does not expire.
- **App secret** (`WHATSAPP_APP_SECRET`): **App settings → Basic → App secret**. Meta signs every webhook with it, and the bot rejects anything that is not signed with it.
- **Verify token** (`WHATSAPP_VERIFY_TOKEN`): make up a long random string. You will paste the same value into Meta in step 4.

## 3. Prepare n8n

1. Add the variables from the README to your n8n environment (or `.env` file) and restart n8n.
2. Import `workflow/whatsapp-lead-qualification-bot.json` and, for alerts, `workflow/error-alert-slack.json`.
3. Activate the error alert workflow, then choose it as the **Error Workflow** in the bot's workflow settings. n8n 2.x skips an error workflow that is switched off, so it must be active.
4. Activate the bot workflow.

Meta needs to reach n8n over HTTPS. A hosted n8n works as is. A local n8n needs a tunnel such as Cloudflare Tunnel or ngrok.

## 4. Connect the webhook

1. In the Meta app, open **WhatsApp → Configuration** and edit the webhook.
2. **Callback URL:** `https://YOUR-N8N-DOMAIN/webhook/whatsapp-lead-bot`
3. **Verify token:** the value of `WHATSAPP_VERIFY_TOKEN`.
4. Click **Verify and save**. Meta sends a GET request to the callback URL, and the "Meta webhook check" nodes answer with the challenge. If verification fails, check that the workflow is active and that the token matches exactly.
5. Under **Webhook fields**, subscribe to **messages**.

The same URL handles both requests: GET for this one time check, POST for every message after that.

## 5. Try it

Send a WhatsApp message from your phone to the test number. Within a few seconds you should get the first question back. Answer the five questions, and the lead appears in GoHighLevel with its tags and note, followed by the Slack alert.

If nothing comes back, open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| No execution at all | The webhook is not subscribed to **messages**, or Meta cannot reach your n8n URL |
| Run stopped at "Stop: Bad Signature" | `WHATSAPP_APP_SECRET` is missing or wrong |
| Run stopped at "Stop: AI Failed" | Check `ANTHROPIC_API_KEY`, your credit balance and `ANTHROPIC_MODEL` |
| Run stopped at "Stop: Reply Failed" | `WHATSAPP_TOKEN` expired (the temporary one lasts 24 hours) or `WHATSAPP_PHONE_NUMBER_ID` is wrong |
| Slack says "NOT saved to GoHighLevel" | Check `GHL_API_KEY`, its scopes (View Contacts and Edit Contacts) and `GHL_LOCATION_ID` |

## Moving to your real number

When you are ready to go live, add your business phone number under **WhatsApp → API Setup**, verify it, and update `WHATSAPP_PHONE_NUMBER_ID`. Meta also asks you to add a payment method and complete business verification before you can message customers at scale.
