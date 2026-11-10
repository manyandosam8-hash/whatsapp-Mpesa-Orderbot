# WhatsApp + M-Pesa Order Bot

Customers order on WhatsApp, pay with an M-Pesa STK Push, and the order lands in a Google Sheet with a receipt sent back to them.

> **Status: sandbox demo.** Runs on the Twilio WhatsApp sandbox and the Safaricom
> Daraja sandbox. Production use needs a verified WhatsApp sender and Daraja Go-Live
> on the business's own paybill or till.



## What it does
- Takes orders by chat (demo shop: "Demo Electronics")
- Sends an STK Push payment prompt and confirms payment from the M-Pesa callback
- Logs every order to Google Sheets (Pending -> Paid) and sends a WhatsApp receipt
- Stores the `CheckoutRequestID` on the order row, so pending payments survive restarts

## How it works
```
Customer (WhatsApp) -> Twilio webhook -> Flask bot -> Daraja STK Push
        ^                                                   |
        |                                      M-Pesa callback
  WhatsApp receipt <---- Google Sheet row updated <---------+
```

## Security
- Twilio webhook requests are verified with `X-Twilio-Signature`
- The M-Pesa callback lives on a secret URL path and only marks an order paid when
  its `CheckoutRequestID` matches a row the bot wrote
- No secrets in code or history; configuration comes from environment variables
- Startup fails with a clear message if a required variable is missing

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in the values
3. Create a Google service account, enable the Sheets API, and share your sheet with
   its email address
4. Run locally: `python full_bot.py` (listens on port 5000; set `PORT` to change)
5. Expose it with HTTPS (a host such as Render, or a tunnel while developing)
6. Set the Twilio sandbox webhook to `<PUBLIC_BASE_URL>/whatsapp`

### Environment variables
| Variable | Purpose |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio credentials |
| `TWILIO_WHATSAPP_NUMBER` | Sender, as `whatsapp:+...` |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET` | Daraja app credentials |
| `MPESA_SHORTCODE`, `MPESA_PASSKEY` | Daraja sandbox shortcode and passkey |
| `PUBLIC_BASE_URL` | Public HTTPS root of the deployed bot |
| `MPESA_CALLBACK_SECRET` | Random string in the callback URL path |
| `SHEET_ID` | Google Sheet used for orders |
| `SERVICE_ACCOUNT_FILE_PATH` | Path to the service account JSON |

## Deploy (Render)
Start command: `gunicorn full_bot:app`. Set the variables above in the service's
Environment tab and mount the service account file as a secret file.

## Limits and next steps
- Sandbox only. Production needs the client's own WhatsApp sender (Meta business
  verification and approved templates) and Daraja Go-Live on their paybill or till
- Google Sheets is the datastore; a database is the next step for volume

## Stack
Python, Flask, Twilio, Safaricom Daraja, Google Sheets API