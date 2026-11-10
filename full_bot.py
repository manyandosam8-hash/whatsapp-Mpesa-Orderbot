"""
WhatsApp + M-Pesa Order Bot - full integration

Flow (event-driven, not linear - two independent entry points):

  /whatsapp route:
    Customer messages -> sees menu -> replies with item number
    -> order logged to Sheets as "Pending Payment"
    -> STK Push triggered to the CUSTOMER'S OWN number
    -> customer told to check their phone

  /mpesa/callback route (fires later, separately, when Safaricom responds):
    -> proactively send a WhatsApp confirmation via twilio.rest.Client
       (NOT MessagingResponse - that only replies within the original
       /whatsapp request, which has long since finished by this point)

Requires:
    pip install flask twilio requests google-auth-oauthlib google-api-python-client python-dotenv

Google Sheet needs a tab named "ORDERS" with header row:
    Timestamp | From | Item | Status | Receipt

.env needs:
    MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET, MPESA_SHORTCODE, MPESA_PASSKEY
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN


"""

import base64
import os
import re
import hmac
from datetime import datetime
from zoneinfo import ZoneInfo #specified for the Nairobi's timezone

from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from functools import wraps
from flask import abort
from twilio.request_validator import RequestValidator
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE_PATH", os.path.join(BASE_DIR, "service_account.json"))

SPREADSHEET_ID = os.getenv("SHEET_ID")
ORDERS_RANGE = "ORDERS!A:G"  # open-ended - no row cap
CHECKOUT_ID_COLUMN_INDEX = 6  # column G (0-indexed) is where we store CheckoutRequestID

MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE", "").strip()
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY", "").strip()


TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

REQUIRED_ENV = ["SHEET_ID", "MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET",
                "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_URL",
                "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_NUMBER"]
missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    raise RuntimeError("Missing environment variables: " + ", ".join(missing))

# Public HTTPS URL where Safaicom send the payment result,
#e.g https://your-service.onrender.com/mpesa/callback
PUBLIC_BASE_URL =os.getenv("PUBLIC_BASE_URL", "").strip("/")
MPESA_CALLBACK_SECRET = os.getenv("MPESA_CALLBACK_SECRET")\

MPESA_CALLBACK_URL = f"{PUBLIC_BASE_URL}/mpesa/callback/{MPESA_CALLBACK_SECRET}"




ORDER_ITEMS = {
    "1": ("Watches", 2000),
    "2": ("Headphones", 3500),
    "3": ("Ear pods", 3), #for testing purposes - will be removed
    "4": ("Smart Watches", 8000),
    "5": ("Speakers", 10000),
    "6": ("Power Banks", 1500), 
    "7": ("Phone Cases", 800),
    "8": ("Screen Protectors", 500),
    "9": ("Chargers", 1200),
    "10": ("Cables", 600),
}

app = Flask(__name__)
# Render terminates HTTPS at a proxy; this makes request.url report https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)

def require_twilio_signature(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        signature = request.headers.get("X-Twilio-Signature", "")
        if not twilio_validator.validate(request.url, request.form, signature):
            abort(403)
        return view(*args, **kwargs)
    return wrapped
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)




def get_credentials():
    
    return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)

_sheets_client = None

def get_sheets_service():
    '''Build the Sheets client on first use, then reuse it'''
    global _sheets_client
    if _sheets_client is None:
        creds = get_credentials()
        _sheets_client = build("sheets", "v4", 
                               credentials=get_credentials(),
                               cache_discovery=False)
    return _sheets_client

class _LazySheets:
    def __getattr__(self, name):
        return getattr(get_sheets_service(), name)

sheets_service = _LazySheets()



def build_order_items():
    lines = ["Welcome to Koja Electronics! Reply with a number to order:"]
    for key, (name, price) in ORDER_ITEMS.items():
        lines.append(f"{key}. {name} - KES {price}")
    return "\n".join(lines)


def log_pending_payment(from_number, item_name):
    """Appends a new order row and returns the row number it landed on,
    so update_sheet() can target that exact row once payment is confirmed."""
    timestamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y-%m-%d %H:%M:%S")
    body = {"values": [[timestamp, from_number, item_name, "Pending Payment", "", "","]]}"]]}
    result = sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=ORDERS_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()

    updated_range = result["updates"]["updatedRange"]  # e.g. "ORDERS!A5:E5"
    row_number = int(re.search(r"\d+", updated_range).group())
    return row_number



def save_checkout_id(row_number, checkout_id):
    '''Stores the CheckoutRequestID in column G of the orders sheet.'''
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"ORDERS!G{row_number}",
        valueInputOption="RAW",
        body={"values": [[checkout_id]]}
    ).execute()



def find_order_by_checkout_id(checkout_id):
    """Looks up the order row in the Sheet by CheckoutRequestID (column G).
    Returns {"row", "from_number", "item", "status"} or None if not found."""
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=ORDERS_RANGE
    ).execute()
    rows = result.get("values", [])
    
    #rows[0] is the header (sheet row 1), so data starts at row 2)
    for sheet_row, row in enumerate(rows[1:], start=2):  # skip header row
        if len(row) > CHECKOUT_ID_COLUMN_INDEX  and row[CHECKOUT_ID_COLUMN_INDEX] == checkout_id:
            return {
                "row": sheet_row,
                "from_number": row[1] if len(row) > 1 else "",
                "item": row[2] if len(row) > 2 else "",
                "status": row[3] if len(row) > 3 else "",
            }
    return None




def update_sheet(row_number, paid, receipt):
    status = "Paid" if paid else "Failed"
    body = {"values": [[status, receipt or ""]]}
    range_to_update = f"ORDERS!D{row_number}:E{row_number}"
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_to_update,
        valueInputOption="RAW",
        body=body
    ).execute()


def send_whatsapp_message(to_number, body_text):
    """to_number must already be in 'whatsapp:+254XXXXXXXXX' format (from_number
    as received from Twilio already looks like this)."""
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to_number,
        body=body_text
    )


def to_mpesa_phone_format(whatsapp_from):
    """Converts Twilio's 'whatsapp:+2547XXXXXXXXX' into Daraja's '2547XXXXXXXXX'."""
    return whatsapp_from.replace("whatsapp:", "").replace("+", "")


def get_access_token():
    api_url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    response = requests.get(api_url, auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET))
    response.raise_for_status()
    return response.json()["access_token"]


def generate_password(timestamp):
    raw = MPESA_SHORTCODE + MPESA_PASSKEY + timestamp
    return base64.b64encode(raw.encode()).decode("utf-8")


def stk_push(amount, phone_number, account_reference, transaction_desc):
    access_token = get_access_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = generate_password(timestamp)

    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": account_reference,
        "TransactionDesc": transaction_desc
    }

    url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    response = requests.post(url, json=payload, headers=headers)
    return response.json()


@app.route("/whatsapp", methods=["POST"])
@require_twilio_signature
def whatsapp_reply():
    incoming = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")

    resp = MessagingResponse()

    if incoming.lower() in ("hi", "hello", "menu", "start"):
        resp.message(build_order_items())

    elif incoming in ORDER_ITEMS:
        item_name, price = ORDER_ITEMS[incoming]
        try:
            row_number = log_pending_payment(from_number, item_name)
            mpesa_phone = to_mpesa_phone_format(from_number)
            stk_result = stk_push(price, mpesa_phone, f"Order{row_number}", item_name)

            checkout_id = stk_result.get("CheckoutRequestID")
            if checkout_id:
                save_checkout_id(row_number, checkout_id)
                resp.message(f"Order received: {item_name} (KES {price}). "
                              f"Check your phone for the M-Pesa prompt!")
            else:
                print(f'STK push did not return a CheckoutRequestID: {stk_result}')
                resp.message("Sorry, we couldn't start the payment request. Please try again.")
                
        except Exception as e:
            print(f"Order/payment failed: {e}")
            resp.message("Sorry, something went wrong placing your order. Please try again shortly.")

    else:
        resp.message("Sorry, I didn't understand that. Reply 'menu' to see options.")

    return str(resp)


MPESA_CALLBACK_SECRET = os.getenv("MPESA_CALLBACK_SECRET")
@app.route("/mpesa/callback/<secret>", methods=["POST"])
def mpesa_callback(secret):
    expected = os.getenv("MPESA_CALLBACK_SECRET", "")
    if not expected or not hmac.compare_digest(secret, expected):
        abort(404)
    ...
    #existing handler body unchaged
    data = request.get_json()
    print("M-Pesa callback : id={checkout_id}, result={result_code}")

    stk_callback = data.get("Body", {}).get("stkCallback", {})
    checkout_id = stk_callback.get("CheckoutRequestID")
    result_code = stk_callback.get("ResultCode")

    try:
        order = find_order_by_checkout_id(checkout_id) if checkout_id else None

        if not order:
            print(f"No matching order for CheckoutRequestID {checkout_id}")
        elif order["status"] != "Pending Payment":
            #safaricom can deliver a callback more than once; only act on the first.
            print(f"Order row {order['row']} already (status={order['status']}, ignoring repeated callback)")
    
        elif result_code == 0:
            items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            details = {item["Name"]: item.get("Value") for item in items}
            receipt = details.get("MpesaReceiptNumber", "")
            update_sheet(order["row"], paid=True, receipt=receipt)
            send_whatsapp_message(
                order["from_number"],
                f"Payment confirmed! Your {order['item']} order is on its way. Receipt: {receipt}"
        )
        else:
            update_sheet(order["row"], paid=False, receipt="")
            send_whatsapp_message(
                order["from_number"],
                f"Payment for {order['item']} wasn't completed ({stk_callback.get('ResultDesc')}). "
                f"Reply 'menu' to try again."
        )

    except Exception as e:
        #Always acknowledge Safaricom, even if ou side failed; the error is in the logs.
        print(f"Callback processing failed for {checkout_id}: {e}")    
    
    return jsonify({"ResultCode": 0, "ResultDesc": "Callback received successfully"})


if __name__ == "__main__":
    app.run(port=int(os.getenv('PORT', 5000)))