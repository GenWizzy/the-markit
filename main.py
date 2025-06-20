import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore
from flask import Flask, request
from flask import Flask, request, jsonify
from google.cloud import firestore
import requests
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, Updater
import threading
import google.cloud.firestore
import os
import json
import hmac
import hashlib
from paystackapi.transfer import Transfer
from nanoid import generate #import uuid
import base64
import nanoid
from difflib import get_close_matches
import difflib
from telegram.ext import Updater, MessageHandler, CallbackQueryHandler
from telegram.ext import filters
from datetime import datetime, timedelta
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import asyncio
from uuid import uuid4
import datetime
import logging
import uuid
import random
from apscheduler.schedulers.background import BackgroundScheduler
import html
from telegram import Bot
from dotenv import load_dotenv



# Load environment variables from the .env file
load_dotenv()

# Retrieve the values
FLW_SECRET_KEY = os.getenv('FLW_SECRET_KEY')
FLW_PUBLIC_KEY = os.getenv('FLW_PUBLIC_KEY')
bot_token = os.getenv('BOT_TOKEN')
webhook_secret = os.getenv('WEBHOOK_SECRET')
NEWS_API_KEY = os.getenv('NEWS_API_KEY')
GOOGLE_CLOUD_PROJECT = os.getenv('GOOGLE_CLOUD_PROJECT')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')
ngrok_url = os.getenv('NGROK_URL')

SERVER_URL = os.getenv('SERVER_URL') or os.getenv('NGROK_URL')


# Retrieve the raw JSON string for the Firebase service account
raw_service_account = os.getenv("FIREBASE_SERVICE_ACCOUNT")


if not raw_service_account:
    raise ValueError("FIREBASE_SERVICE_ACCOUNT is not set.")

# Convert literal "\n" sequences to actual newlines
fixed_service_account = raw_service_account.encode('utf-8').decode('unicode_escape')


# Parse the JSON to get the service account info
service_account_info = json.loads(fixed_service_account)

# Initialize Firebase credentials
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)

db = firestore.client()




# Global variables:
previous_message_id = None
active_payment = {}  # This dictionary maps chat_id to some state (or simply True) when a payment is active.
sale_mapping = {}
test_mode = False
TIMEOUT = 10  # Timeout for HTTP requests in seconds
shown_products = set()
NEWS_API_URL = "https://newsapi.org/v2/everything"

app = Flask(__name__)
bot = Bot(token=bot_token)
TELEGRAM_API_URL = f"https://api.telegram.org/bot{bot_token}"
CHANNEL_ID = -1002442941388  # Replace with your actual channel ID
TIMEOUT = 10  # Increase timeout value to 10 seconds




current_read_count = 0
current_write_count = 0
current_delete_count = 0

# Initialize variables to None
current_category = None
current_location = None
current_price_range = None
current_actual_price = None
current_delivery_price = None

bot_requesting_price = False
bot_requesting_delivery_amount = False
bot_requesting_photo = False
bot_requesting_price = False
bot_requesting_delivery_amount = False

# Global dictionaries:
pending_listings = {}         # For storing sellers’ pending product listings.
pending_payment_info = {}     # For storing buyer-specific payment information (e.g., product ID, email, phone).
pending_admin_denials = {}    # key: admin_chat_id, value: tuple (lister_chat_id, timestamp)
user_data = {}
active_chats = {}             # Global dictionary to track active chat sessions.
# Use the user’s Telegram ID as key and a dict containing session info and partner’s ID.
pending_onboarding = {}
support_sessions = {}       # Key: user_id -> { 'user_chat_id': chat_id, 'active': True, 'history': [] }
admin_pending_reply = {}    # Key: admin ID (e.g., ADMIN_CHAT_ID) -> target user_id
pending_purchases = {}

# Global in‑memory dictionary to track aggregate report counts. Keys are reported usernames:

user_reports = {}  # e.g., { "seller_username": 2, "buyer_username": 1 }
user_report_tracker = {}  # e.g., { ("buyer1", "seller1"): True }
user_blocks = {}
user_recommendations = {}    # e.g., { "user_id": recommendation_count, ... }
user_recommend_tracker = {}  # e.g., { (recommender_id, recommended_id): True, ... }

# Constants and configuration variables
MAX_MESSAGE_LENGTH = 4096  # Telegram's message character limit
MAX_CAPTION_LENGTH = 1024  # Maximum length for photo captions


# Telegram Webhook
@app.route(f'/{bot_token}', methods=['POST'])
def receive_update():
    update = request.get_json()
    print(f"Received update: {update}")
    handle_update(update)
    return "OK"

# Create a Payment Endpoint
@app.route('/initialize_payment', methods=['POST'])
def initialize_payment():
    data = request.json
    response = Transaction.initialize(
        reference=data['reference'],
        amount=data['amount'],
        email=data['email']
    )
    return jsonify(response)

# Verify Payment Endpoint
@app.route('/verify_payment/<reference>', methods=['GET'])
def verify_payment(reference):
    response = Transaction.verify(reference)
    return jsonify(response)


def extract_meta_value(meta, key):
    """
    Extracts a meta value given the meta payload and key.
    Supports both dictionary and list formats.
    """
    if isinstance(meta, dict):
        return meta.get(key)
    elif isinstance(meta, list):
        for item in meta:
            if item.get("metaname") == key:
                return item.get("metavalue")
    return None

# 🔸 Updated Flutterwave Webhook Handler
@app.route('/webhook', methods=['POST'])
def flutterwave_webhook():
    try:
        # Debug: log headers and raw payload.
        print("DEBUG: Headers received:", dict(request.headers))
        raw_payload = request.data
        print("DEBUG: Raw payload received by server:", raw_payload)

        if not raw_payload:
            print("DEBUG: Empty payload received.")
            return jsonify({"status": "error", "message": "Empty payload"}), 400

        # Normalize and parse JSON.
        raw_payload = raw_payload.replace(b"\r", b"").replace(b"\n", b"")
        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError as json_err:
            print(f"DEBUG: Failed to parse JSON payload: {json_err}")
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400

        print("DEBUG: Parsed webhook payload:", json.dumps(data, indent=2))

        # Verify webhook signature.
        secret_hash =   webhook_secret
        signature = request.headers.get("verifi-hash") or request.headers.get("verif-hash")
        if not signature or signature != secret_hash:
            print(f"DEBUG: Invalid webhook signature. Received: {signature}, Expected: {secret_hash}")
            return jsonify({"status": "error", "message": "Unauthorized"}), 403

        # Extract payload components.
        data_part = data.get("data", {})
        # Try to retrieve meta from either "meta" or "meta_data".
        meta = data.get("meta") or data.get("meta_data") or {}
        # Some payloads use "event.type" instead of "event".
        event = data.get("event") or data.get("event.type")

        # Extract values using the helper.
        product_id = extract_meta_value(meta, "product_id")
        buyer_chat_id = extract_meta_value(meta, "buyer_chat_id")

        # Debug print the meta field and extracted values.
        print(f"DEBUG: Extracted meta: {meta}")
        print(f"DEBUG: product_id: {product_id}, buyer_chat_id: {buyer_chat_id}")

        # Process only if payment is complete and successful.
        if event == "charge.completed" and (data_part.get("status", "").lower().strip() == "successful"):
            official_txid = data_part.get("id")
            reference = data_part.get("tx_ref")
            flw_ref = data_part.get("flw_ref")
            sale_id = None

            if product_id:
                try:
                    # Retrieve product details from Firestore.
                    product_doc_ref = db.collection('products').document(product_id)
                    product_doc = product_doc_ref.get()
                    if product_doc.exists:
                        product_data = product_doc.to_dict()
                        product_id_short = product_data.get("short_product_id")
                        if not product_id_short:
                            product_id_short = generate_short_product_id()
                            product_doc_ref.update({"short_product_id": product_id_short})
                            print(f"DEBUG: Generated and updated short_product_id: {product_id_short} for product: {product_id}")
                    else:
                        print(f"DEBUG: Product document {product_id} does not exist.")
                        return jsonify({"status": "error", "message": "Product not found"}), 404

                    # Search for the sale record by payment reference.
                    print(f"DEBUG: Looking for sale record with payment_reference: {reference}")
                    sales_query = (db.collection('products').document(product_id)
                                   .collection('sales')
                                   .where("payment_reference", "==", reference)
                                   .limit(1))
                    sales = list(sales_query.stream())

                    if sales:
                        sale_doc = sales[0]
                        sale_data = sale_doc.to_dict()
                        if sale_data.get("status", "").lower() not in ["settled", "released"]:
                            sale_doc.reference.update({
                                "txid": official_txid,
                                "status": "waiting_for_release",
                                "flw_ref": flw_ref
                            })
                            sale_id = sale_doc.id
                            print(f"DEBUG: Updated sale record {sale_id} with txid: {official_txid}, status 'waiting_for_release', flw_ref: {flw_ref}")

                            # Save mapping.
                            mapping_doc_ref = db.collection('sale_mapping').document(product_id_short)
                            existing_mapping = mapping_doc_ref.get().to_dict() if mapping_doc_ref.get().exists else {}
                            sale_index = str(len(existing_mapping))
                            existing_mapping[sale_index] = sale_id
                            mapping_doc_ref.set(existing_mapping)
                            print(f"DEBUG: Saved sale_mapping -> {product_id_short} -> {sale_index} = {sale_id}")

                            # Mark the payment as complete for the buyer.
                            buyer_key = str(buyer_chat_id)
                            if buyer_key in pending_payment_info:
                                pending_payment_info[buyer_key]['payment_complete'] = True
                                if 'action_initiated' not in pending_payment_info[buyer_key]:
                                    pending_payment_info[buyer_key]['action_initiated'] = False
                                pending_payment_info[buyer_key]['product_id_short'] = product_id_short
                                pending_payment_info[buyer_key]['sale_index'] = sale_index
                            else:
                                print("DEBUG: buyer_chat_id not in pending_payment_info, so adding new entry.")
                                pending_payment_info[buyer_key] = {
                                    "product_id": product_id,
                                    "payment_complete": True,
                                    "action_initiated": False,
                                    "product_id_short": product_id_short,
                                    "sale_index": sale_index
                                }

                            # Send follow-up message to buyer with inline buttons.
                            followup_keyboard = {
                                "inline_keyboard": [
                                    [{"text": "Release Funds", "callback_data": f"rf_{product_id_short}_{sale_index}"}],
                                    [{"text": "Request Refund", "callback_data": f"rr_{product_id_short}_{sale_index}"}]
                                ]
                            }
                            send_message_with_keyboard(buyer_chat_id,
                                                       "Please choose an option below to proceed:",
                                                       followup_keyboard)

                            # Seller Notification Section.
                            if not sale_data.get("seller_notified", False):
                                send_seller_notification(sale_doc.id, product_id)
                                sale_doc.reference.update({"seller_notified": True})
                        else:
                            print(f"DEBUG: Sale record {sale_doc.id} already marked as 'settled' or 'released'")
                    else:
                        print(f"DEBUG: No matching sale record found for payment_reference: {reference}")
                        return jsonify({"status": "error", "message": "Sale not found"}), 404

                except Exception as e:
                    print(f"DEBUG: Firestore error: {e}")
                    return jsonify({"status": "error", "message": "Database error"}), 500
            else:
                print("DEBUG: No product_id found in meta data.")
                return jsonify({"status": "error", "message": "Missing product_id"}), 400

        return jsonify({"status": "success", "message": "Webhook processed."}), 200

    except Exception as e:
        print(f"DEBUG: Error handling webhook: {e}")
        return jsonify({"status": "error", "message": "Internal server error."}), 500




# 🔹 Telegram Webhook Handler
@app.route(f'/{bot_token}', methods=['POST'])
def telegram_webhook():
    try:
        update_json = request.get_json(force=True)
        print("DEBUG: Telegram update received:")
        print(json.dumps(update_json, indent=2))

        # Convert JSON update into a Telegram Update object.
        update = Update.de_json(update_json, application.bot)

        # Process the update synchronously.
        application.process_update(update)
        return jsonify({"ok": True})
    except Exception as e:
        print("DEBUG: Exception in webhook:", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def handle_update(update):
    """Routes incoming updates to the appropriate handler."""
    print(f"DEBUG: Handling update: {update}")
    if 'message' in update:
        handle_message(update['message'])
    elif 'inline_query' in update:
        handle_inline_query(update['inline_query'])
    elif 'callback_query' in update:
        handle_callback_query(update['callback_query'])
    else:
        print("DEBUG: Update does not match any known type; skipping.")


def handle_inline_query(inline_query):
    """Handles inline queries sent by users."""
    query_id = inline_query['id']
    user_id = inline_query['from']['id']
    query = inline_query['query']
    print(f"DEBUG: Handling inline query from user_id {user_id}: {query}")

    # Example response structure
    results = [
        {
            "type": "article",
            "id": "1",
            "title": "List Products",
            "input_message_content": {"message_text": "List Products"},
            "description": "List your products here."
        },
        {
            "type": "article",
            "id": "2",
            "title": "Browse Products",
            "input_message_content": {"message_text": "Browse Products"},
            "description": "Browse available products."
        }
    ]

    url = f"{TELEGRAM_API_URL}/answerInlineQuery"
    payload = {"inline_query_id": query_id, "results": results}
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print(f"DEBUG: Inline query response sent successfully: {response.json()}")
        else:
            print(f"DEBUG: Failed to send inline query response: {response.text}")
    except Exception as e:
        print(f"DEBUG: Error handling inline query: {e}")



def generate_short_product_id(length=8):
    """
    Generates a short product ID using NanoID.

    Args:
        length (int): The desired length of the generated ID (default is 8).

    Returns:
        str: A randomly generated ID string of the specified length.
    """
    return generate(size=length)




def delete_old_message(chat_id, message_id):
    """Deletes old inline keyboard messages if needed."""
    if message_id is None:
        print("DEBUG: No previous message id provided; skipping deletion.")
        return
    url = f"{TELEGRAM_API_URL}/deleteMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            print(f"DEBUG: Deleted old message successfully: {response.json()}")
        else:
            print(f"DEBUG: Failed to delete old message: {response.text}")
    except Exception as e:
        print(f"DEBUG: Error deleting message: {e}")


def forward_message(partner_id, text, sender_id, session_id, sender_name):
    # Format the forwarded text.
    formatted_text = f"Message from {sender_name}: {text}"
    # Only attach the end chat button if there's non-empty text.
    reply_markup = None
    if text:  # Only attach if text isn't empty.
        reply_markup = {
            "inline_keyboard": [
                [{"text": "End Chat", "callback_data": f"end_chat:{session_id}"}]
            ]
        }
    send_message(partner_id, formatted_text, reply_markup=reply_markup)
    print(f"DEBUG: Forwarded message from {sender_id} ({sender_name}) to {partner_id}: {text}")




def handle_message(message):
    global admin_pending_reply, pending_listings, support_sessions
    chat_id = str(message['chat']['id'])
    sender_id = str(message['from']['id'])
    text = message.get('text', '').strip()
    user_id = str(message['from']['id'])
    print(f"DEBUG: Received message from sender_id={sender_id}, chat_id={chat_id}. admin_pending_reply={admin_pending_reply}")

    # Check if user is blocked using their user_id.
    is_blocked, unblock_ts = is_user_blocked(user_id)
    if is_blocked:
        support_keyboard = {
            "inline_keyboard": [
                [{"text": "Contact Support", "callback_data": "contact_support"}]
            ]
        }
        send_message_with_keyboard(
            chat_id,
            f"Hi @{user_username}, your account is suspended until {datetime.datetime.fromtimestamp(unblock_ts)}. "
            "If you think this is an error, please contact support.",
            support_keyboard
        )
        return

    # Extract sender's name.
    sender_info = message.get('from', {})
    sender_first = sender_info.get('first_name', '')
    sender_last = sender_info.get('last_name', '')
    sender_name = sender_first
    if sender_last:
        sender_name += " " + sender_last

    # --- Active Chat Forwarding ---
    if sender_id in active_chats:
        session_info = active_chats[sender_id]
        partner_id = session_info.get("partner_id")
        session_id = session_info.get("session_id")
        if partner_id and session_id:
            forward_message(partner_id, text, sender_id, session_id, sender_name)
            return

    # --- ADMIN CUSTOM DENIAL FLOW ---
    if chat_id in pending_admin_denials:
        (lister_chat_id, timestamp) = pending_admin_denials.pop(chat_id)
        deny_product(lister_chat_id, timestamp, custom_message=text)
        send_message(chat_id, "Custom denial message sent to the lister.")
        return

    # --- ADMIN Reply Handling ---
    if sender_id == str(ADMIN_CHAT_ID):
        print(f"DEBUG: In admin reply handling. admin_pending_reply = {admin_pending_reply}")
        if sender_id in admin_pending_reply:
            target_user = admin_pending_reply[sender_id]
            if target_user in support_sessions:
                support_sessions[target_user]["history"].append(f"Admin: {text}")
                user_chat_id = support_sessions[target_user]["user_chat_id"]
                send_message(user_chat_id, f"Admin: {text}")
                send_message(ADMIN_CHAT_ID, f"Replied to user {target_user}.")
            else:
                send_message(ADMIN_CHAT_ID, f"No active support session for user {target_user}.")
            # Optionally, delete the pending reply record.
            # del admin_pending_reply[sender_id]
            return

    # --- Handling "Get Product" Pending State ---
    if chat_id in pending_listings:
        current_state = pending_listings[chat_id].get("state", "")
        if current_state == "awaiting_product_id":
            product_id_input = text
            get_product_by_id(chat_id, product_id_input)
            pending_listings.pop(chat_id, None)
            return

        # --- User Support Message Handling ---
        if current_state == "awaiting_support_message":
            if sender_id not in support_sessions:
                support_sessions[sender_id] = {"user_chat_id": chat_id, "active": True, "history": []}
            support_sessions[sender_id]["history"].append(f"User: {text}")
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "Reply", "callback_data": f"admin_reply_{sender_id}"},
                        {"text": "End Chat", "callback_data": f"admin_end_{sender_id}"},
                        {"text": "Show History", "callback_data": f"show_history_{sender_id}"}
                    ]
                ]
            }
            admin_message = (
                f"Support session started with user {sender_id}.\n\n"
                f"Initial message: {text}\n\n"
                "Use the buttons below to reply/end the session/show conversation history."
            )
            send_message_with_keyboard(ADMIN_CHAT_ID, admin_message, keyboard)
            send_message(chat_id, "Your support message has been sent. An admin will reply shortly.")
            pending_listings.pop(chat_id, None)
            return

    # --- Active Support Session Handling ---
    if sender_id in support_sessions and support_sessions[sender_id]["active"]:
        support_sessions[sender_id]["history"].append(f"User: {text}")
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Reply", "callback_data": f"admin_reply_{sender_id}"},
                    {"text": "End Chat", "callback_data": f"admin_end_{sender_id}"},
                    {"text": "Show History", "callback_data": f"show_history_{sender_id}"},
                    {"text": "Unblock", "callback_data": f"admin_unblock_{sender_id}"}
                ]
            ]
        }
        forward_text = f"Message from user {sender_id}: {text}"
        send_message_with_keyboard(ADMIN_CHAT_ID, forward_text, keyboard)
        return

    # --- Pending Payment Reminder for Buyers ---
    # If the buyer has completed payment (so payment_complete is True) but hasn't taken action (action_initiated is False),
    # then send a reminder with the Release Funds/Request Refund buttons.
    if chat_id in pending_payment_info:
        buyer_info = pending_payment_info[chat_id]
        if buyer_info.get("payment_complete") and not buyer_info.get("action_initiated", False):
            product_id_short = buyer_info.get("product_id_short")
            sale_index = buyer_info.get("sale_index")
            reminder_message = (
                "Reminder: To continue the process, please use the buttons below to either release funds or request a refund."
            )
            reminder_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "Release Funds", "callback_data": f"rf_{product_id_short}_{sale_index}"},
                        {"text": "Request Refund", "callback_data": f"rr_{product_id_short}_{sale_index}"}
                    ]
                ]
            }
            send_message_with_keyboard(chat_id, reminder_message, reminder_keyboard)
            return

    # --- BUYER PAYMENT FLOW ---
    if chat_id in pending_payment_info:
        product_info = pending_payment_info[chat_id]

        # Step 1: If buyer's phone hasn't been captured yet, treat this as the phone number.
        if "buyer_phone" not in product_info:
            # Validate that the input only contains digits.
            if not text.isdigit():
                send_message(chat_id, "Invalid input. Please enter a valid phone number that contains only digits:")
                return
            product_info["buyer_phone"] = text
            print(f"DEBUG: Received phone number {product_info['buyer_phone']} from buyer {chat_id}")
            # Prompt for email next.
            send_message(chat_id, "Thank you! Now, please enter your email address for your payment receipt:")
            return

        # Step 2: If the phone number is already captured and email is missing, treat this as the email.
        if "email" not in product_info:
            product_info["email"] = text
            product_id = product_info.get("product_id")
            print(f"DEBUG: Received email {product_info['email']} from buyer {chat_id} for product {product_id}")
            # Initiate the escrow payment using the provided email.
            initiate_escrow_payment(chat_id, product_id, product_info["email"])
            return



    # --- SELLER ONBOARDING FLOW ---
    # If the sender is in an onboarding flow, delegate message handling to the onboarding handler.
    if sender_id in pending_onboarding:
        if handle_onboarding_message(message):
            # Message handled by onboarding; do not process further.
            return

    # --- SELLER LISTING FLOW ---

    if chat_id in pending_listings:
        listing = pending_listings[chat_id]
        current_state = listing.get("state", "")
        print(f"DEBUG: Listing state for chat_id {chat_id} is {current_state}")

        if current_state == "awaiting_actual_price":
            try:
                actual_price = float(text.replace(',', ''))
                if actual_price < 500:
                    send_message(chat_id, "🚫 Actual price must be at least 500. Please enter a valid price.")
                    return
                listing["actual_price"] = actual_price
                listing["state"] = "awaiting_delivery_cost"
                send_message(chat_id, "💰 Please enter the delivery cost of your product within the state:")
            except ValueError:
                send_message(chat_id, "🚫 Invalid actual price. Please enter a valid number.")
            return

        elif current_state == "awaiting_delivery_cost":
            try:
                delivery_price = float(text.replace(',', ''))
            except Exception as e:
                send_message(chat_id, f"🚫 Invalid delivery cost. Please enter a valid number. ({e})")
                return

            listing["delivery_price"] = delivery_price
            try:
                actual_price = listing["actual_price"]
                total_price = actual_price + delivery_price
                listing["total_price"] = total_price

                # Flutterwave fees:
                # Base fee: 2% of the total price.
                base_fee = total_price * 0.02
                # VAT: 7.5% of that base fee.
                vat = base_fee * 0.075
                flutterwave_fee = base_fee + vat

                # Stamp Duty: ₦50 for transactions of ₦10,000 or more.
                stamp_duty = 50 if total_price >= 10000 else 0

                # Total Flutterwave deductions.
                total_flutterwave_deductions = flutterwave_fee + stamp_duty

                # Net settlement after Flutterwave deductions.
                net_settlement = total_price - total_flutterwave_deductions

                # Split the net settlement:
                # 5% goes to you (the store owner)
                store_fee = net_settlement * 0.05
                # 95% goes to the seller (the subaccount)
                seller_net = net_settlement - store_fee

            except Exception as e:
                send_message(chat_id, f"🚫 Error calculating fees: {e}")
                return

            msg = (
                "For Local Transactions:\n"
                "-------------------------------------\n"
                "• Flutterwave fees are calculated as follows:\n"
                "   - Transaction Fee: 2% of the Total Price\n"
                "   - VAT: 7.5% on the Transaction Fee\n"
                "   - Stamp Duty: ₦50 for transactions ≥ ₦10,000\n\n"
                "• After these fees, the remaining (Net Settlement) is split as:\n"
                "   - 5% to TheMarkit (that’s for me!😊)\n"
                "   - 95% to the Seller (that's for you!😊)\n"
                "-------------------------------------\n"
                "Your product details:\n"
                f"💲 Actual Price: ₦{actual_price:,.2f}\n"
                f"🚚 Delivery Price: ₦{delivery_price:,.2f}\n"
                f"💵 Total Price: ₦{total_price:,.2f}\n"
                f"💳 Flutterwave Fee (2% + VAT): ₦{flutterwave_fee:,.2f}\n"
                f"💳 Stamp Duty: ₦{stamp_duty:,.2f}\n"
                f"🧾 Net Settlement after Flutterwave Fees: ₦{net_settlement:,.2f}\n"
                f"🔖 TheMarkit Fee (5% of Net Settlement): ₦{store_fee:,.2f}\n"
                f"💰 Seller's Amount (95% of Net Settlement): ₦{seller_net:,.2f}\n\n"
                "Do you accept these values?"
            )
            listing["state"] = "confirm_details"
            keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ Accept", "callback_data": f"accept_details_{chat_id}"}],
                    [{"text": "📝 Re-enter values", "callback_data": f"reenter_details_{chat_id}"}]
                ]
            }
            send_message_with_keyboard(chat_id, msg, keyboard)
            return

        elif current_state == "confirm_details":
            send_message(chat_id, "⏳ Please use the provided buttons to confirm or re-enter your values.")
            return



        elif current_state == "awaiting_photo":
            if "photo" in message:
                return handle_photo_message(message)
            else:
                send_message(chat_id,
                             "🚫 Please upload a photo of your product with a caption as the product name. "
                             "Other formats are not allowed. (this caption will be stored as your product name)")
            return


        elif current_state == "awaiting_description":
            # Here the seller types their product description.
            listing["description"] = text.strip()
            listing["state"] = "awaiting_phone_number"
            send_message(chat_id, "✅ Description saved! Now please enter your phone number for internal records.")
            return




        elif current_state == "awaiting_phone_number":
            phone_number = text.strip()
            if not phone_number.isdigit() or len(phone_number) < 10:
                send_message(chat_id, "🚫 Invalid phone number. Please enter a valid phone number:")
                return

            listing["phone_number"] = phone_number
            db.collection("sellers").document(chat_id).set({
                "phone_number": phone_number,
                "updated_at": int(time.time())
            }, merge=True)

            timestamp = int(time.time())
            product_id = generate(size=10)  # NanoID generation for a unique product ID
            print(f"DEBUG: Generated product ID: {product_id}")

            data = {
                'product_id': product_id,
                'category': listing.get("category"),
                'location': listing.get("location"),
                'price_range': listing.get("price_range"),
                'actual_price': listing.get("actual_price"),
                'delivery_price': listing.get("delivery_price"),
                'total_price': listing.get("total_price"),
                'seller_subaccount': listing.get("seller_subaccount", "Already Onboarded"),
                'phone_number': listing.get("phone_number"),
                'product_name': listing.get("product_name", "No product name provided."),  # New field from caption
                'description': listing.get("description", ""),  # Detailed description (if provided separately)
                'photo_file_id': listing.get("photo_file_id"),
                'chat_id': chat_id,
                'timestamp': timestamp
            }

            print(f"DEBUG: Product data to store: {data}")

            db.collection('pending_products').document(product_id).set(data)
            print(f"DEBUG: Product stored in 'pending_products' with ID: {product_id}")

            keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ Approve", "callback_data": f"approve_{chat_id}_{product_id}"}],
                    [{"text": "❌ Deny", "callback_data": f"deny_{chat_id}_{product_id}"}],
                    [{"text": "📝 Deny with text", "callback_data": f"denytext_{chat_id}_{product_id}"}]
                ]
            }
            send_photo(ADMIN_CHAT_ID, listing.get("photo_file_id"), caption=f"Approval needed: {data}")
            send_message_with_keyboard(
                ADMIN_CHAT_ID,
                "Approve or deny the product listing using the buttons below:",
                keyboard
            )
            send_message(
                chat_id,
                "Your product listing has been submitted for approval. Once approved, your product will be live for 72 hours."
            )
            pending_listings.pop(chat_id, None)
            show_main_menu(chat_id)
            return

        elif current_state == "awaiting_admin_approval":
            send_message(chat_id,
                         "Your product is awaiting admin approval. Please wait while an admin reviews your listing.")
            return

        else:
            # If no valid state is found, show the main menu.
            show_main_menu(chat_id)
            return

    # --- ACTIVE PAYMENT FLOW (if any) ---
    if chat_id in active_payment:
        print(f"DEBUG: Chat {chat_id} is in an active payment process. Not sending default menu.")
        return

    # --- OTHER COMMANDS / DEFAULT HANDLING ---
    if text.startswith("/start"):
        # A catchy, concise welcome message relating to buying and selling.
        welcome_text = (
            "🚀 Welcome to TheMarkitBot!\n"
            "Buy & Sell with ease - where smart commerce clicks! 🛒💰\n\n"
            "👉 Please tap Start to continue."
        )
        # Create an inline keyboard with one Start button.
        keyboard = [[InlineKeyboardButton("Start", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Use the global 'bot' to send message asynchronously.
        coro = bot.send_message(chat_id=chat_id, text=welcome_text, reply_markup=reply_markup)
        future = asyncio.run_coroutine_threadsafe(coro, async_loop)
        try:
            future.result(timeout=10)
        except Exception as e:
            print(f"Error sending welcome message: {e}")
    else:
        send_start_button(chat_id)

    if chat_id in ["6014538461", "6334679159"]:
        print(f"DEBUG: handle_message ended for chat_id: {chat_id}")






def handle_channel_post(channel_post):
    chat_id = channel_post['chat']['id']
    text = channel_post.get('text', '')

    # Debugging: Track channel post handling for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: handle_channel_post called for chat_id: {chat_id}, text: {text}")

    # Handle commands intended to be private
    if text.startswith('/send_to_channel ') and chat_id == ADMIN_CHAT_ID:
        # Extract the message after the command
        channel_message = text.replace('/send_to_channel ', '', 1)
        if send_message_to_channel(channel_message):
            return  # Do not resend the start menu if the message was successfully sent to the channel

    # Handle media posts intended to be private
    elif 'photo' in channel_post and chat_id != CHANNEL_ID:
        handle_photo_message(channel_post)
    elif 'video' in channel_post and chat_id != CHANNEL_ID:
        handle_video_message(channel_post)

    # Debugging: Track end of handle_channel_post for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: handle_channel_post ended for chat_id: {chat_id}")


# Dictionary to track already processed confirmations (avoid duplicate notifications)
processed_confirmations = {}



def handle_callback_query(callback_query):
    global current_category, current_location, current_price_range, current_actual_price, current_delivery_price, current_write_count, user_data

    data = callback_query['data']
    sender_id = str(callback_query['from']['id'])
    chat_id = str(callback_query['message']['chat']['id'])

    # Log every callback for debugging.
    print(f"DEBUG: Callback query from user_id {sender_id}, chat_id {chat_id}: {data}")

    # Define reporter_username up front so it's available in all branches.
    reporter_username = callback_query['from'].get("username")
    if not reporter_username:
        reporter_username = str(callback_query['from'].get("id"))

    # Also extract the chat_id for messaging.
    chat_id = str(callback_query['message']['chat']['id'])

    # --- Report User Button Pressed ---
    if data.startswith("report_user_"):
        # Extract the reported user's username from the callback data.
        reported_username = data.split("_")[-1]

        # Check if this reporter has already reported the target.
        if user_report_tracker.get((reporter_username, reported_username)):
            send_message(chat_id, "❌ You have already reported this user.")
            answer_callback_query(callback_query)
            return

        # Build a confirmation inline keyboard.
        confirm_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Yes", "callback_data": f"confirm_report_{reported_username}"},
                    {"text": "❌ Cancel", "callback_data": f"cancel_report_{reported_username}"}
                ]
            ]
        }
        send_message_with_keyboard(chat_id,
                                   f"🤔 Are you sure you want to report user @{reported_username}? This action cannot be undone.",
                                   confirm_keyboard)
        answer_callback_query(callback_query)
        return


    # --- Confirm Report Branch ---
    elif data.startswith("confirm_report_"):
        reported_username = data.split("_")[-1]

        # Double-check if this reporter has already reported the target.
        if user_report_tracker.get((reporter_username, reported_username)):
            send_message(chat_id, "🚫 You've already reported this user!")
            answer_callback_query(callback_query)
            return

        # Persist this individual report in Firestore.
        reporter_key = f"{reporter_username}_{reported_username}"
        db.collection("user_report_tracker").document(reporter_key).set({
            "reporter": reporter_username,
            "reported": reported_username,
            "timestamp": int(time.time())
        })
        # Update the in-memory tracker.
        user_report_tracker[(reporter_username, reported_username)] = True

        # Increment the in-memory aggregate count.
        current_count = user_reports.get(reported_username, 0) + 1
        user_reports[reported_username] = current_count

        # Persist the updated aggregate count to Firestore.
        try:
            db.collection("user_reports").document(reported_username).set(
                {"report_count": current_count},
                merge=True
            )
        except Exception as e:
            print(f"DEBUG: Error saving report info for user {reported_username}: {e}")

        # Provide emoji-enhanced feedback based on the new count.
        if current_count == 3:
            send_message(
                chat_id,
                f"✅ Report confirmed! User @{reported_username} has now been reported 3 times. ⚠️ A warning has been issued."
            )
        elif current_count == 6:
            three_months_in_seconds = 90 * 24 * 60 * 60
            unblock_timestamp = int(time.time()) + three_months_in_seconds
            send_message(
                chat_id,
                f"✅ Report confirmed! User @{reported_username} has now been reported 6 times and will be suspended for 3 months. 🔒"
            )
            user_blocks[reported_username] = unblock_timestamp
            try:
                db.collection("user_blocks").document(str(reported_username)).set(
                    {"blocked_until": unblock_timestamp}
                )
            except Exception as e:
                print(f"DEBUG: Error saving block info for user {reported_username}: {e}")
        else:
            send_message(
                chat_id,
                f"✅ Report confirmed! User @{reported_username} now has {current_count} report(s)."
            )

        answer_callback_query(callback_query)
        return

        # Provide feedback based on the count.
        if current_count == 3:
            send_message(chat_id,
                         f"Report confirmed. User @{reported_username} has now been reported 3 times. A warning has been issued.")
        elif current_count == 6:
            three_months_in_seconds = 90 * 24 * 60 * 60
            unblock_timestamp = int(time.time()) + three_months_in_seconds
            send_message(chat_id,
                         f"Report confirmed. User @{reported_username} has now been reported 6 times and will be suspended for 3 months.")
            user_blocks[reported_username] = unblock_timestamp
            try:
                db.collection("user_blocks").document(str(reported_username)).set({"blocked_until": unblock_timestamp})
            except Exception as e:
                print(f"DEBUG: Error saving block info for user {reported_username}: {e}")
        else:
            send_message(chat_id, f"Report confirmed. User @{reported_username} now has {current_count} report(s).")

        answer_callback_query(callback_query)
        return

    # --- Cancel Report Branch ---
    elif data.startswith("cancel_report_"):
        # Extract the reported username from the callback data.
        reported_username = data.split("_")[-1]

        # Check if the reporter had actually reported this user.
        if user_report_tracker.get((reporter_username, reported_username)):
            # Undo the report.
            current_count = user_reports.get(reported_username, 0)
            if current_count > 0:
                new_count = current_count - 1
                user_reports[reported_username] = new_count
                try:
                    # Update Firestore with the new count.
                    db.collection("user_reports").document(reported_username).update({
                        "report_count": new_count
                    })
                except Exception as e:
                    print(f"DEBUG: Error updating report info for user {reported_username}: {e}")

            # Remove the tracking record.
            user_report_tracker.pop((reporter_username, reported_username), None)

            # Optionally remove the document if count goes to zero.
            if new_count == 0:
                try:
                    db.collection("user_reports").document(reported_username).delete()
                except Exception as e:
                    print(f"DEBUG: Error deleting report info for user {reported_username}: {e}")

            send_message(chat_id, f"✅ Your report for @{reported_username} has been cancelled.")
        else:
            send_message(chat_id, "ℹ️ No report was found to cancel.")

        answer_callback_query(callback_query)
        return


    # --- Start Chat for Seller Branch ---
    elif data.startswith("start_chat_seller:"):
        print("DEBUG: Entered start_chat_seller branch")
        session_id = data.split("start_chat_seller:")[1]
        print(f"DEBUG: Received start_chat_seller callback for session: {session_id}")
        try:
            session_ref = db.collection("chat_sessions").document(session_id)
            session_doc = session_ref.get()
            print(f"DEBUG: Retrieved session document. Exists? {session_doc.exists}")
            if not session_doc.exists:
                send_message(chat_id, "❌ Chat session not found or expired.")
                return

            session_data = session_doc.to_dict()
            print(f"DEBUG: Session data: {session_data}")

            # Verify that the sender is the seller.
            if str(session_data.get("seller_id")) != sender_id:
                send_message(chat_id, "🚫 Only the seller can start the chat using this button.")
                return

            print("DEBUG: Seller verified; proceeding to update chat session status.")

            # Update the session details.
            expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=3)  # Chat expires in 3 days.
            session_ref.update({
                "active": True,
                "last_activity": firestore.SERVER_TIMESTAMP,
                "expires_at": expires_at
            })

            print("DEBUG: Session updated successfully.")

            send_message(chat_id, "✅ Chat session started! You can now send messages to the buyer.")
            print("DEBUG: Confirmation message sent to seller.")

            # Notify the buyer.
            buyer_id = session_data.get("buyer_id")
            if buyer_id:
                send_message(buyer_id, "💬 The seller has initiated the chat. You may now reply.")
                print("DEBUG: Notification sent to buyer.")

            # Update the active chat mapping.
            active_chats[sender_id] = {"session_id": session_id, "partner_id": buyer_id}
            active_chats[buyer_id] = {"session_id": session_id, "partner_id": sender_id}
        except Exception as e:
            print(f"DEBUG: Error starting chat session for seller: {e}")
            send_message(chat_id, "❌ An error occurred while starting the chat session.")
        return

    # Ensure there's a storage object for this sender.
    if sender_id not in user_data:
        user_data[sender_id] = {}

        # --- Onboarding Flow Branches ---
    if data == "start_onboarding":
        pending_onboarding[sender_id] = {"state": "awaiting_business_name"}
        send_message(sender_id, "👋 Welcome to seller onboarding. Please enter your Business Name:")
        answer_callback_query(callback_query)
        return
    elif data == "cancel_onboarding":
        if sender_id in pending_onboarding:
            del pending_onboarding[sender_id]
        show_main_menu(sender_id)
        answer_callback_query(callback_query)
        return
        # Add a branch for bank selection callback data:
    elif data.startswith("select_bank_"):
        bank_code = data.replace("select_bank_", "")
        print(f"DEBUG: Bank selected callback data received: {data} -> bank_code: {bank_code}", flush=True)
        if sender_id in pending_onboarding:
            pending_onboarding[sender_id]["bank_code"] = bank_code
            pending_onboarding[sender_id]["state"] = "awaiting_account_number"  # Update state appropriately.
            send_message(sender_id, "✅ Bank selected! Please enter your Account Number:")
        else:
            print(f"DEBUG: Onboarding state not found for sender_id: {sender_id}", flush=True)
        answer_callback_query(callback_query)
        return

    # --- Resend (Release and Refund) Branch ---
    if data.startswith("resend_buttons_"):
        print(f"DEBUG: pending_payment_info keys: {list(pending_payment_info.keys())}")
        if sender_id not in pending_payment_info:
            print(f"DEBUG: No pending payment record for sender_id {sender_id}")
            send_message(sender_id,
                         "ℹ️ Payment hasn't been made yet. Please complete your payment using the link above first.")
            return

        print(f"DEBUG: pending_payment_info[{sender_id}] = {pending_payment_info[sender_id]}")
        buyer_state = pending_payment_info[sender_id]

        if not buyer_state.get("payment_complete", False):
            send_message(sender_id,
                         "ℹ️ Payment hasn't been made yet. Please complete your payment using the link above first.")
            return

        if buyer_state.get("action_initiated", False):
            send_message(sender_id, "ℹ️ Release/Refund has already been initiated for this payment.")
            return

        product_id_short = buyer_state.get("product_id_short")
        sale_index = buyer_state.get("sale_index")

        if not (product_id_short and sale_index):
            send_message(sender_id, "❌ Unable to resend buttons because required sale information is missing.")
            return

        followup_keyboard = {
            "inline_keyboard": [
                [{"text": "💸 Release Funds", "callback_data": f"rf_{product_id_short}_{sale_index}"}],
                [{"text": "🔄 Request Refund", "callback_data": f"rr_{product_id_short}_{sale_index}"}]
            ]
        }
        send_message_with_keyboard_retry(sender_id, "👉 Please choose an option below to proceed:", followup_keyboard)
        send_message(sender_id, "✅ Action buttons re-sent successfully.")
        return




    # --- ADMIN APPROVAL/DENIAL BRANCH ---
    elif data.startswith('approve_'):
        parts = data.split('_')
        if len(parts) >= 3:
            user_chat_id = parts[1]
            product_id = '_'.join(parts[2:])  # Rebuild in case the product ID contains underscores.
            approve_product(user_chat_id, product_id)
        else:
            print("DEBUG: Callback data for 'approve_' is in unexpected format.")
        return

    elif data.startswith('denytext_'):
        parts = data.split('_')
        if len(parts) >= 3:
            user_chat_id = parts[1]
            product_id = '_'.join(parts[2:])
            pending_admin_denials[sender_id] = (user_chat_id, product_id)
            send_message(sender_id, "Please enter your custom denial message to be sent to the lister:")
        else:
            print("DEBUG: Callback data for 'denytext_' is in unexpected format.")
        return

    elif data.startswith('deny_'):
        parts = data.split('_')
        if len(parts) >= 3:
            user_chat_id = parts[1]
            product_id = '_'.join(parts[2:])
            deny_product(user_chat_id, product_id)
        else:
            print("DEBUG: Callback data for 'deny_' is in unexpected format.")
        return

    # --- Start Chat Branch ---
    elif data.startswith("start_chat:"):
        print("DEBUG: Entered start_chat branch")
        session_id = data.split("start_chat:")[1]
        print(f"DEBUG: Received start_chat callback for session: {session_id}")
        try:
            session_ref = db.collection("chat_sessions").document(session_id)
            session_doc = session_ref.get()
            print(f"DEBUG: Retrieved session document. Exists? {session_doc.exists}")
            if not session_doc.exists:
                send_message(chat_id, "Chat session not found or expired.")
                return

            session_data = session_doc.to_dict()
            print(f"DEBUG: Session data: {session_data}")

            if str(session_data.get("buyer_id")) != sender_id:
                send_message(chat_id, "Only the buyer can start the chat.")
                return

            print("DEBUG: Buyer verified; proceeding to update chat session status.")

            # Update the session.
            expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=3)
  # Chat expires in 3 days

            session_ref.update({
                "active": True,
                "last_activity": firestore.SERVER_TIMESTAMP,
                "expires_at": expires_at
            })

            print("DEBUG: Session updated successfully.")

            send_message(chat_id, "Chat session started! You can now send messages to the seller.")
            print("DEBUG: Confirmation message sent to buyer.")

            seller_id = session_data.get("seller_id")
            if seller_id:
                send_message(seller_id, "The buyer has started the chat. You may now reply.")
                print("DEBUG: Notification sent to seller.")

            # Update the active chat mapping for both buyer and seller.
            active_chats[sender_id] = {"session_id": session_id, "partner_id": seller_id}
            active_chats[seller_id] = {"session_id": session_id, "partner_id": sender_id}
        except Exception as e:
            print(f"DEBUG: Error starting chat session: {e}")
            send_message(chat_id, "An error occurred while starting the chat session.")
        return



    # --- End Chat Branch ---
    elif data.startswith("end_chat:"):
        session_id = data.split("end_chat:")[1]
        try:
            session_ref = db.collection("chat_sessions").document(session_id)
            session_doc = session_ref.get()
            if not session_doc.exists:
                send_message(chat_id, "Chat session not found or already ended.")
                return
            session_data = session_doc.to_dict()

            # Retrieve the buyer and seller IDs from the session data.
            buyer_id = session_data.get("buyer_id")
            seller_id = session_data.get("seller_id")

            # Allow ending the chat if the sender is either buyer or seller.
            if sender_id not in [buyer_id, seller_id]:
                send_message(chat_id, "Only the buyer or seller can end the chat.")
                return

            # Delete the session document from Firestore.
            session_ref.delete()
            send_message(chat_id, "Chat session ended.")

            # Notify the other party depending on who ended the chat.
            if sender_id == buyer_id and seller_id:
                send_message(seller_id, "The buyer has ended the chat.")
            elif sender_id == seller_id and buyer_id:
                send_message(buyer_id, "The seller has ended the chat.")

            # Remove the active chat mapping for both participants.
            active_chats.pop(buyer_id, None)
            active_chats.pop(seller_id, None)
        except Exception as e:
            print(f"DEBUG: Error ending chat session: {e}")
            send_message(chat_id, "An error occurred while ending the chat session.")
        return




    # --- Seller Confirmation Branch ---
    elif data.startswith('accept_details_'):
        parts = data.split('_')
        if len(parts) >= 3:
            seller_chat_id = parts[2]
            if seller_chat_id in pending_listings:
                pending_listings[seller_chat_id]["state"] = "awaiting_photo"
                send_message(seller_chat_id, "Great! Please now upload a photo of your product. Use a caption as the product name.")
        else:
            print("DEBUG: Callback data for 'accept_details_' is in unexpected format.")
        return

    elif data.startswith('reenter_details_'):
        parts = data.split('_')
        if len(parts) >= 3:
            seller_chat_id = parts[2]
            if seller_chat_id in pending_listings:
                pending_listings[seller_chat_id].pop("actual_price", None)
                pending_listings[seller_chat_id].pop("delivery_price", None)
                pending_listings[seller_chat_id].pop("total_price", None)
                pending_listings[seller_chat_id]["state"] = "awaiting_actual_price"
                send_message(seller_chat_id, "Please re-enter the actual price of your product:")
        else:
            print("DEBUG: Callback data for 'reenter_details_' is in unexpected format.")
        return

    # --- AVAILABILITY CONFIRMATION BRANCH ---
    elif data.startswith('available_'):
        trimmed = data[len("available_"):].split('_', 1)  # Split into [buyer_chat_id, product_id]
        if len(trimmed) < 2:
            print("DEBUG: Callback data for 'available_' is in unexpected format.")
            return

        buyer_chat_id, product_id = trimmed
        lister_chat_id = str(callback_query['from']['id'])
        confirmation_key = f"{buyer_chat_id}_{product_id}"

        if confirmation_key in processed_confirmations:
            print(f"DEBUG: Availability already confirmed for buyer_chat_id: {buyer_chat_id}, product_id: {product_id}")
            return

        print(f"DEBUG: Availability confirmed by lister for buyer_chat_id: {buyer_chat_id}, product_id: {product_id}")
        process_availability_confirmation(lister_chat_id, buyer_chat_id, product_id, available=True)
        processed_confirmations[confirmation_key] = True

    elif data.startswith('not_available_'):
        trimmed = data[len("not_available_"):].split('_', 1)  # Split into [buyer_chat_id, product_id]
        if len(trimmed) < 2:
            print("DEBUG: Callback data for 'not_available_' is in unexpected format.")
            return

        buyer_chat_id, product_id = trimmed
        lister_chat_id = str(callback_query['from']['id'])
        confirmation_key = f"{buyer_chat_id}_{product_id}"

        if confirmation_key in processed_confirmations:
            print(f"DEBUG: Unavailability already confirmed for buyer_chat_id: {buyer_chat_id}, product_id: {product_id}")
            return

        print(f"DEBUG: Unavailability confirmed by lister for buyer_chat_id: {buyer_chat_id}, product_id: {product_id}")
        process_availability_confirmation(lister_chat_id, buyer_chat_id, product_id, available=False)
        processed_confirmations[confirmation_key] = True

        # --- Proceed to Payment Branch ---
    elif data.startswith('proceed_'):
        product_id = data[len("proceed_"):]
        buyer_chat_id = str(callback_query['from']['id'])
        print(f"DEBUG: Buyer {buyer_chat_id} chose to proceed with payment for product_id: {product_id}")
        # Store product_id in pending_payment_info and mark payment as pending.
        pending_payment_info[buyer_chat_id] = {"product_id": product_id, "payment_complete": False}
        # Prompt for phone number.
        send_message(buyer_chat_id, "Please enter your phone number for your payment receipt (digits only):")
        return


    # --- Release Funds and Refund Branch
    elif data.startswith('rf_') or data.startswith('rr_'):
        # Handle release funds request
        if data.startswith('rf_'):
            action = 'release'
            suffix = data[len('rf_'):]  # e.g., "7hn3t9yG_0"
        # Handle refund cancellation
        elif data.startswith('rr_cancel'):
            send_message(callback_query["message"]["chat"]["id"], "❌ Refund cancelled.")
            return
        # If this is a confirmed refund (callback data starts with "rr_confirm_")
        elif data.startswith('rr_confirm_'):
            action = 'refund'
            suffix = data[len('rr_confirm_'):]  # e.g., "7hn3t9yG_0" (or possibly with an extra prefix we’ll remove)
        # Otherwise, this is the initial refund request—prompt for confirmation.
        elif data.startswith('rr_'):
            action = 'refund'
            original_suffix = data[len("rr_"):]  # e.g., "7hn3t9yG_0"
            confirm_keyboard = {
                "inline_keyboard": [
                    [{"text": "Yes, proceed with refund", "callback_data": "rr_confirm_" + original_suffix}],
                    [{"text": "No, cancel refund", "callback_data": "rr_cancel"}]
                ]
            }
            info_text = ("⚠️ Refund Request Confirmation:\n"
                         "Are you sure you want to request a refund? "
                         "Refunds can take 3 to 5 business days.")
            send_message_with_keyboard(
                callback_query["message"]["chat"]["id"],
                info_text,
                confirm_keyboard,
                parse_mode="Markdown"
            )
            return

        # Now, regardless of action, try to parse the remaining callback data.
        try:
            # Ensure the suffix contains an underscore separating the product_id short and sale index.
            if '_' not in suffix:
                print(f"DEBUG: Callback data missing '_' separator for action {action}: {suffix}")
                return

            # Split the suffix into product_id_short and sale_index
            product_id_short, sale_index = suffix.rsplit("_", 1)
            # Option 2: Strip any extra "confirm_" prefix if present.
            if product_id_short.startswith("confirm_"):
                product_id_short = product_id_short[len("confirm_"):]
            print(f"DEBUG: Triggering '{action}' action with data: product_id_short={product_id_short}, index={sale_index}")

            # Retrieve the sale mapping document from Firestore
            mapping_doc = db.collection("sale_mapping").document(product_id_short).get()
            if not mapping_doc.exists:
                send_message(callback_query["message"]["chat"]["id"], f"❌ No sale mapping found for product: {product_id_short}")
                print(f"DEBUG: Mapping document not found for product_id_short: {product_id_short}")
                return

            mapping_data = mapping_doc.to_dict()
            print(f"DEBUG: Sale mapping document data: {mapping_data}")
            sale_id = mapping_data.get(sale_index)
            if not sale_id:
                send_message(callback_query["message"]["chat"]["id"], "❌ Invalid sale reference.")
                return

            print(f"DEBUG: Found mapping -> sale_id: {sale_id}")
            # Extract the full product_id from sale_id. This example assumes sale_id is in the format "productid_..."
            if "_" in sale_id:
                product_id = sale_id.split("_")[0]
            else:
                print(f"DEBUG: Invalid sale_id format: {sale_id}")
                return

            # Now process the funds action using your updated functions.
            if action == 'release':
                # Call the release function and capture its return flag.
                stop_processing = handle_funds_release_flutterwave(callback_query)
            else:
                # Call the refund function and capture its return flag.
                stop_processing = handle_refund_flutterwave(callback_query)

            # If the handler indicates that no further processing should occur (i.e. funds already processed),
            # simply return (the handler will have already sent the menu/message).
            if stop_processing:
                return

            # Otherwise, continue with the shared post-processing code.
            sale_doc_ref = db.collection('products').document(product_id).collection('sales').document(sale_id)
            sale_doc_snapshot = sale_doc_ref.get()
            if sale_doc_snapshot.exists:
                sale_data = sale_doc_snapshot.to_dict()
                buyer_chat_id = sale_data.get("buyer_chat_id")
                seller_chat_id = sale_data.get("seller_chat_id")

                # Update the sale status.
                status_update = {"status": "settled"} if action == 'release' else {"status": "refund_requested"}
                sale_doc_ref.update(status_update)

                # Notify the buyer.
                if buyer_chat_id:
                    buyer_msg = ("✅ You have released the funds. The transaction is now complete."
                                 if action == 'release'
                                 else "🔄 You have requested a refund. The refund process has been initiated.")
                    send_message(buyer_chat_id, buyer_msg)
                # Notify the seller.
                if seller_chat_id:
                    seller_msg = ("✅ The buyer has released the funds. The transaction is now complete."
                                  " Funds can take 3 to 5 business days to be received into your bank account."
                                  if action == 'release'
                                  else "🔄 The buyer has requested a refund. Please await further instructions.")
                    send_message(seller_chat_id, seller_msg)

                # Optionally, prompt both parties for recommendations.
                if buyer_chat_id and seller_chat_id:
                    buyer_recommend_keyboard = {
                        "inline_keyboard": [
                            [{"text": "👍 Recommend Seller",
                              "callback_data": f"recommend_{buyer_chat_id}_{seller_chat_id}"}]
                        ]
                    }
                    send_message_with_keyboard(
                        buyer_chat_id,
                        "✅ Action complete. Would you like to recommend the seller?",
                        buyer_recommend_keyboard
                    )
                    seller_recommend_keyboard = {
                        "inline_keyboard": [
                            [{"text": "👍 Recommend Buyer",
                              "callback_data": f"recommend_{seller_chat_id}_{buyer_chat_id}"}]
                        ]
                    }
                    send_message_with_keyboard(
                        seller_chat_id,
                        "✅ Action complete. Would you like to recommend the buyer?",
                        seller_recommend_keyboard
                    )
            else:
                print(f"DEBUG: Sale document does not exist for sale_id: {sale_id}")

        except Exception as e:
            print(f"DEBUG: Error handling {action} callback: {e}")
        return




    # Cancel Payment when Link has been generated ---
    elif data.startswith("cancel_payment"):
        try:
            # (Optional) Update sale record in Firestore to mark as canceled, if needed.
            # Remove the pending payment record.
            if sender_id in pending_payment_info:
                pending_payment_info.pop(sender_id, None)
                print(f"DEBUG: Removed {sender_id} from pending_payment_info")
            else:
                print(f"DEBUG: Sender {sender_id} not found in pending_payment_info")

            # Also remove the sender from active_payment to restore normal messaging.
            if sender_id in active_payment:
                active_payment.pop(sender_id, None)
                print(f"DEBUG: Removed {sender_id} from active_payment")
            else:
                print(f"DEBUG: Sender {sender_id} not found in active_payment")

            send_message(sender_id, "❌ Payment process cancelled. You can now send messages as normal.")
        except Exception as e:
            print(f"DEBUG: Error cancelling payment process: {e}")
            send_message(sender_id, "❌ An error occurred while cancelling your payment.")
        answer_callback_query(callback_query)
        return


    # Recommendation Branch ---
    elif data.startswith("recommend_"):
        # Expected format: "recommend_{recommender_id}_{recommended_id}"
        try:
            parts = data.split("_")
            if len(parts) < 3:
                send_message(chat_id, "❌ Invalid recommendation data.")
                answer_callback_query(callback_query)
                return

            recommender_id = parts[1]
            recommended_id = parts[2]

            # Check to ensure that the recommender hasn't already recommended this user.
            if user_recommend_tracker.get((recommender_id, recommended_id)):
                send_message(chat_id, "ℹ️ You have already recommended this user.")
                answer_callback_query(callback_query)
                return

            # Record the recommendation in memory.
            user_recommend_tracker[(recommender_id, recommended_id)] = True
            current_count = user_recommendations.get(recommended_id, 0) + 1
            user_recommendations[recommended_id] = current_count

            # Persist the updated recommendation count to Firestore.
            try:
                db.collection("user_recommendations").document(recommended_id).set(
                    {"recommendation_count": current_count},
                    merge=True
                )
            except Exception as e:
                print(f"DEBUG: Error saving recommendation info for user {recommended_id}: {e}")

            send_message(chat_id, f"👍 Recommendation recorded. This user now has {current_count} recommendation(s).")
        except Exception as e:
            print(f"DEBUG: Error processing recommendation: {e}")
            send_message(chat_id, "❌ An error occurred while processing your recommendation.")
        answer_callback_query(callback_query)
        return


    # --- Cancel Branch ---
    elif data == 'cancel':
        buyer_chat_id = str(callback_query['from']['id'])
        print(f"DEBUG: Buyer {buyer_chat_id} canceled the transaction.")
        show_main_menu(buyer_chat_id)
        return

    # --- Onboarding Branch ---
    if data == "start_onboarding":
        pending_onboarding[sender_id] = {"state": "awaiting_business_name"}
        print(f"DEBUG: Initiated onboarding for {sender_id}: {pending_onboarding[sender_id]}")
        send_message(sender_id, "👋 Welcome to seller onboarding. Please enter your Business Name:")
        answer_callback_query(callback_query)
        return
    elif data == "cancel_onboarding":
        if sender_id in pending_onboarding:
            del pending_onboarding[sender_id]
        show_main_menu(sender_id)
        answer_callback_query(callback_query)
        return

    # --- Navigation & Buyer Actions ---

    if data == 'start':
        # The user clicked "Start"; now check using Firestore.
        if not has_user_started(sender_id):
            # The user hasn’t accepted the policy yet, so present it.
            show_policy_and_terms(chat_id)
        else:
            # If there's a pending purchase, serve it and let the user continue.
            if sender_id in pending_purchases:
                product_doc_id = pending_purchases.pop(sender_id)
                get_product_by_docid(chat_id, product_doc_id)
            else:
                # Otherwise, continue to show the main menu.
                show_main_menu(sender_id)

    elif data == 'list_products':
        seller_doc_ref = db.collection('sellers').document(str(sender_id))
        seller_doc = seller_doc_ref.get()
        seller_data = seller_doc.to_dict() if seller_doc.exists else {}

        subaccount_id = seller_data.get("subaccount_id")
        if subaccount_id:
            if verify_subaccount_id(subaccount_id):
                # Subaccount verified on Flutterwave – proceed with listing.
                show_category_options(sender_id)  # This builds buttons with callback data like "list_category_..."
            else:
                # The stored subaccount ID is invalid on Flutterwave.
                # Delete the outdated seller record from Firestore.
                seller_doc_ref.delete()
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "🚀 Start Onboarding", "callback_data": "start_onboarding"}],
                        [{"text": "❌ Cancel", "callback_data": "cancel_onboarding"}]
                    ]
                }
                send_message_with_keyboard(
                    sender_id,
                    "⚠️ Your seller account details seem outdated. Please re-onboard to list your products.",
                    keyboard
                )
        else:
            # No subaccount record exists in Firestore.
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🚀 Start Onboarding", "callback_data": "start_onboarding"}],
                    [{"text": "❌ Cancel", "callback_data": "cancel_onboarding"}]
                ]
            }
            send_message_with_keyboard(
                sender_id,
                "ℹ️ It looks like you haven't completed seller onboarding. Please select an option:",
                keyboard
            )

    # ---------------- Listing Flow (Seller Mode) ----------------

    elif data.startswith('list_category_'):
        current_category = data.split('_')[-1]
        if current_category.strip().lower() == "fashion/wears".lower():
            show_gender_options(sender_id, mode="list")
        else:
            show_location_options(sender_id, current_category)
        return

    elif data.startswith('list_gender_'):
        # For example: "list_gender_male" or "list_gender_female"
        gender = data.split('_')[-1]
        updated_category = "Fashion/Wears_" + gender
        show_fashion_subcategory_options(sender_id, updated_category, mode="list")
        return

    elif data.startswith('list_fashion_subcat_'):
        # Expected format: "list_fashion_subcat_Fashion/Wears_male_Tops"
        parts = data.split('_')[3:]  # This retrieves: ["Fashion/Wears", "male", "Tops"]
        updated_category = "_".join(parts)  # e.g., "Fashion/Wears_male_Tops"
        show_location_options(sender_id, updated_category)
        return

    elif data.startswith('list_location_'):
        parts = data.split('_')[2:]
        current_location = parts[-1]
        current_category = "_".join(parts[:-1])
        get_price_range(sender_id, current_category, current_location)
        return

    elif data.startswith('list_price_'):
        parts = data.split('_')[2:]
        current_price_range = parts[-1]
        current_location = parts[-2]
        current_category = "_".join(parts[:-2])
        prompt_actual_price_input(sender_id, current_category, current_location, current_price_range)
        return

    # ---------------- Browse Flow (Buyer Mode) ----------------

    elif data == 'browse_products':
        show_browse_category_options(sender_id)
        return

    elif data.startswith('browse_category_'):
        current_category = data.split('_')[-1]
        if current_category.strip().lower() == "fashion/wears".lower():
            show_gender_options(sender_id, mode="browse")
        else:
            show_browse_location_options(sender_id, current_category)
        return

    elif data.startswith('browse_gender_'):
        gender = data.split('_')[-1]
        updated_category = "Fashion/Wears_" + gender
        show_fashion_subcategory_options(sender_id, updated_category, mode="browse")
        return

    elif data.startswith('browse_fashion_subcat_'):
        parts = data.split('_')[3:]  # e.g., ["Fashion/Wears", "female", "Accessories"]
        updated_category = "_".join(parts)  # e.g., "Fashion/Wears_female_Accessories"
        show_browse_location_options(sender_id, updated_category)
        return

    elif data.startswith('browse_location_'):
        parts = data.split('_')[2:]
        current_location = parts[-1]
        current_category = "_".join(parts[:-1])
        show_browse_price_range_options(sender_id, current_category, current_location)
        return

    elif data.startswith('browse_price_'):
        parts = data.split('_')[2:]
        current_price_range = parts[-1]
        current_location = parts[-2]
        current_category = "_".join(parts[:-2])
        show_matching_products(sender_id, current_category, current_location, current_price_range)
        return

    # ---------------- Other Actions ----------------

    elif data == 'get_product':  # New branch for Get Product
        # Set a pending state so that the next text message is interpreted as a product ID.
        pending_listings[sender_id] = {"state": "awaiting_product_id"}
        send_message(sender_id, "🔎 Please enter the Product ID of the product:")
        return

    elif data.startswith('select_location_'):
        # This handles the selection of a location via the inline keyboard in listing mode.
        # Expected callback data format: "select_location_<Location>"
        selected_location = data.split('_', 2)[-1]
        pending_listing = pending_listings.get(sender_id, {})
        pending_listing["location"] = selected_location
        # Set the state to the next step (e.g., awaiting_actual_price).
        pending_listing["state"] = "awaiting_actual_price"
        pending_listings[sender_id] = pending_listing
        send_message(sender_id,
                     f"✅ Location set to {selected_location}. Please enter the actual price of your product.")
        return

    # (Any additional branches go here.)

    elif data.startswith('buy_'):

        product_id = data.split('_')[1]

        if not has_user_started(sender_id):

            # User hasn't initiated a private chat, so store their pending purchase.

            pending_purchases[sender_id] = product_id

            send_start_button(sender_id)

            answer_callback_query(callback_query["id"], "Please click 'Start' to continue your purchase.")

        else:

            # User has already interacted with the bot, proceed as before.

            check_product_availability(sender_id, product_id)


    elif data == "cancel_onboarding":
        # When the user clicks Cancel, return them to the main menu.
        show_main_menu(sender_id)

    elif data == "agree_terms":
        update_user_terms_agreement(sender_id, True)
        send_message(sender_id, "✅ Thank you for accepting our Terms & Policies!")
        show_main_menu(sender_id)
        answer_callback_query(callback_query)
        return

    elif data == "disagree_terms":
        send_message(sender_id,
                     "❌ You must agree to our Terms & Policies to use The Markit Bot. Please contact support if you have any questions.")
        answer_callback_query(callback_query)
        return

    elif data == "contact_support":
        # Set a pending state so the next message is interpreted as a support message.
        pending_listings[sender_id] = {"state": "awaiting_support_message"}
        send_message(sender_id, "💬 Please type your support message. It will be sent directly to the admin.")
        answer_callback_query(callback_query)
        return

    # Admin inline actions for support sessions
    elif data.startswith("admin_reply_"):
        # Extract the target user_id from callback data.
        target_user = data.split("admin_reply_")[1]
        # Mark that the admin is now replying to this particular session.
        admin_pending_reply[sender_id] = target_user
        send_message(sender_id, f"✏️ Please type your reply message for user {target_user}.")
        answer_callback_query(callback_query)
        return

    elif data.startswith("admin_end_"):
        target_user = data.split("admin_end_")[1]
        if target_user in support_sessions:
            support_sessions[target_user]["active"] = False
            send_message(support_sessions[target_user]["user_chat_id"],
                         "🛑 The admin has ended this support session. Thank you!")
            send_message(ADMIN_CHAT_ID, f"✅ Support session with user {target_user} ended.")
            try:
                db.collection("support_sessions").document(target_user).delete()
                print(f"Deleted support session for user {target_user} from Firestore.")
            except Exception as e:
                print(f"Error deleting support session for user {target_user}: {e}")
            del support_sessions[target_user]
        else:
            send_message(ADMIN_CHAT_ID, f"❌ No active support session for user {target_user}.")
        answer_callback_query(callback_query)
        return

    elif data.startswith("show_history_"):
        target_user = data.split("show_history_")[1]
        if target_user in support_sessions:
            history = support_sessions[target_user].get("history", [])
            if history:
                conversation_summary = "\n".join(history)
                send_message(ADMIN_CHAT_ID,
                             f"📜 Conversation History with user {target_user}:\n\n{conversation_summary}")
            else:
                send_message(ADMIN_CHAT_ID, f"ℹ️ No history available for user {target_user}.")
        else:
            send_message(ADMIN_CHAT_ID, f"❌ No active support session for user {target_user}.")
        answer_callback_query(callback_query)
        return

    # --- Updated Unblock Branch ---
    if data.startswith("admin_unblock_"):
        # Extract the target user id from callback data.
        target_user = data.split("_")[-1]

        # Check if target_user is in our blocked records.
        if target_user in user_blocks:
            # Remove block from in-memory storage.
            del user_blocks[target_user]

            # Remove block document from Firestore.
            try:
                db.collection("user_blocks").document(target_user).delete()
                print(f"DEBUG: Block record for user {target_user} deleted from Firestore.")
            except Exception as e:
                print(f"DEBUG: Error deleting block record for user {target_user}: {e}")

            # Also remove the user report record from Firestore.
            try:
                db.collection("user_reports").document(target_user).delete()
                print(f"DEBUG: User report record for user {target_user} deleted from Firestore.")
            except Exception as e:
                print(f"DEBUG: Error deleting user report record for user {target_user}: {e}")

            # Notify the user they are unblocked.
            send_message(target_user, "🎉 Your account has been unblocked. You may now continue using the bot.")
            # Notify the admin.
            send_message(ADMIN_CHAT_ID, f"✅ User {target_user} has been unblocked.")
        else:
            # If the user wasn't blocked, let the admin know.
            send_message(ADMIN_CHAT_ID, f"❌ User {target_user} is not blocked.")

        answer_callback_query(callback_query)
        return

    # --- Final Debug Output for Selected Users (Optional) ---
    if sender_id in ["6014538461", "6334679159"] or chat_id in ["6014538461", "6334679159"]:
        print(f"DEBUG: Callback query handled for user_id: {sender_id}, chat_id: {chat_id}")
    # Fallback: Always answer the callback query to prevent indefinite loading indicators.
    answer_callback_query(callback_query)


def answer_callback_query(callback_query, text=None):
    # If callback_query is a dict, extract its id; otherwise assume it's already an id.
    if isinstance(callback_query, dict):
        callback_query_id = callback_query.get("id")
    else:
        callback_query_id = callback_query

    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    # Set up the payload with the mandatory callback_query_id.
    payload = {"callback_query_id": callback_query_id}

    # If a text message is provided, include it in the payload.
    if text is not None:
        payload["text"] = text

    try:
        response = requests.post(url, json=payload, timeout=TIMEOUT)
        result = response.json()
        if not result.get("ok"):
            print("DEBUG: answerCallbackQuery error:", result.get("description"))
        else:
            print("DEBUG: Answered callback query:", result)
    except requests.exceptions.RequestException as e:
        print("DEBUG: Failed to answer callback query:", e)


def handle_photo_message(message):
    chat_id = str(message['chat']['id'])

    # Process the photo to obtain the file_id (the highest resolution image is at the end of the list)
    file_id = message.get("photo")[-1].get("file_id")

    # Extract the caption from the photo message (if provided)
    caption = message.get("caption", "")

    # Retrieve the current listing for this chat (or create an empty dict if none exists)
    listing = pending_listings.get(chat_id, {})
    listing["photo_file_id"] = file_id

    # Save the caption as the product name.
    listing["product_name"] = caption if caption.strip() else "No product name provided."

    pending_listings[chat_id] = listing

    print(f"DEBUG: Photo received for chat_id {chat_id} with caption: {repr(caption)}")
    send_message(chat_id, "Photo received. Please enter your product description")

    # Update the state to proceed to the next step in the listing flow.
    listing["state"] = "awaiting_description"
    return



def reset_current_selections():
    global current_category, current_location, current_price_range
    current_category = None
    current_location = None
    current_price_range = None

menu_displayed = False

def show_main_menu(chat_id):
    # Debugging for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: Showing main menu to chat_id {chat_id}")

    keyboard = {
        "inline_keyboard": [
            [{"text": "🛍️ List Products", "callback_data": "list_products"}],
            [{"text": "🔍 Browse Products", "callback_data": "browse_products"}],
            [{"text": "📦 Get Product by Id", "callback_data": "get_product"}],
            [{"text": "☎️ Contact & Support", "callback_data": "contact_support"}]
        ]
    }
    send_message_with_keyboard(chat_id, "👉 Choose an option below:", keyboard)

    # Confirm menu sent
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: Main menu sent to chat_id {chat_id}")




def send_message_with_keyboard(chat_id, text, keyboard, parse_mode=None):
    """
    Sends a message with an inline keyboard to a Telegram chat using the Telegram Bot API.

    Args:
        chat_id (int or str): The Telegram chat ID to send the message to.
        text (str): The message text.
        keyboard (dict): The inline keyboard markup as a dictionary.
        parse_mode (str, optional): The parsing mode for formatting ("Markdown", "MarkdownV2", or "HTML"). Defaults to None.

    Returns:
        dict: The response from the Telegram API.
    """
    global previous_message_id  # Track the previous message ID for deletion or updating if needed

    # Debug logging for specified chat_ids
    if str(chat_id) in ["6014538461", "6334679159"]:
        print(f"DEBUG: send_message_with_keyboard called for chat_id {chat_id}, text: {text}")

    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard  # Inline keyboard as a dictionary
    }

    # If a parse mode is provided, include it in the payload.
    if parse_mode:
        payload["parse_mode"] = parse_mode

    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        response_data = response.json()
        print(f"DEBUG: Response from Telegram API: {response_data}")

        if response.status_code != 200 or not response_data.get('ok'):
            print(
                f"DEBUG: Failed to send message with keyboard. Status: {response.status_code}, Response: {response_data}")
        else:
            # Update previous_message_id with the new message ID
            previous_message_id = response_data.get("result", {}).get("message_id")
            print(f"DEBUG: Updated previous_message_id: {previous_message_id}")

        return response_data
    except requests.exceptions.RequestException as e:
        print(f"DEBUG: Error sending message with keyboard: {e}")
        return {"ok": False}




def send_message_with_keyboard_retry(chat_id, text, keyboard, max_retries=3, delay=2):
    """
    Attempts to send a message with an inline keyboard.
    If the response is not successful, it retries up to max_retries times, with a delay between attempts.

    Args:
        chat_id (int or str): The Telegram chat ID.
        text (str): The message text.
        keyboard (dict): The inline keyboard markup as a dictionary.
        max_retries (int): The maximum number of retry attempts.
        delay (int or float): The delay between retries, in seconds.

    Returns:
        dict: The final response from the Telegram API.
    """
    attempt = 0
    response_data = None
    while attempt < max_retries:
        response_data = send_message_with_keyboard(chat_id, text, keyboard)
        if response_data.get("ok"):
            print(f"DEBUG: Message with keyboard sent successfully on attempt {attempt + 1}.")
            return response_data
        else:
            print(f"DEBUG: Attempt {attempt + 1} failed. Retrying in {delay} second(s)...")
            time.sleep(delay)
            attempt += 1

    print("ERROR: Failed to send message with keyboard after {} attempt(s).".format(max_retries))
    return response_data

def normalize_for_callback(s):
    """
    Replace problematic characters in a string for use in Telegram callback_data.
    This function:
      - Replaces the ₦ symbol with "N"
      - Replaces spaces and dashes with underscores
      - Removes commas
    """
    s = s.replace("₦", "N")      # Replace currency symbol with N
    s = s.replace(" ", "_")      # Replace spaces with underscores
    s = s.replace("-", "_")      # Replace dashes with underscores
    s = s.replace(",", "")       # Remove commas
    return s



def show_category_options(chat_id):
    # Define category options with a tuple of (Category Name, Emoji)
    categories = [
        ("Fashion/Wears", "👗"),
        ("Electronics", "💻"),
        ("Food", "🍔"),
        ("Housing Items", "🏡"),
        ("Gadgets", "📱"),
        ("Others", "🤔")
    ]

    # Build an inline keyboard row for each category using the same pattern.
    keyboard = {
        "inline_keyboard": [
            [{"text": f"{emoji} {name}", "callback_data": f"list_category_{name}"}]
            for name, emoji in categories
        ]
    }
    send_message_with_keyboard(chat_id, "Select a category:", keyboard)


def show_browse_category_options(chat_id):
    # Define categories with corresponding emojis (including Others).
    categories = [
        ("Fashion/Wears", "👗"),
        ("Electronics", "💻"),
        ("Food", "🍔"),
        ("Housing Items", "🏡"),
        ("Gadgets", "📱"),
        ("Others", "🤔")
    ]

    # Build an inline keyboard row for each category.
    keyboard = {
        "inline_keyboard": [
            [{"text": f"{emoji} {name}", "callback_data": f"browse_category_{name}"}]
            for name, emoji in categories
        ]
    }
    send_message_with_keyboard(chat_id, "Select a category:", keyboard)


def show_gender_options(chat_id, mode="list"):
    """
    Displays gender options when the user selects the Fashion category.
    'mode' distinguishes between listing (sale) and browsing flows.
    """
    callback_prefix = "list_gender_" if mode == "list" else "browse_gender_"

    keyboard = {
        "inline_keyboard": [
            [{"text": "👨 Male", "callback_data": f"{callback_prefix}male"}],
            [{"text": "👩 Female", "callback_data": f"{callback_prefix}female"}]
        ]
    }
    send_message_with_keyboard(chat_id, "Select Gender:", keyboard)


def show_fashion_subcategory_options(chat_id, updated_category, mode="list"):
    """
    Displays an inline keyboard for fashion subcategories.
    updated_category: e.g. "Fashion/Wears_male" or "Fashion/Wears_female"
    mode: "list" (seller listing) or "browse" (user browsing)
    """
    # Normalize the updated_category by replacing "/" and spaces with underscores
    normalized_category = updated_category.replace("/", "_").replace(" ", "_")

    subcategories = [
        ("Tops 👕", "Tops"),
        ("Bottoms 👖", "Bottoms"),
        ("Outerwear 🧥", "Outerwear"),
        ("Footwear 👟", "Footwear"),
        ("Accessories 👜", "Accessories")
    ]

    # Use a callback prefix based on the mode.
    callback_prefix = "list_fashion_subcat_" if mode == "list" else "browse_fashion_subcat_"

    # Build the inline keyboard and log each callback value.
    keyboard = {"inline_keyboard": []}
    for text, sub in subcategories:
        callback_data = f"{callback_prefix}{normalized_category}_{sub}"
        # Debug: print callback data and its length
        print(f"DEBUG: Callback data: {callback_data} (length: {len(callback_data)})")
        keyboard["inline_keyboard"].append([{"text": text, "callback_data": callback_data}])

    # Also print the entire keyboard payload in JSON format.
    import json
    print("DEBUG: Full keyboard payload:")
    print(json.dumps(keyboard, indent=2))

    send_message_with_keyboard(chat_id, "Select a fashion subcategory:", keyboard)


def show_location_options(chat_id, category):
    locations = [
        "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno", "Cross River",
        "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "FCT", "Gombe", "Imo", "Jigawa", "Kaduna", "Kano",
        "Katsina", "Kebbi", "Kogi", "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun",
        "Oyo", "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara"
    ]

    # Normalize the provided category for callback safety.
    norm_category = normalize_for_callback(category)

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": f"📍 {loc}",
                    "callback_data": f"list_location_{norm_category}_{normalize_for_callback(loc)}"
                }
                for loc in locations[i:i + 5]
            ]
            for i in range(0, len(locations), 5)
        ]
    }

    # Debug: Print the full keyboard payload as JSON.
    import json
    print("DEBUG: Full location keyboard payload:")
    print(json.dumps(keyboard, indent=2))

    send_message_with_keyboard(chat_id, "Select a location:", keyboard)


def show_browse_location_options(chat_id, category):
    locations = [
        "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno", "Cross River",
        "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "FCT", "Gombe", "Imo", "Jigawa", "Kaduna", "Kano",
        "Katsina", "Kebbi", "Kogi", "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun",
        "Oyo", "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara"
    ]

    # Normalize the provided category for callback safety
    norm_category = normalize_for_callback(category)

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": f"📍 {loc}",
                    "callback_data": f"browse_location_{norm_category}_{normalize_for_callback(loc)}"
                }
                for loc in locations[i:i + 5]
            ]
            for i in range(0, len(locations), 5)
        ]
    }

    # Debug: Print the full keyboard payload as JSON
    import json
    print("DEBUG: Location keyboard payload:")
    print(json.dumps(keyboard, indent=2))

    send_message_with_keyboard(chat_id, "Select a location:", keyboard)


def get_price_range(chat_id, category, location):
    price_ranges = [
        "₦500 - ₦5,000",
        "₦5,001 - ₦20,000",
        "₦20,001 - ₦50,000",
        "₦50,001 - ₦100,000",
        "₦100,001 - ₦500,000",
        "₦500,001 - ₦1,000,000",
        "₦1,000,001 and above"
    ]

    # Normalize the category and location for safe callback data.
    norm_category = normalize_for_callback(category)
    norm_location = normalize_for_callback(location)

    keyboard = {"inline_keyboard": []}

    for price in price_ranges:
        # Normalize each price string.
        norm_price = normalize_for_callback(price)
        callback_data = f"list_price_{norm_category}_{norm_location}_{norm_price}"
        print(f"DEBUG: Price callback data: {callback_data} (length: {len(callback_data)})")
        keyboard["inline_keyboard"].append(
            [{"text": f"💰 {price}", "callback_data": callback_data}]
        )

    # Debug: Print the full inline keyboard payload as JSON.
    import json
    print("DEBUG: Full price range keyboard payload:")
    print(json.dumps(keyboard, indent=2))

    send_message_with_keyboard(chat_id, "Select a price range:", keyboard)


def show_browse_price_range_options(chat_id, category, location):
    # Normalize category and location first.
    norm_category = normalize_for_callback(category)
    norm_location = normalize_for_callback(location)

    price_ranges = [
        "₦500 - ₦5,000",
        "₦5,001 - ₦20,000",
        "₦20,001 - ₦50,000",
        "₦50,001 - ₦100,000",
        "₦100,001 - ₦500,000",
        "₦500,001 - ₦1,000,000",
        "₦1,000,001 and above"
    ]

    keyboard = {"inline_keyboard": []}

    for price in price_ranges:
        norm_price = normalize_for_callback(price)
        callback_data = f"browse_price_{norm_category}_{norm_location}_{norm_price}"
        print(f"DEBUG: Price callback data: {callback_data} (length: {len(callback_data)})")
        keyboard["inline_keyboard"].append([{"text": f"💰 {price}", "callback_data": callback_data}])

    import json
    print("DEBUG: Full price range keyboard payload:")
    print(json.dumps(keyboard, indent=2))

    send_message_with_keyboard(chat_id, "Select a price range:", keyboard)


def show_matching_products(chat_id, category, location, price_range):
    """
    Retrieve and display matching products based on category, location, and price range.
    """
    global current_read_count

    try:
        print(f"Browsing products for category: {category}, location: {location}, price range: {price_range}")  # Debugging

        products_ref = db.collection('products')

        # Ensure we don't exceed read limits
        if current_read_count >= 50000:
            send_message(chat_id, "⚠️ Read limit reached. Please try again later.")
            show_main_menu(chat_id)
            return

        # Query Firestore for matching products
        query = products_ref \
            .where('category', '==', category) \
            .where('location', '==', location) \
            .where('price_range', '==', price_range)

        print(f"Constructed query: {query}")  # Debugging

        results = query.stream()
        results_list = list(results)  # Convert query results to a list for easier handling

        # Debugging: Log raw query results
        print(f"Raw query results: {results_list}")

        if not results_list:
            send_message(chat_id, "😞 No matching products found.")
            show_main_menu(chat_id)
            return

        # Process and display each product
        for doc in results_list:
            product = doc.to_dict()
            product_id    = product.get('product_id', 'Unknown ID')  # Explicitly fetch the product ID
            photo_file_id = product.get('photo_file_id')
            product_name  = product.get('product_name', 'No product name available')
            description   = product.get('description', 'No description available')
            actual_price  = product.get('actual_price', 'Unknown price')
            delivery_price= product.get('delivery_price', 0)
            total_price   = product.get('total_price', 'Unknown total price')

            # Debugging: Validate the document ID vs. product_id
            print(f"DEBUG: Document ID: {doc.id}, Product ID: {product_id}, Data: {product}")

            # Build the message string with emojis.
            message = (
                f"📦 Product Name: {product_name}\n"
                f"🆔 Product ID: {product_id}\n"  # Display the product ID for copy-pasting
                f"💵 Actual Price: ₦{actual_price}\n"
                f"🚚 Delivery Price: ₦{delivery_price}\n"
                f"💰 Total Price: ₦{total_price}\n"
                f"📝 Description: {description}"
            )

            # Retrieve seller sales count and append if available (and if > 0).
            seller_chat_id = product.get('chat_id')
            if seller_chat_id:
                sales_count = get_sales_count(seller_chat_id)
                if sales_count and sales_count > 0:
                    message += f"\n\n⭐ Seller Sales: {sales_count}"

            # Precaution: Truncate the message if it exceeds Telegram's max message length.
            if len(message) > MAX_MESSAGE_LENGTH:
                truncation_notice = "\n...[truncated]"
                message = message[:MAX_MESSAGE_LENGTH - len(truncation_notice)] + truncation_notice

            # Build the inline keyboard using doc.id for callback data.
            keyboard = {
                "inline_keyboard": [
                    [{"text": f"🛒 Buy for ₦{total_price}", "callback_data": f"buy_{doc.id}"}]
                ]
            }

            # Send product details and photo to the buyer.
            send_message_with_keyboard(chat_id, message, keyboard)
            send_photo(chat_id, photo_file_id)

        current_read_count += len(results_list)
        print(f"DEBUG: Total products retrieved: {len(results_list)}. Updated current_read_count: {current_read_count}")

    except Exception as e:
        print(f"ERROR: Failed to retrieve products. Exception: {e}")  # Debugging
        send_message(chat_id, "❌ Failed to retrieve products. Please try again later.")

    # Show the main menu after completing the operation
    show_main_menu(chat_id)






def prompt_actual_price_input(chat_id, category, location, price_range):
    # Initialize the listing state for this seller.
    pending_listings[chat_id] = {
        "state": "awaiting_actual_price",
        "category": category,
        "location": location,
        "price_range": price_range,
        # Add other fields if needed.
    }
    send_message(chat_id, "Please enter the actual price of your product:")


def prompt_delivery_price_input(chat_id):
    send_message(chat_id, "Please enter the delivery cost of your product within the state:")



def send_photo(chat_id, photo_file_id, caption=None, reply_markup=None, parse_mode=None):
    # Debugging: Track send_photo for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: send_photo called for chat_id {chat_id}, photo_file_id: {photo_file_id}, caption: {caption}, reply_markup: {reply_markup}, parse_mode: {parse_mode}")

    url = f"{TELEGRAM_API_URL}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": photo_file_id,
        "caption": caption
    }
    # Add reply_markup to payload if provided.
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    # Add parse_mode to payload if provided.
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode

    response = requests.post(url, json=payload, timeout=TIMEOUT)
    print(f"Response from Telegram API: {response.json()}")  # Debugging

    # Debugging: Confirm photo sent for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: Photo sent to chat_id {chat_id}")




def prompt_photo_input(chat_id, category, location, price_range):
    global bot_requesting_photo, menu_displayed
    bot_requesting_photo = True
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": "Please upload a photo of your product along with a description, including the specific address and price in the caption."}
    requests.post(url, json=payload, timeout=TIMEOUT)
    menu_displayed = False


def send_message(chat_id, text, parse_mode=None, reply_markup=None):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    # Include parse_mode if provided.
    if parse_mode:
        payload["parse_mode"] = parse_mode

    if reply_markup:
        # If reply_markup is a dict, iterate over inline_keyboard buttons and ensure "text" fields are strings.
        if isinstance(reply_markup, dict):
            if "inline_keyboard" in reply_markup:
                for row in reply_markup["inline_keyboard"]:
                    for button in row:
                        if "text" in button:
                            button["text"] = str(button["text"])
            payload["reply_markup"] = reply_markup

        # If it's an object that can be converted to a dict, do so.
        elif hasattr(reply_markup, "to_dict"):
            temp = reply_markup.to_dict()
            if "inline_keyboard" in temp:
                for row in temp["inline_keyboard"]:
                    for button in row:
                        if "text" in button:
                            button["text"] = str(button["text"])
            payload["reply_markup"] = temp

        # Fallback: attempt to convert directly.
        else:
            try:
                payload["reply_markup"] = json.loads(json.dumps(reply_markup))
            except Exception as e:
                print(f"DEBUG: Error serializing reply_markup: {e}")

    try:
        response = requests.post(url, json=payload, timeout=TIMEOUT)
        resp_json = response.json()
        print(f"Response from Telegram API: {resp_json}")  # Debugging
    except requests.exceptions.RequestException as e:
        print(f"DEBUG: Error sending message to chat_id {chat_id}: {e}")
        resp_json = None

    if str(chat_id) in ["6014538461", "6334679159"]:
        print(f"DEBUG: Message sent to chat_id {chat_id}")

    return resp_json


def set_webhook():
    # Attempt to get the production server URL from the environment;
    # Fallback to NGROK_URL if none is set.
    public_url = os.getenv('SERVER_URL') or ngrok_url
    webhook_url = f"{public_url}/{bot_token}"

    response = requests.post(
        f"{TELEGRAM_API_URL}/setWebhook",
        data={'url': webhook_url},
        timeout=TIMEOUT
    )

    if response.status_code == 200:
        print(f"Webhook set successfully: {webhook_url}")
    else:
        print(f"Failed to set webhook: {response.text}")


def has_user_started(user_id):
    """
    Check if the user (buyer or seller) has accepted the policy.
    Returns True if the user’s record exists in either collection with terms_accepted True.
    """
    user_id = str(user_id)

    # Check in the sellers collection
    seller_ref = db.collection('sellers').document(user_id)
    seller_doc = seller_ref.get()
    if seller_doc.exists:
        seller_data = seller_doc.to_dict()
        if seller_data.get("terms_accepted", False):
            return True

    # Check in the buyers collection (if you maintain one separately)
    buyer_ref = db.collection('buyers').document(user_id)
    buyer_doc = buyer_ref.get()
    if buyer_doc.exists:
        buyer_data = buyer_doc.to_dict()
        if buyer_data.get("terms_accepted", False):
            return True

    return False


def send_start_button(chat_id):
    # Debugging: Track start button send for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: send_start_button called for chat_id {chat_id}")

    keyboard = {
        "inline_keyboard": [
            [{"text": "🚀 Start", "callback_data": "start"}]
        ]
    }
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": " Click '🚀 Start' to continue your journey.",
        "reply_markup": keyboard
    }
    response = requests.post(url, json=payload, timeout=TIMEOUT)
    if response.status_code == 200:
        message_id = response.json()['result']['message_id']
        print(f"Start button sent to chat_id {chat_id}, message_id: {message_id}")  # Debugging

    print(f"Response from Telegram API: {response.json()}")  # Debugging

    # Debugging: Confirm start button sent for specific users
    if chat_id in [6014538461, 6334679159]:
        print(f"DEBUG: Start button sent to chat_id {chat_id}")



def pin_message(chat_id, message_id):
    url = f"{TELEGRAM_API_URL}/pinChatMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    response = requests.post(url, json=payload, timeout=TIMEOUT)
    print(f"Pin message response from Telegram API: {response.json()}")  # Debugging


def delete_document_and_subcollections(doc_ref):
    """
    Recursively delete a document and all of its subcollections.
    """
    # Iterate over each subcollection in the document.
    subcollections = list(doc_ref.collections())
    for subcol in subcollections:
        # Iterate through all documents in the subcollection.
        for sub_doc in subcol.stream():
            # Recursively delete each sub-document (and any of its subcollections)
            delete_document_and_subcollections(sub_doc.reference)

    # Finally, delete the parent document.
    doc_ref.delete()
    print(f"Deleted document: {doc_ref.id}")



def delete_old_products():
    global current_delete_count

    while True:
        try:
            if current_delete_count >= 20000:
                print("Delete limit reached. Skipping deletion for today.")
                time.sleep(24 * 60 * 60)
                # Optionally, reset current_delete_count here if needed.
                continue

            # Use UTC datetime and subtract 3 days.
            three_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
            # Convert the datetime to a Unix timestamp (seconds since epoch)
            three_days_ago_unix = int(three_days_ago.timestamp())

            products_ref = db.collection('products')
            # Compare using Unix timestamp instead of a datetime object
            query = products_ref.where('timestamp', '<=', three_days_ago_unix)
            results = query.stream()

            deleted_products = set()  # Track deleted product IDs

            found_expired = False  # Debug: to check if any expired product is found.
            for product in results:
                found_expired = True
                if current_delete_count >= 20000:
                    break
                if product.id not in deleted_products:
                    # Recursively delete the product document and its subcollections.
                    delete_document_and_subcollections(product.reference)
                    current_delete_count += 1
                    deleted_products.add(product.id)
                    print(f"Deleted old product: {product.id}")

            if not found_expired:
                print("No expired products found. (Check your timestamp format or criteria.)")

        except Exception as e:
            print(f"Failed to delete old products: {e}")

        # Sleep for 24 hours before running again.
        time.sleep(24 * 60 * 60)




def reset_counters_at_midnight():
    while True:
        now = time.localtime()
        seconds_until_midnight = ((24 - now.tm_hour - 1) * 60 * 60) + ((60 - now.tm_min - 1) * 60) + (60 - now.tm_sec)
        time.sleep(seconds_until_midnight)
        global current_read_count, current_write_count, current_delete_count
        current_read_count = 0
        current_write_count = 0
        current_delete_count = 0
        print("Counters reset at midnight.")

def check_storage_limit():
    # Ensure the project ID is set in the environment
    os.environ["GOOGLE_CLOUD_PROJECT"] = "the-markit-446be"

    client = firestore.Client(project="the-markit-446be")
    collections = client.collections()
    total_size = 0
    for collection in collections:
        for document in collection.stream():
            total_size += len(document.to_dict())
    return total_size >= 1 * 1024 * 1024 * 1024  # 1 GB


def bot_requesting_input():
    return (bot_requesting_photo or
            bot_requesting_price or
            bot_requesting_delivery_amount or
            bot_requesting_description)



def approve_product(user_chat_id, product_id):
    """
    Approves a product listing after ensuring the seller has a valid subaccount.
    The product listing is set live for 72 hours (3 days).
    """
    global current_write_count

    try:
        # Retrieve product details from 'pending_products'
        product_doc = db.collection('pending_products').document(product_id).get()
        product_details = product_doc.to_dict() if product_doc.exists else None

        if not product_details:
            print(f"ERROR: Product ID {product_id} not found in 'pending_products'.")
            send_message(ADMIN_CHAT_ID, "❌ Failed to approve the product listing. No pending product found.")
            return

        # Instead of checking the old 'bank_details' field, now we look for seller subaccount info.
        seller_subaccount = product_details.get("seller_subaccount", "")

        # If the subaccount field is not set or indicates that the seller is onboarded,
        # then look up the seller's document.
        if not seller_subaccount or seller_subaccount in ["", "Missing", None, "Already Onboarded"]:
            seller_doc = db.collection('sellers').document(user_chat_id).get()
            if seller_doc.exists:
                seller_data = seller_doc.to_dict()
                seller_subaccount = seller_data.get("subaccount_id", "")
            else:
                seller_subaccount = ""

        if not seller_subaccount or seller_subaccount in ["", "Missing"]:
            send_message(ADMIN_CHAT_ID, "❌ Failed to approve product. Missing bank details (subaccount).")
            return

        # Set the live expiration timestamp (72 hours from now)
        product_details["live_until"] = int(time.time()) + 72 * 60 * 60

        # Approve the product, moving it from pending to live
        db.collection('products').document(product_id).set(product_details)
        print(f"DEBUG: Product ID {product_id} approved and saved to 'products' collection.")
        db.collection('pending_products').document(product_id).delete()
        print(f"DEBUG: Product ID {product_id} deleted from 'pending_products' collection.")

        # Increment the write counter and notify both the seller and admin
        current_write_count += 1
        send_message(user_chat_id, "✅ Your product listing has been approved and is now live for 72 hours! 🎉")
        send_message(ADMIN_CHAT_ID, f"✅ Product listing approved successfully for product ID {product_id}.")

        # Show the main menu to the seller.
        show_main_menu(user_chat_id)
    except Exception as e:
        print(f"ERROR: Failed to approve product listing. Exception: {e}")
        send_message(ADMIN_CHAT_ID, f"❌ Failed to approve the product listing. Error: {e}")


def deny_product(user_chat_id, product_id, custom_message=None):
    """
    Denies a product listing by deleting it from the pending products.
    Optionally sends a custom denial message to the seller.
    """
    try:
        # Check if the product exists in pending_products
        product_doc = db.collection('pending_products').document(product_id).get()
        if product_doc.exists:
            db.collection('pending_products').document(product_id).delete()
            print(f"DEBUG: Product ID {product_id} deleted from 'pending_products'.")
        else:
            print(f"DEBUG: No pending product found for product ID: {product_id}")

        # Send an appropriate message to the seller
        if custom_message:
            send_message(user_chat_id, custom_message)
        else:
            send_message(
                user_chat_id,
                "Your product listing has been denied because it did not follow our policies. "
                "Please ensure the listing includes adequate information about the product, including its type and address."
            )

        send_message(ADMIN_CHAT_ID, f"Product listing {product_id} denied successfully.")
        # Show the start menu to the seller
        show_main_menu(user_chat_id)

    except Exception as e:
        print(f"Failed to deny product listing: {e}")
        send_message(ADMIN_CHAT_ID, "Failed to deny the product listing.")


def send_video_to_channel(video_file_id, caption=None):
    url = f"{TELEGRAM_API_URL}/sendVideo"
    payload = {
        "chat_id": CHANNEL_ID,
        "video": video_file_id,
        "caption": caption
    }
    response = requests.post(url, json=payload, timeout=TIMEOUT)
    print(f"Response from Telegram API: {response.json()}")  # Debugging
    return response.json().get('ok', False)

def send_message_to_channel(message):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": message}
    response = requests.post(url, json=payload, timeout=TIMEOUT)
    print(f"Response from Telegram API: {response.json()}")  # Debugging
    return response.json().get('ok', False)

def send_photo_to_channel(photo_file_id, caption=None):
    url = f"{TELEGRAM_API_URL}/sendPhoto"
    payload = {
        "chat_id": CHANNEL_ID,
        "photo": photo_file_id,
        "caption": caption
    }
    response = requests.post(url, json=payload, timeout=TIMEOUT)
    print(f"Response from Telegram API: {response.json()}")  # Debugging
    return response.json().get('ok', False)


def check_product_availability(buyer_chat_id, product_id):
    """
    Checks the availability of the product and sends a confirmation request to the seller.
    If not found in 'products', falls back to 'pending_products'.
    """
    try:
        # First, attempt to retrieve the product from the 'products' collection
        product_doc = db.collection('products').document(product_id).get()
        if not product_doc.exists:
            print(f"DEBUG: Product {product_id} not found in 'products' collection. Trying 'pending_products'.")
            # Fallback: try retrieving from the 'pending_products' collection
            product_doc = db.collection('pending_products').document(product_id).get()

        # Extract product details or handle missing data
        product_details = product_doc.to_dict() if product_doc.exists else None
        if not product_details:
            send_message(buyer_chat_id, "Sorry, we were unable to locate the product.")
            print(f"DEBUG: Product {product_id} not found in any collection.")
            return

        # Extract essential product details
        lister_chat_id = product_details.get('chat_id')
        product_description = product_details.get('description', 'No description available')
        photo_file_id = product_details.get('photo_file_id', 'No photo available')
        actual_price = product_details.get('actual_price', 'Unknown price')
        delivery_price = product_details.get('delivery_price', 0)
        total_price = product_details.get('total_price', 'Unknown total price')
        location = product_details.get('location', 'Unknown location')

        # Debugging: Ensure extracted values match expectations
        print(f"DEBUG: Retrieved details for product_id {product_id}:\n{product_details}")

        # Notify the buyer that availability is being confirmed
        send_message(buyer_chat_id, "Please hold on while we confirm the product's availability with the seller.")

        # Prepare an inline keyboard for the seller to confirm availability
        keyboard = {
            "inline_keyboard": [
                [{"text": "Available", "callback_data": f"available_{buyer_chat_id}_{product_id}"}],
                [{"text": "Not Available", "callback_data": f"not_available_{buyer_chat_id}_{product_id}"}]
            ]
        }

        # Build a caption with the product details
        caption = (
            f"Product: {product_description}\n"
            f"Location: {location}\n"
            f"Actual Price: ₦{actual_price}\n"
            f"Delivery Price: ₦{delivery_price}\n"
            f"Total Price: ₦{total_price}\n"
            "Please confirm if your product is still available:"
        )

        # Send the product image and caption to the seller for availability confirmation
        send_photo(lister_chat_id, photo_file_id, caption=caption)
        send_message_with_keyboard(lister_chat_id, "Is this product still available?", keyboard)

        # Save the availability confirmation request in Firestore
        confirmation_doc_id = f"{lister_chat_id}_{buyer_chat_id}_{product_id}"
        db.collection('pending_confirmations').document(confirmation_doc_id).set({
            "buyer_chat_id": buyer_chat_id,
            "product_id": product_id,
            "lister_chat_id": lister_chat_id
        })

        print(f"DEBUG: Stored buyer_chat_id: {buyer_chat_id} in pending_confirmations for product {product_id}")
    except Exception as e:
        print(f"ERROR: Failed to process availability check for product_id {product_id}. Exception: {e}")
        send_message(buyer_chat_id, "An unexpected error occurred while confirming product availability. Please try again later.")



# Track processed confirmations
processed_confirmations = {}

def process_availability_confirmation(lister_chat_id, buyer_chat_id, product_id, available):
    # Generate a unique confirmation key
    confirmation_key = f"{buyer_chat_id}_{product_id}"

    # (Removed the check for processed_confirmations to allow multiple confirmations)
    print(f"DEBUG: Processing availability confirmation for product_id: {product_id}, available: {available}")

    try:
        # Query pending confirmations in the database
        query = db.collection('pending_confirmations') \
                  .where("lister_chat_id", "==", lister_chat_id) \
                  .where("product_id", "==", product_id)
        confirmations = list(query.stream())
    except Exception as e:
        print(f"Error querying pending_confirmations: {e}")
        return

    # If no confirmations are found, handle directly.
    if not confirmations:
        if available:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "Proceed to Payment", "callback_data": f"proceed_{product_id}"}],
                    [{"text": "Cancel", "callback_data": "cancel"}]
                ]
            }
            response = send_message_with_keyboard(
                buyer_chat_id,
                "The product is available. Do you want to proceed to payment?",
                keyboard
            )
            if response.get('ok'):
                send_message(
                    lister_chat_id,
                    "Your response has been sent to the buyer. The product is marked as available. "
                    "Please wait for the buyer to proceed with payment."
                )
                show_main_menu(lister_chat_id)
                print(f"DEBUG: Sent 'available' message to buyer_chat_id: {buyer_chat_id}")
            else:
                print(f"Failed to send 'available' message to buyer_chat_id: {buyer_chat_id}, response: {response}")
        else:
            send_message(buyer_chat_id, "The product is not available. Please try another product.")
            show_main_menu(lister_chat_id)
        return

    # Process confirmations found in the database.
    for confirmation in confirmations:
        details = confirmation.to_dict()
        buyer_chat = details.get('buyer_chat_id', buyer_chat_id)

        # Delete the pending confirmation entry
        db.collection('pending_confirmations').document(confirmation.id).delete()

        if available:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "Proceed to Payment", "callback_data": f"proceed_{product_id}"}],
                    [{"text": "Cancel", "callback_data": "cancel"}]
                ]
            }
            response = send_message_with_keyboard(
                buyer_chat,
                "The product is available. Do you want to proceed to payment?",
                keyboard
            )
            if response.get('ok'):
                send_message(
                    lister_chat_id,
                    "Your response has been sent to the buyer. The product is marked as available. "
                    "Please wait for the buyer to proceed with payment."
                )
                show_main_menu(lister_chat_id)
                print(f"DEBUG: Sent 'available' message to buyer_chat_id: {buyer_chat}")
            else:
                print(f"Failed to send 'available' message to buyer_chat_id: {buyer_chat}, response: {response}")
        else:
            try:
                response = send_message_with_keyboard(
                    buyer_chat,
                    "Sorry, the product is not available.",
                    {"inline_keyboard": []}
                )
                if response.get('ok'):
                    send_message(
                        lister_chat_id,
                        "Your response has been sent to the buyer. The product is marked as not available."
                    )
                    show_main_menu(buyer_chat)
                    print(f"DEBUG: Sent 'not available' message to buyer_chat_id: {buyer_chat}")
                else:
                    print(f"Failed to send 'not available' message to buyer_chat_id: {buyer_chat}, response: {response}")
            except Exception as e:
                print(f"Failed to send unavailability response: {e}")
            show_main_menu(lister_chat_id)

    # Optional: If you still want to record that a confirmation happened,
    # you can update processed_confirmations here, but it's no longer used
    # to reject further attempts.
    processed_confirmations[confirmation_key] = True




def send_unavailability_response(lister_chat_id, buyer_chat_id, product_id):
    try:
        send_message(buyer_chat_id, "Sorry, the product is no longer available.")
        send_message(lister_chat_id, "Your response has been sent to the intending buyer. The product is marked as not available.")
        show_main_menu(buyer_chat_id)  # Send the start menu to the intending buyer
        print(f"DEBUG: Sent 'not available' message to buyer_chat_id: {buyer_chat_id}")
    except Exception as e:
        print(f"Failed to send unavailability response: {e}")  # Debugging




def initiate_escrow_payment(buyer_chat_id, product_id, buyer_email):
    product_id = str(product_id)
    print(f"DEBUG: Retrieving product details for product_id: {product_id}")

    # Retrieve product details from 'products'; if not found, try 'pending_products'
    product_ref = db.collection('products').document(product_id)
    product_snapshot = product_ref.get()
    if not product_snapshot.exists:
        print("DEBUG: Product not found in 'products'; trying 'pending_products'.")
        product_ref = db.collection('pending_products').document(product_id)
        product_snapshot = product_ref.get()

    product_data = product_snapshot.to_dict()
    print(f"DEBUG: Retrieved product_data: {product_data}")
    if not product_data:
        send_message(buyer_chat_id, "😞 Sorry, we couldn't retrieve the product details. Please try again later.")
        pending_payment_info.pop(buyer_chat_id, None)
        active_payment.pop(buyer_chat_id, None)
        show_main_menu(buyer_chat_id)
        return

    # Mark this chat as active in the payment process.
    active_payment[buyer_chat_id] = True
    print(f"DEBUG: Marked chat {buyer_chat_id} as active in payment process.")

    # Convert product price to float.
    try:
        amount = float(product_data.get('total_price'))
    except Exception as e:
        print(f"DEBUG: Error converting price: {e}")
        send_message(buyer_chat_id, "⚠️ There was an error processing the payment amount. Please check the price and try again.")
        return

        # Generate a unique payment reference and sale ID.
    reference = f"{buyer_chat_id}_{product_id}_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    #reference = f"live_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    print(f"DEBUG: Sale payment reference generated: {reference}")

    # Retrieve buyer_phone from pending_payment_info
    buyer_phone = pending_payment_info.get(buyer_chat_id, {}).get("buyer_phone", "")

    sale_id = f"{product_id}_{int(time.time())}"
    print(f"DEBUG: Generated sale ID: {sale_id}")

    # Retrieve seller's subaccount info. If missing or placeholder, look it up from the seller's document.
    seller_subaccount = product_data.get("seller_subaccount", "")
    if not seller_subaccount or seller_subaccount in ["", "Missing", "Already Onboarded"]:
        seller_chat_id = product_data.get("chat_id")
        if seller_chat_id:
            seller_doc = db.collection('sellers').document(seller_chat_id).get()
            if seller_doc.exists:
                seller_data = seller_doc.to_dict()
                seller_subaccount_id = seller_data.get("subaccount_id", "")
            else:
                seller_subaccount_id = ""
        else:
            seller_subaccount_id = ""
    else:
        seller_subaccount_id = seller_subaccount

    if not seller_subaccount_id:
        send_message(buyer_chat_id, "🚫 Cannot process payment as the seller's bank details (subaccount) are missing.")
        return

    # Verify the seller's subaccount; if invalid, notify the buyer and update Firestore.
    if not verify_subaccount_id(seller_subaccount_id):
        send_message(buyer_chat_id,
                     "❌ It appears the seller's account is no longer active. The seller needs to re-onboard to update bank details.")
        seller_chat_id = product_data.get("chat_id")
        if seller_chat_id:
            db.collection('sellers').document(seller_chat_id).update({
                "subaccount_id": "",  # Or update with a flag indicating re-onboarding is needed
            })
        return

    # Your provided keys and URLs.
    FLW_PUBLIC_KEY = 'FLWPUBK-e165ceed81c5840ed090c7da72db6e56-X'
    redirect_url = "https://t.me/the_markit"
    logo_url = "https://imgur.com/daGfOCs.jpeg"

    # Build the payload for Flutterwave payment initialization.
    payload = {
        "tx_ref": reference,
        "amount": amount,  # Adjust the amount as required
        "currency": "NGN",
        "redirect_url": redirect_url,  # Replace with your live redirect URL
        "payment_options": "card",
        "customer": {
            "email": buyer_email,
            "phonenumber": buyer_phone,
            "name": pending_payment_info.get(buyer_chat_id, {}).get("buyer_name", "Valued Customer")
        },
        "meta": [
            {"metaname": "rave_escrow_tx", "metavalue": 1},
            {"metaname": "product_id", "metavalue": product_id},
            {"metaname": "buyer_chat_id", "metavalue": buyer_chat_id}
        ],
        "customizations": {
            "title": "Escrow Payment",
            "description": f" Payment for product {product_id}",
            "logo": logo_url  # Replace with your logo URL if needed
        },
        "subaccounts": [
            {
                "id": seller_subaccount_id,
                "transaction_charge_type": "percentage",
                "transaction_charge": 0.05
            }
        ]
    }

    try:
        url = "https://api.flutterwave.com/v3/payments"
        headers = {
            "Authorization": f"Bearer {FLW_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()
        print(f"DEBUG: Payment API request: {json.dumps(payload, indent=2)}") #Request payload
        print(f"DEBUG: Payment API response: {json.dumps(response_data, indent=2)}")

        if response_data.get("status") == "success" and response_data.get("data", {}).get("link"):
            payment_url = response_data["data"]["link"]

            # Send the payment link to the buyer using a "Pay Now" button.
            initial_response_text = (
                "💳 Please click the button below to complete your secure payment.\n"
                "🔒 Your payment is safe and funds will be held in escrow until verified."
            )
            pay_keyboard = {
                "inline_keyboard": [
                    [{"text": "💳 Pay Now", "url": payment_url}]
                ]
            }
            send_message_with_keyboard_retry(buyer_chat_id, initial_response_text, pay_keyboard)
            print(f"DEBUG: Payment link sent to buyer {buyer_chat_id} for product {product_id}")

            # Send a Cancel Payment button.
            cancel_keyboard = {
                "inline_keyboard": [
                    [{"text": "❌ Cancel Payment", "callback_data": "cancel_payment"}]
                ]
            }
            send_message_with_keyboard_retry(
                buyer_chat_id,
                "🚫 If you wish to cancel your payment and resume normal chat, click the button below:",
                cancel_keyboard
            )

            # Create a chat session in Firestore.
            seller_chat_id = product_data.get('chat_id')
            if not seller_chat_id:
                send_message(buyer_chat_id, "😞 Sorry, there was an error retrieving seller data.")
                return
            session_id = create_chat_session(buyer_chat_id, seller_chat_id)

            # Provide additional messaging options to the buyer.
            seller_report_count = user_reports.get(seller_chat_id, 0)
            seller_report_info = (
                f"⚠️ Note: This seller has been reported {seller_report_count} time(s).\n"
                if seller_report_count > 0 else ""
            )
            buyer_prompt = (
                f"✅ Your payment has been initiated.\n"
                f"{seller_report_info}"
                "💬 When ready, click below to start chatting with the seller or 🚩 report the seller if necessary."
            )
            buyer_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "💬 Start Chat with Seller", "callback_data": f"start_chat:{session_id}"},
                        {"text": "🚩 Report Seller", "callback_data": f"report_user_{seller_chat_id}"}
                    ]
                ]
            }
            send_message_with_keyboard_retry(buyer_chat_id, buyer_prompt, buyer_keyboard)

            # Fallback message in case action buttons are missing.
            fallback_message = (
                "ℹ️ If you do not see the payment action buttons, click the button below to request them again. "
                "Use this only after payment is made."
            )
            fallback_keyboard = {
                "inline_keyboard": [
                    [{"text": "🔄 Resend Buttons", "callback_data": f"resend_buttons_{product_id}"}]
                ]
            }
            send_message_with_keyboard_retry(buyer_chat_id, fallback_message, fallback_keyboard)

            official_txid = response_data["data"].get("id")
            if official_txid:
                print(f"DEBUG: Official transaction ID (txid) received: {official_txid}")
            else:
                print("DEBUG: No official transaction ID found in the payment API response.")

            # Create a sale record in Firestore with status 'initiated'.
            sale_data = {
                "payment_reference": reference,
                "txid": official_txid,
                "buyer_chat_id": buyer_chat_id,
                "seller_chat_id": seller_chat_id,
                "session_id": session_id,
                "amount": amount,
                "status": "initiated",  # Pending payment
                "created_at": int(time.time()),
                "short_product_id": generate(size=8),
                "short_sale_id": generate(size=8),
                "seller_notified": False  # Will be updated on payment confirmation
            }

            sale_doc_ref = db.collection('products').document(product_id).collection('sales').document(sale_id)
            sale_doc_ref.set(sale_data)
            print(
                f"DEBUG: Sale record created with short_product_id: {sale_data['short_product_id']} "
                f"and short_sale_id: {sale_data['short_sale_id']}"
            )

            # Do NOT notify the seller here.
            print(f"DEBUG: Payment initiated for sale_id {sale_id}. Awaiting payment confirmation before seller notification.")
        else:
            error_message = response_data.get("message", "Failed to initialize payment.")
            send_message(buyer_chat_id, f"❌ Failed to initiate payment. Error: {error_message}")
            print(f"DEBUG: Payment initialization failed for buyer {buyer_chat_id}, response: {response_data}")
    except Exception as e:
        print(f"DEBUG: Error initiating escrow payment: {e}")
        send_message(buyer_chat_id, "⚠️ An error occurred while initiating the payment. Please try again later.")



def generate_short_id(length=8):
    return nanoid.generate(size=length)



def calculate_expected_net_amount(price):
    """
    Calculates fee for Local Transactions:
     - If price < 2500: fee = 2.25% of price.
     - If price ≥ 2500: fee = 2.25% of price + NGN 150.
     - Fee is capped at NGN 2000.
    Returns a tuple: (applied_fee, seller_net)
    """
    if price < 2500:
        fee = price * 0.0225
    else:
        fee = price * 0.0225 + 150
    applied_fee = min(fee, 2000)
    seller_net = price - applied_fee
    return applied_fee, seller_net


def prompt_email_input(buyer_chat_id, product_id):
    print(f"DEBUG: Storing product_id '{product_id}' for buyer {buyer_chat_id}")
    # Store product_id and mark payment as not complete.
    pending_payment_info[buyer_chat_id] = {"product_id": product_id, "payment_complete": False}
    send_message(buyer_chat_id, "Please enter your email address for your payment receipt:")



def handle_buyer_email_input(buyer_chat_id, text):
    """
    Called when the buyer sends their email address.
    It records the email, then prompts the buyer with a fallback button
    in case they did not receive the payment action buttons.
    """
    buyer_email = text.strip()
    # Update the pending payment info with the email
    pending_payment_info[buyer_chat_id]['email'] = buyer_email
    print(f"DEBUG: Stored buyer email for chat {buyer_chat_id}: {buyer_email}")

    # Send confirmation message with fallback option
    fallback_message = (
        "Email recorded successfully.\n"
        "If you do not see the payment action buttons (Release Funds/Request Refund), "
        "click the button below to request them again."
    )
    fallback_keyboard = {
        "inline_keyboard": [
            [{"text": "Resend Buttons",
              "callback_data": f"resend_buttons_{pending_payment_info[buyer_chat_id]['product_id']}"}]
        ]
    }
    send_message_with_keyboard(buyer_chat_id, fallback_message, fallback_keyboard)


def handle_resend_buttons(callback_query):
    buyer_chat_id = str(callback_query['from']['id'])
    data = callback_query.get('data', '')
    # Expected format: "resend_buttons_{product_id}"
    try:
        product_id = data.split('_', 2)[-1]
    except IndexError:
        send_message(buyer_chat_id, "Invalid request for re-sending buttons.")
        return

    # Re-build the inline keyboard exactly as before. Adjust the callback data as needed.
    # In your other flow, you use something like "rf_{product_id_short}_{sale_index}".
    # Here we'll assume you want to re-send basic options using the product_id as reference.
    followup_keyboard = {
        "inline_keyboard": [
            [{"text": "Release Funds", "callback_data": f"release_funds_{product_id}"}],
            [{"text": "Request Refund", "callback_data": f"refund_{product_id}"}]
        ]
    }
    message_text = "Please choose an option below to proceed:"

    # Use either the retry function or your regular send_message_with_keyboard.
    # For reliability, you could use the retry version:
    response = send_message_with_keyboard_retry(buyer_chat_id, message_text, followup_keyboard)
    if response.get("ok"):
        send_message(buyer_chat_id, "Action buttons re-sent successfully.")
    else:
        send_message(buyer_chat_id, "Sorry, we could not resend the action buttons. Please try again later.")


def handle_buyer_contact_input(buyer_chat_id, text):
    if buyer_chat_id in pending_payment_info:
        # Save the phone number into the buyer's payment info.
        pending_payment_info[buyer_chat_id]['phone'] = text.strip()
        send_message(
            buyer_chat_id,
            "Thank you! You can now proceed to payment or cancel the transaction.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Proceed to Payment", "callback_data": "proceed_to_payment"}],
                    [{"text": "Cancel", "callback_data": "cancel_transaction"}]
                ]
            }
        )
        print(f"DEBUG: Stored phone number for buyer {buyer_chat_id}: {text.strip()}")
    else:
        send_message(buyer_chat_id, "No pending transaction found. Please try again.")



def get_bank_code(bank_name):
    """
    Retrieves the bank code for a given bank name.
    """
    # Normalize the bank name to lowercase for consistent matching
    bank_name = bank_name.strip().lower()

    # Comprehensive mapping of bank names to bank codes
    bank_codes = {
        "gtbank": "058",
        "guaranty trust bank": "058",
        "first bank": "011",
        "first bank of nigeria": "011",
        "zenith bank": "057",
        "access bank": "044",
        "access bank plc": "044",
        "uba": "033",
        "united bank for africa": "033",
        "fidelity bank": "070",
        "ecobank": "050",
        "wema bank": "035",
        "polaris bank": "076",
        "fcmb": "214",
        "first city monument bank": "214",
        # Add more mappings as needed
    }

    # Look up the bank code using the normalized bank name
    return bank_codes.get(bank_name, "")


def create_transfer_recipient_if_missing(seller_chat_id, bank_info):
    """
    Creates a transfer recipient in Flutterwave for the seller using a direct HTTP request.
    This function is used to prepare for fund disbursement to the seller's bank account.
    """
    url = "https://api.flutterwave.com/v3/transfers"
    headers = {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "account_bank": bank_info.get("bank_code"),  # Flutterwave requires the bank code
        "account_number": bank_info.get("account"),  # Seller's account number
        "amount": 0,  # Set to 0 for verification purposes (no actual transfer initiated here)
        "narration": f"Verify recipient for seller {seller_chat_id}",
        "currency": "NGN",
        "reference": f"verify_{seller_chat_id}_{int(time.time())}"  # Unique reference for this verification
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        # Check API response for successful validation
        if response_data.get("status") == "success":
            print(f"DEBUG: Recipient creation/verification successful: {response_data}")
            return response_data.get("data")
        else:
            print(f"DEBUG: Error creating/validating recipient: {response_data}")
            return None
    except Exception as e:
        print(f"DEBUG: Exception during recipient creation/validation: {e}")
        return None


def disburse_funds_to_seller(seller_chat_id, product_id):
    """
    Notifies the seller that their funds have been disbursed.

    In your split payment/escrow setup, Flutterwave automatically handles fund allocation.
    This function logs the payout event and sends a Telegram message to the seller.
    """
    # Retrieve product details from Firestore.
    product_ref = db.collection('products').document(product_id)
    product_data = product_ref.get().to_dict()
    if not product_data:
        print(f"DEBUG: No product data found for product_id: {product_id}")
        return

    product_price = product_data.get('total_price')
    # Calculate processing fee and seller's net amount.
    processing_fee, seller_net = calculate_expected_net_amount(product_price)

    # Generate a unique transfer reference (if you want to use it for logging or later tracking).
    reference = f"{seller_chat_id}_{product_id}_{int(time.time())}"
    print(f"DEBUG: Using transfer reference: {reference}")

    try:
        # Since funds are released automatically via split payment/escrow,
        # we do not need to call an external disbursement API.
        # We simply log the successful disbursement and notify the seller.
        print(
            f"DEBUG: Funds disbursed for product_id: {product_id}, seller_chat_id: {seller_chat_id}. Seller net: NGN {seller_net}")
        #send_message(seller_chat_id, f"✅ Funds have been disbursed to your account. You received NGN {seller_net}.")
    except Exception as e:
        print(f"Error notifying seller for product_id: {product_id}, seller_chat_id: {seller_chat_id}: {e}")


def notify_seller_payment_received(product_id, buyer_chat_id):
    """
    Notifies the seller and buyer that payment has been verified and the seller is expected to deliver the product.
    """
    # Retrieve product details from Firestore
    product_data = db.collection('products').document(product_id).get().to_dict()
    if not product_data:
        print(f"DEBUG: No product data found for product_id: {product_id}")
        send_message(buyer_chat_id, "We couldn't find the product details. Please contact support for assistance.")
        return

    lister_chat_id = product_data.get('chat_id')
    product_description = product_data.get('description', 'Unknown product')
    seller_phone_number = product_data.get('phone_number', 'Not provided')

    # Retrieve the buyer's email (handle gracefully if missing)
    buyer_email = pending_payment_emails.get(buyer_chat_id, {}).get('email', 'Not provided')

    # Notify the seller
    seller_message = (
        f"The payment for your product '{product_description}' has been successfully made and is now in escrow.\n"
        "The buyer is expecting the product to be delivered. Once the buyer confirms receipt of the product, "
        "the payment will be released to you.\n"
        f"Buyer's Email: {buyer_email}"
    )
    try:
        send_message(lister_chat_id, seller_message)
        print(f"DEBUG: Sent payment received notification to lister_chat_id: {lister_chat_id}")
    except Exception as e:
        print(f"DEBUG: Error sending message to lister_chat_id {lister_chat_id}: {e}")

    # Notify the buyer with the seller's phone number (if available)
    buyer_message = (
        f"Your payment has been verified. The seller has been notified to deliver the product.\n"
        f"Seller's Phone Number: {seller_phone_number}\n"
        "Once you receive the product and are satisfied, please click 'Release Funds' to complete the transaction. "
        "If the seller doesn't deliver or if there are issues with the product, please click 'Request Refund'."
    )
    try:
        send_message(buyer_chat_id, buyer_message)
        print(f"DEBUG: Sent seller's phone number to buyer_chat_id: {buyer_chat_id}")
    except Exception as e:
        print(f"DEBUG: Error sending message to buyer_chat_id {buyer_chat_id}: {e}")


def release_funds(txid):
    """
    Settles (releases) an escrow payment using Flutterwave's settlement API.
    """
    try:
        url = "https://api.ravepay.co/v2/gpx/transactions/escrow/settle"
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json"
        }
        payload = {
            "id": txid,  # Transaction ID from your verify call.
            "secret_key": FLW_SECRET_KEY  # Ensure FLW_SECRET_KEY is set correctly.
        }

        print(f"DEBUG: Sending POST to {url}")
        print(f"DEBUG: Payload: {payload}")

        response = requests.post(url, json=payload, headers=headers)
        print(f"DEBUG: HTTP status code: {response.status_code}")
        print(f"DEBUG: Raw Response: {response.text}")

        if response.status_code == 200:
            try:
                result = response.json()
                return result
            except ValueError as e:
                print(f"DEBUG: Failed to parse JSON response: {e}")
                return {"status": "error", "message": "Invalid JSON response"}
        else:
            return {
                "status": "error",
                "message": f"Failed with status {response.status_code}: {response.text}"
            }

    except requests.exceptions.RequestException as e:
        print(f"DEBUG: Error in release_funds: {e}")
        return {"status": "error", "message": f"Request failed: {str(e)}"}
    except Exception as e:
        print(f"DEBUG: Unexpected error in release_funds: {e}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def release_funds_with_retry(txid, retries=3, delay=5):
    """
    Retries the release funds request in case of a transient (503) error.
    """
    for attempt in range(retries):
        print(f"DEBUG: Attempt {attempt + 1} to release funds for transaction {txid}.")
        result = release_funds(txid)
        status_message = result.get("message", "").lower()

        # If successful or if the escrow is already settled, then return.
        if result.get("status") == "success":
            return result

        # If error message indicates a transient issue (503 service unavailable), then retry.
        if result.get("status") == "error" and "503" in result.get("message", ""):
            print(f"DEBUG: Received a 503 error. Retrying after {delay} seconds...")
            time.sleep(delay)
        else:
            # For errors that are not 503 (or transient), break out of the loop immediately.
            return result

    return {"status": "error", "message": f"Failed to release funds after {retries} attempts."}



def handle_funds_release_flutterwave(callback_query):
    """
    Processes a release funds request initiated via a Telegram callback query.
    Expected callback data format: "rf_{short_product_id}_{sale_index}"
    Example: "rf_Br_UE2Fx_1" where "Br_UE2Fx" is the product short ID and "1" is the sale index.
    Returns True if processing stops to prevent further actions.
    """
    try:
        print("DEBUG: Entered handle_release_funds")
        callback_data = callback_query['data']  # e.g., "rf_Br_UE2Fx_1"
        # Parse the callback data; assuming parse_callback_data is defined properly.
        product_id_short, sale_index = parse_callback_data(callback_data, "rf")
        if not product_id_short or not sale_index:
            send_message(str(callback_query['from']['id']),
                         "❌ Invalid release request format. Please try again.")
            return True  # Stop further processing

        buyer_chat_id = str(callback_query['from']['id'])
        print(f"DEBUG: Parsed product_id_short: {product_id_short} and sale_index: {sale_index}")

        # Retrieve sale mapping document from Firestore.
        mapping_doc = db.collection('sale_mapping').document(product_id_short).get()
        if not mapping_doc.exists:
            send_message(buyer_chat_id, f"❌ Sale mapping not found for product: {product_id_short}")
            return True

        mapping_data = mapping_doc.to_dict()
        sale_id = mapping_data.get(sale_index)
        if not sale_id:
            send_message(buyer_chat_id, "❌ Invalid sale reference.")
            return True
        print(f"DEBUG: Found mapping -> sale_id: {sale_id}")

        # Retrieve the sale document.
        # Assuming sale_id is of format "productId_..." so that the first part is product_id.
        product_id = sale_id.split("_")[0]
        sale_doc_ref = db.collection('products').document(product_id)\
                        .collection('sales').document(sale_id)
        sale_doc = sale_doc_ref.get()
        if not sale_doc.exists:
            send_message(buyer_chat_id, "❌ Sale details not found.")
            return True

        sale_data = sale_doc.to_dict()

        # Check if funds have already been processed: either released or refunded.
        if sale_data.get("status") in ("settled", "refunded"):
            send_message(
                buyer_chat_id,
                "🚫 Funds have already been processed (released or refunded) for this transaction."
            )
            show_main_menu(buyer_chat_id)
            return True  # Immediately stop further processing

        txid = sale_data.get("txid")
        if not txid:
            send_message(buyer_chat_id, "❌ Missing transaction ID. Cannot process release.")
            return True

        # Mark that an action is being initiated.
        if buyer_chat_id in pending_payment_info:
            pending_payment_info[buyer_chat_id]['action_initiated'] = True
            print(f"DEBUG: Marked action_initiated for buyer {buyer_chat_id}.")
        else:
            print(f"DEBUG: No pending payment record found for buyer {buyer_chat_id}.")
            # Depending on your flow you can continue or return here.

        # Verify the transaction.
        transaction_data = verify_transaction(txid)
        print(f"DEBUG: Full transaction_data: {transaction_data}")
        transaction_status = transaction_data.get("data", {}).get("status", "")
        if not transaction_status:
            transaction_status = transaction_data.get("status", "")
        print(f"DEBUG: transaction_status: {transaction_status}")

        if transaction_status.lower() != "successful":
            send_message(buyer_chat_id, "❌ Transaction verification failed or was not successful.")
            return True  # Stop further processing on failure

            # Call the release funds function using the transaction ID.
        result = release_funds(txid)
        print(f"DEBUG: Release funds response: {result}")
        if result.get("status") == "success":
            # Update Firestore to mark the sale as settled.
            sale_doc_ref.update({"status": "settled"})
            send_message(buyer_chat_id, "✅ Funds successfully released.")

            # Optionally, disburse funds to the seller.
            seller_chat_id = sale_data.get("seller_chat_id")
            disburse_funds_to_seller(seller_chat_id, product_id)

            # Update the seller's sales counter in Firestore.
            update_sales_count(seller_chat_id)

            # Retrieve the updated sales count.
            total_sales = get_sales_count(seller_chat_id)

            # Notify the seller of their updated sales count.
            send_message(seller_chat_id, f"🎉 Congratulations! You now have {total_sales} sales!")

            # Return False here to indicate processing can continue.
            return False
        else:
            send_message(buyer_chat_id,
                            f"❌ Funds release failed: {result.get('message', 'Unknown error')}")
            return True
    except Exception as e:
        print(f"DEBUG: Error in handle_funds_release_flutterwave: {e}")
        send_message(str(callback_query['from']['id']), "❌ Error processing release funds.")
        return True
    finally:
        show_main_menu(str(callback_query['from']['id']))


def update_sales_count(seller_chat_id):
    """Increment the seller's sales counter by 1."""
    seller_ref = db.collection('sellers').document(seller_chat_id)
    try:
        seller_ref.update({'sales_count': firestore.Increment(1)})
        print(f"DEBUG: Updated sales count for seller {seller_chat_id}.")
    except Exception as e:
        print(f"DEBUG: Error updating sales count for seller {seller_chat_id}: {e}")

def get_sales_count(seller_chat_id):
    seller_ref = db.collection('sellers').document(seller_chat_id)
    try:
        doc = seller_ref.get()
        if doc.exists:
            sales_count = doc.get('sales_count')
            if sales_count is None:
                sales_count = 0
            print(f"DEBUG: Seller {seller_chat_id} has {sales_count} sales.")
            return sales_count
        else:
            print(f"DEBUG: No seller document found for {seller_chat_id}.")
            return 0
    except Exception as e:
        print(f"DEBUG: Error retrieving sales count for seller {seller_chat_id}: {e}")
        return 0






def settle_escrow_payment(txid):
    """
    Settles an escrow payment using Flutterwave's settlement endpoint.
    txid: Transaction ID returned from the payment verification.
    """
    url = "https://api.ravepay.co/v2/gpx/transactions/escrow/settle"
    headers = {"Content-Type": "application/json"}
    payload = {
        "id": str(txid),
        "secret_key": FLW_SECRET_KEY
    }

    try:
        # Debugging: Log payload and headers
        print(f"DEBUG: Payload being sent to API: {payload}")
        print(f"DEBUG: Headers being sent: {headers}")

        # Make the API request
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        # Debugging: Log full response
        print(f"DEBUG: HTTP status code: {response.status_code}")
        print(f"DEBUG: Escrow settlement response for txid {txid}: {response_data}")

        # Handle non-200 status codes
        if response.status_code != 200:
            print(f"DEBUG: Non-200 status code received: {response.status_code}")
            print(f"DEBUG: Response content: {response.text}")
            return {"status": "error", "message": f"HTTP {response.status_code}: {response.text}"}

        return response_data
    except requests.exceptions.RequestException as e:
        print(f"DEBUG: Network error during escrow settlement for txid {txid}: {e}")
        return {"status": "error", "message": f"Network error: {e}"}
    except Exception as e:
        print(f"DEBUG: Unexpected error during escrow settlement for txid {txid}: {e}")
        return {"status": "error", "message": str(e)}


def parse_callback_data(callback_data, prefix):
    """
    Parses callback data of the form:
         "{prefix}_{short_product_id}_{sale_index}"
    For example, for a refund: "rr_Br_UE2Fx_1"
    This removes the prefix and splits on the rightmost underscore.

    Returns:
         (short_product_id, sale_index) or (None, None) if parsing fails.
    """
    expected_prefix = prefix + "_"
    if not callback_data.startswith(expected_prefix):
        return None, None
    trimmed = callback_data[len(expected_prefix):]  # e.g., "Br_UE2Fx_1"
    parts = trimmed.rsplit("_", 1)  # Yields ["Br_UE2Fx", "1"]
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def calculate_net_refund(sale_data, transaction_data):
    """
    Calculate and return the net refund amount.

    This function first tries to use the 'amount_settled' field from the transaction data.
    If that field isn’t available, it falls back to computing:
         net_refund = sale_data["amount"] - app_fee
    """
    try:
        # Check for top-level 'amount_settled'
        if "amount_settled" in transaction_data and str(transaction_data["amount_settled"]).strip():
            print(f"DEBUG: Using top-level amount_settled: {transaction_data['amount_settled']}")
            return float(transaction_data["amount_settled"])

        # Check inside the nested 'data' object.
        data_obj = transaction_data.get("data", {})
        if "amount_settled" in data_obj and str(data_obj["amount_settled"]).strip():
            print(f"DEBUG: Using nested amount_settled: {data_obj['amount_settled']}")
            return float(data_obj["amount_settled"])

        # Fallback: use gross sale amount minus fee.
        gross_amount = float(sale_data["amount"])
        fee = 0
        if "app_fee" in transaction_data and str(transaction_data.get("app_fee")).strip():
            fee = float(transaction_data["app_fee"])
        else:
            fee = float(data_obj.get("app_fee", 0))
        net_refund = gross_amount - fee
        print(f"DEBUG: Fallback calculation: gross {gross_amount} - fee {fee} = {net_refund}")
        return net_refund
    except Exception as e:
        print("DEBUG: Error calculating net refund:", e)
        return 0


def refund_payment(txid, comments="Customer requested refund due to product dissatisfaction"):
    try:
        if not txid:
            raise ValueError("Transaction ID (txid) is required.")

        # Use the escrow refund endpoint as per Flutterwave's documentation.
        refund_url = "https://api.ravepay.co/v2/gpx/transactions/escrow/refund"
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json"
        }
        payload = {
            "id": txid,  # The transaction ID obtained from the verify response.
            "comment": comments,  # Reason for the refund.
            "secret_key": FLW_SECRET_KEY  # Your merchant secret key.
        }

        print(f"DEBUG: Sending POST to {refund_url}")
        print(f"DEBUG: Payload: {payload}")
        response = requests.post(refund_url, headers=headers, json=payload)

        print(f"DEBUG: HTTP status code: {response.status_code}")
        print(f"DEBUG: Response: {response.text}")

        if response.status_code == 200:
            try:
                result = response.json()
                print(f"DEBUG: Refund response: {result}")
                return {"status": "success", "data": result}
            except ValueError:
                print("DEBUG: Failed to parse JSON response.")
                return {"status": "error", "message": "Invalid JSON response from API"}
        else:
            return {
                "status": "error",
                "message": f"Failed with status {response.status_code}: {response.text}"
            }
    except Exception as e:
        print(f"DEBUG: Exception in refund_payment: {e}")
        return {"status": "error", "message": str(e)}

def refund_payment_with_retry(txid, comments="Customer requested refund due to product dissatisfaction", retries=3, delay=5):
    """
    Attempts to process a refund, retrying a specified number of times if a transient error (e.g., HTTP 503)
    is encountered.
    """
    for attempt in range(retries):
        print(f"DEBUG: Attempt {attempt+1} to refund payment for transaction {txid}.")
        result = refund_payment(txid, comments)
        # If refund was successful, just return the result.
        if result.get("status") == "success":
            return result
        # If we get a 503 error (or other transient error), retry after a short delay.
        elif result.get("status") == "error" and "503" in result.get("message", ""):
            print(f"DEBUG: Received a 503 error on refund. Retrying in {delay} seconds...")
            time.sleep(delay)
        # Optionally, you might also decide to retry on other server errors.
        elif result.get("status") == "error" and "500" in result.get("message", ""):
            print(f"DEBUG: Received a 500 error on refund. Retrying in {delay} seconds...")
            time.sleep(delay)
        else:
            # For non-transient errors, don't retry.
            return result

    return {"status": "error", "message": f"Refund failed after {retries} attempts."}






def handle_refund_flutterwave(callback_query):
    """
    Processes a refund request initiated via a Telegram callback query.
    Expected callback data format: "rr_{short_product_id}_{sale_index}"
    Example: "rr_Br_UE2Fx_1" (short_product_id="Br_UE2Fx", sale_index="1")
    Returns True if the processing should stop further actions.
    """
    try:
        print("DEBUG: Entered handle_refund_flutterwave")
        callback_data = callback_query['data']  # e.g., "rr_Br_UE2Fx_1"
        buyer_chat_id = str(callback_query['from']['id'])

        expected_prefix = "rr_"
        if not callback_data.startswith(expected_prefix):
            send_message(buyer_chat_id, "❌ Invalid refund request format.")
            return True  # stop further processing

        # Safely parse product_id_short and sale_index.
        trimmed = callback_data[len(expected_prefix):]  # e.g., "Br_UE2Fx_1"
        product_id_short, sale_index = trimmed.rsplit("_", 1)
        # Remove extra "confirm_" if present.
        if product_id_short.startswith("confirm_"):
            product_id_short = product_id_short[len("confirm_"):]
        print(f"DEBUG: Parsed product_id_short: {product_id_short} and sale_index: {sale_index}")

        # Get sale mapping from Firestore.
        mapping_doc = db.collection("sale_mapping").document(product_id_short).get()
        if not mapping_doc.exists:
            send_message(buyer_chat_id, f"❌ No sale mapping found for product: {product_id_short}")
            print(f"DEBUG: Mapping document not found for product_id_short: {product_id_short}")
            return True
        mapping_data = mapping_doc.to_dict()
        print(f"DEBUG: Sale mapping document data: {mapping_data}")

        sale_id = mapping_data.get(sale_index)
        if not sale_id:
            send_message(buyer_chat_id, "❌ Invalid sale reference.")
            return True

        print(f"DEBUG: Found mapping -> sale_id: {sale_id}")

        # Extract full product_id (handles underscores safely).
        product_id = "_".join(sale_id.split("_")[:-1])
        print(f"DEBUG: Extracted product_id: {product_id}")

        sale_ref = db.collection("products").document(product_id).collection("sales").document(sale_id)
        sale_doc = sale_ref.get()
        if not sale_doc.exists:
            send_message(buyer_chat_id, "❌ Sale details not found.")
            return True

        sale_data = sale_doc.to_dict()

        # Check if the sale is already settled or refunded.
        if sale_data.get("status") in ("settled", "refunded"):
            send_message(buyer_chat_id,
                         "🚫 Funds have already been released or refunded for this transaction.")
            show_main_menu(buyer_chat_id)
            return True  # Immediately stop further processing

        txid = sale_data.get("txid")
        if not txid:
            send_message(buyer_chat_id, "❌ Transaction ID missing. Cannot process refund.")
            return True

        # Verify the transaction.
        transaction_data = verify_transaction(txid)
        print(f"DEBUG: Full transaction_data: {transaction_data}")

        # Optionally, check if the transaction metadata indicates a refund.
        transaction_meta = transaction_data.get("meta", {})
        if transaction_meta.get("rave_escrow_tx", "").upper() == "REFUNDED":
            send_message(buyer_chat_id, "❌ This transaction has already been refunded.")
            show_main_menu(buyer_chat_id)
            return True  # Stop further processing

        transaction_status = transaction_data.get("data", {}).get("status", "") or transaction_data.get("status", "")
        print(f"DEBUG: transaction_status: {transaction_status}")

        if transaction_status.lower() == "successful":
            print(f"DEBUG: Transaction verified as successful for sale_id: {sale_id}")

            # (Optional) Calculate net refund amount for debugging.
            net_refund_amount = calculate_net_refund(sale_data, transaction_data)
            print(f"DEBUG: Final net refund amount (not used in API): {net_refund_amount}")

            # Mark the refund action so that resend logic won't repeat this action.
            if buyer_chat_id in pending_payment_info:
                pending_payment_info[buyer_chat_id]['action_initiated'] = True
                print(f"DEBUG: Marked action_initiated for buyer {buyer_chat_id}.")
            else:
                pending_payment_info[buyer_chat_id] = {
                    "product_id": product_id,
                    "payment_complete": True,
                    "action_initiated": True
                }
                print(f"DEBUG: Created new pending payment info for buyer {buyer_chat_id} with action_initiated=True.")

            # Process refund with the updated escrow refund API.
            custom_comment = "Customer requested refund due to product dissatisfaction"
            result = refund_payment(txid, comments=custom_comment)
            print(f"DEBUG: Refund response from Flutterwave: {result}")

            # Update Firestore and inform the user.
            if result.get("status") == "success":
                send_message(buyer_chat_id, "✅ Refund successfully processed.")
                sale_ref.update({"status": "refunded"})
            else:
                send_message(buyer_chat_id, f"❌ Refund failed: {result.get('message', 'Unknown error')}")
        else:
            send_message(buyer_chat_id, "❌ Transaction verification failed or was not successful.")
            print(f"DEBUG: Transaction verification failed for txid: {txid}")

    except Exception as e:
        print(f"DEBUG: Error in handle_refund_flutterwave: {e}")
        send_message(buyer_chat_id, "❌ Error processing refund.")
    return True  # Ensure processing stops if this function returns True








def initiate_flutterwave_refund(payment_reference, comment="Refund requested by buyer", amount=None):
    """
    Initiates a refund for an escrow payment via Flutterwave's Refund API.
    - payment_reference: The transaction ID (txid) from payment verification.
    - comment: Reason for refund.
    - amount: Optionally, specify an amount for a partial refund.
    """
    url = "https://api.ravepay.co/v2/gpx/transactions/escrow/refund"
    headers = {"Content-Type": "application/json"}
    payload = {
        "id": str(payment_reference),
        "comment": comment,
        "secret_key": FLW_SECRET_KEY
    }
    if amount:
        payload["amount"] = str(amount)  # Ensure amount is a string if required

    try:
        response = requests.post(url, json=payload, headers=headers)
        return response.json()
    except Exception as e:
        print(f"DEBUG: Exception during refund initiation: {e}")
        return {"status": "error", "message": str(e)}


def verify_webhook(payload, signature, secret_hash):
    computed_signature = hmac.new(
        secret_hash.encode(),  # Ensure no extra spaces or newlines
        payload,               # Raw payload (bytes)
        hashlib.sha256         # SHA-256 algorithm
    ).hexdigest()
    print("DEBUG: Computed signature:", computed_signature)  # For troubleshooting only
    print("DEBUG: Received signature:", signature)
    return hmac.compare_digest(computed_signature, signature)


def handle_flutterwave_event(data):
    """
    Handles webhook events from Flutterwave.
    Expects data to be the payload from Flutterwave's webhook.
    """
    if data.get('event') == 'charge.completed' and data.get('data', {}).get('status') == 'successful':
        payment_details = data['data']
        reference = payment_details.get('tx_ref')  # Flutterwave's reference field
        amount = payment_details.get('amount')  # Amount is in Naira
        buyer_email = payment_details.get('customer', {}).get('email', 'Unknown')

        # Extract escrow status from the meta field
        meta = payment_details.get('meta', [])
        escrow_status = None
        for m in meta:
            if m.get('metaname') == 'rave_escrow_tx':
                escrow_status = m.get('metavalue')
                break

        print(f"Payment successful: Reference={reference}, Amount=₦{amount}, Buyer={buyer_email}")
        print(f"DEBUG: Escrow status: {escrow_status}")

        # Notify the buyer only if their chat id can be extracted
        buyer_chat_id = extract_buyer_chat_id(reference)  # Ensure this function extracts based on your tx_ref format
        if buyer_chat_id:
            # Inform the buyer whether funds are still held in escrow or settled
            if escrow_status == "1":
                send_message(buyer_chat_id,
                             "Payment successful! Your funds are secure in escrow until you confirm product delivery.")
            elif escrow_status == "SETTLED":
                send_message(buyer_chat_id, "Payment successful! The funds have been released to the seller.")
            else:
                send_message(buyer_chat_id, "Payment successful! (Escrow status unknown)")

        # Update product status or perform further actions
        mark_payment_successful(reference)
    else:
        print(
            f"Unhandled event type or unsuccessful transaction: {data.get('event')}, status: {data.get('data', {}).get('status')}")


def extract_buyer_chat_id(reference):
    # Assume the reference is in format: "buyerChatID_productID_timestamp"
    try:
        # Attempt to split the reference and extract the buyer chat ID
        parts = reference.split('_')
        if len(parts) < 3:
            raise ValueError("Invalid reference format: Less than 3 parts")
        return parts[0]  # Return the buyer chat ID (first part)
    except (IndexError, ValueError) as e:
        # Log specific error if format is incorrect
        print(f"Error extracting buyer chat ID from reference '{reference}': {str(e)}")
        return None


def mark_payment_successful(reference):
    """
    Marks the payment as successful for the given reference.
    Updates the payment status in Firestore and logs the outcome.
    """
    try:
        # Parse the reference to extract the product ID
        parts = reference.split('_')
        if len(parts) < 2:
            print(f"Invalid reference format: {reference}")
            return

        product_id = parts[1]
        product_ref = db.collection('products').document(product_id)
        product_snapshot = product_ref.get()

        if product_snapshot.exists:
            product_data = product_snapshot.to_dict()
            # Update the payment status in Firestore
            product_ref.update({'payment_status': 'successful'})
            print(f"Payment status updated to 'successful' for product_id: {product_id}")
        else:
            print(f"Product not found in Firestore for product_id: {product_id}")

    except Exception as e:
        print(f"Error in marking payment as successful: {str(e)}")


def test_inline_keyboard(chat_id, sale_doc_id, product_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "Test Button", "callback_data": f"test_{sale_doc_id}_{product_id}"}]
        ]
    }
    send_message_with_keyboard(chat_id, "Test inline keyboard:", keyboard)


def handle_escrow_for_payment(reference, payment_details):
    """
    Handles the escrow process after a successful payment.
    Expects payment reference in the format: "buyerID_productID_timestamp"
    and uses Firestore to retrieve product details.
    Notifies the buyer with the seller’s phone number and provides buttons
    for releasing funds or requesting a refund.
    """
    # Parse the payment reference
    parts = reference.split('_')
    if len(parts) < 2:
        print("Invalid payment reference format.")
        return

    buyer_chat_id = parts[0]
    product_id = parts[1]

    print(f"DEBUG: Looking for product with ID: {product_id}")

    # Retrieve product details from the 'products' collection
    product_doc = db.collection('products').document(product_id).get()
    product_data = product_doc.to_dict() if product_doc.exists else None

    # Fallback: if not found in 'products', try 'pending_products'
    if not product_data:
        print(f"DEBUG: Product {product_id} not found in 'products' collection. Checking 'pending_products'.")
        product_doc = db.collection('pending_products').document(product_id).get()
        product_data = product_doc.to_dict() if product_doc.exists else None

    if not product_data:
        print(f"ERROR: Product {product_id} not found in the database.")
        return

    # Extract seller information
    seller_chat_id = product_data.get('chat_id')
    seller_phone = product_data.get('phone_number', 'Not provided')
    description = product_data.get('description', 'your product')

    if not seller_chat_id:
        print("ERROR: Seller's chat ID is missing.")
        return

    # Compose the buyer's message including seller's contact number
    buyer_message = (
        f"Your payment for {description} has been successfully received.\n"
        f"Seller's Contact Number: {seller_phone}\n\n"
        "Once you receive the product and are satisfied, please click 'Release Funds' to complete the transaction.\n"
        "If the seller doesn't deliver or if there are issues with the product, please click 'Request Refund'."
    )

    # Provide an inline keyboard for buyer options
    keyboard = {
        "inline_keyboard": [
            [{"text": "Release Funds", "callback_data": f"release_funds_{product_id}"}],
            [{"text": "Request Refund", "callback_data": f"refund_{product_id}"}]
        ]
    }

    # Compose the seller's message
    seller_message = (
        f"You have received payment for {description}.\n"
        "Please arrange for prompt delivery.\n"
        "Once the buyer confirms receipt of the product, the funds will be released to you."
    )

    # Send notifications to buyer and seller
    try:
        # Replace direct call with the retry-based call for critical buyer message.
        response_buyer = send_message_with_keyboard_retry(buyer_chat_id, buyer_message, keyboard)
        response_seller = send_message(seller_chat_id, seller_message)
        print("DEBUG: Escrow notifications sent.")
        print(f"DEBUG: Buyer message response: {response_buyer}")
        print(f"DEBUG: Seller message response: {response_seller}")
    except Exception as e:
        print(f"ERROR: Error sending escrow messages: {e}")




def verify_payment_with_flutterwave(payment_id):
    """
    Verifies a transaction using Flutterwave's Verify Payment API.
    Returns a tuple: (escrow_status, payment_data)
    - escrow_status: the value of 'rave_escrow_tx' from the meta field, e.g., "1" for held, "SETTLED" if settled.
    - payment_data: full payment details from the verification response.
    """
    url = f"https://api.flutterwave.com/v3/transactions/{payment_id}/verify"
    headers = {"Authorization": f"Bearer {FLW_SECRET_KEY}"}

    try:
        response = requests.get(url, headers=headers)
        response_data = response.json()

        # Debugging log for the complete response
        print(f"DEBUG: Payment verification response: {json.dumps(response_data, indent=2)}")

        if response.status_code == 200 and response_data.get("status") == "success":
            payment_data = response_data.get("data", {})
            escrow_status = None

            # Extract the escrow status from meta details
            meta = payment_data.get("meta", [])
            for m in meta:
                if m.get("metaname") == "rave_escrow_tx":
                    escrow_status = m.get("metavalue")
                    break

            if escrow_status is None:
                print(f"DEBUG: No escrow status found in meta for payment_id {payment_id}")

            return escrow_status, payment_data
        else:
            error_message = response_data.get("message", "Unknown error")
            print(f"DEBUG: Payment verification failed: {error_message}")
            return "error", error_message
    except requests.exceptions.Timeout:
        print(f"ERROR: Timeout error during payment verification for payment_id {payment_id}")
        return "error", "Request timed out"
    except requests.exceptions.TooManyRedirects:
        print(f"ERROR: Too many redirects during payment verification for payment_id {payment_id}")
        return "error", "Too many redirects"
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Request error during payment verification for payment_id {payment_id}: {e}")
        return "error", f"Request error: {e}"
    except Exception as e:
        print(f"ERROR: Unexpected error during payment verification for payment_id {payment_id}: {e}")
        return "error", f"Unexpected error: {e}"





def handle_payment_webhook(webhook_data):
    try:
        # Log the full webhook payload for detailed debugging
        print(f"DEBUG: Full Webhook Payload: {json.dumps(webhook_data, indent=2)}")

        # Extract the 'data' portion and log it
        data_part = webhook_data.get("data", {})
        print(f"DEBUG: Data part: {json.dumps(data_part, indent=2)}")

        # Retrieve the raw status value and log it
        raw_status = data_part.get("status")
        print(f"DEBUG: Raw status value: {raw_status}")

        # Use strip() in case there are extra spaces and lower-case the value
        payment_status = (raw_status or "").strip().lower()
        print(f"DEBUG: Extracted payment_status: {payment_status}")

        # Check if payment is successful
        if payment_status != "successful":
            print(f"DEBUG: Payment not successful. Status: {payment_status}")
            return

        # Extract other required fields from the payload
        product_id = webhook_data.get("meta_data", {}).get("product_id")
        txid = data_part.get("id")
        reference = data_part.get("tx_ref")

        # Split the reference into product_id and timestamp
        if reference:
            reference_parts = reference.split('_')
            if len(reference_parts) < 2:
                print(f"DEBUG: Invalid reference format: {reference}")
                return
            product_id_from_ref = reference_parts[0]
            timestamp_from_ref = reference_parts[1]
            print(f"DEBUG: Extracted Product ID: {product_id_from_ref}, Timestamp: {timestamp_from_ref}")
        else:
            print("DEBUG: No reference found in the webhook data.")
            return

        # Query Firestore for the corresponding sale record using both product_id and timestamp
        sales_query = (
            db.collection('products').document(product_id_from_ref).collection('sales')
            .where("product_id", "==", product_id_from_ref)  # Filter by product ID
            .where("timestamp", "==", int(timestamp_from_ref))  # Filter by timestamp (make sure it's an integer)
            .limit(1)
        )
        sales = list(sales_query.stream())
        if not sales:
            print(f"DEBUG: No matching sale record found for product_id: {product_id_from_ref} and timestamp: {timestamp_from_ref}")
            return

        # Update the sale record with the txid and mark as 'settled'
        sale_doc = sales[0]
        sale_doc.reference.update({"txid": txid, "status": "settled"})
        print(f"DEBUG: Updated sale record {sale_doc.id} with txid: {txid}, marked as 'settled'")

    except Exception as e:
        print(f"DEBUG: Error handling payment webhook: {e}")


def add_sale_mapping(index, product_id, sale_id):
    try:
        # Reference to the sale_mapping document for the product
        sale_mapping_ref = db.collection('sale_mapping').document(product_id)

        # Prepare the data as a dictionary where index points to the sale_id
        update_data = {str(index): sale_id}

        # Update Firestore without overwriting existing data
        sale_mapping_ref.set(update_data, merge=True)

        print(f"Sale data added: Product {product_id}, Index {index}, Sale ID {sale_id}")
    except Exception as e:
        print(f"Error adding sale mapping: {e}")


def add_multiple_sales(sales):
    try:
        sale_mapping = {}

        for index, sale in enumerate(sales, start=1):  # Start index from 1
            product_id = sale['product_id']
            sale_id = sale['sale_id']

            # Group sales under the same product ID
            if product_id not in sale_mapping:
                sale_mapping[product_id] = {}

            sale_mapping[product_id][str(index)] = sale_id  # Store using string keys

            # Add reverse mapping
            add_reverse_sale_mapping(sale_id, product_id, index)

        # Update Firestore for each product
        for product_id, mapping_data in sale_mapping.items():
            sale_mapping_ref = db.collection('sale_mapping').document(product_id)
            sale_mapping_ref.set(mapping_data, merge=True)
            print(f"Updated sale mapping for {product_id}: {mapping_data}")

        print("All sales added successfully.")
    except Exception as e:
        print(f"Error adding multiple sales: {e}")


def add_reverse_sale_mapping(sale_id, product_id, index):
    try:
        db.collection('sale_index').document(sale_id).set({
            'product_id': product_id,
            'index': str(index)
        })
        print(f"Reverse mapping saved for sale_id {sale_id} -> product_id {product_id}, index {index}")
    except Exception as e:
        print(f"Error saving reverse mapping: {e}")


def get_sale_reference_from_index(sale_id):
    try:
        # Step 1: Look up reverse mapping from sale_index collection
        doc = db.collection('sale_index').document(sale_id).get()
        if not doc.exists:
            print(f"No reverse mapping found for sale_id: {sale_id}")
            return None

        data = doc.to_dict()
        product_id = data['product_id']
        index = data['index']

        # Step 2: Fetch the actual sale_id using product_id + index
        mapping_doc = db.collection('sale_mapping').document(product_id).get()
        if not mapping_doc.exists:
            print(f"No sale_mapping found for product_id: {product_id}")
            return None

        sale_mapping = mapping_doc.to_dict()
        sale_ref = sale_mapping.get(index)

        print(f"Resolved sale_id {sale_id} -> sale_ref {sale_ref}")
        return sale_ref
    except Exception as e:
        print(f"Error in get_sale_reference_from_index: {e}")
        return None




def create_preauth_charge(card_details, amount, currency, tx_ref, customer_info):
    """
    Creates a preauthorized (manual capture) charge in Flutterwave test mode.

    Args:
        card_details (dict): Card information (e.g., number, expiry month/year, CVV).
        amount (str or number): The amount you wish to preauthorize.
        currency (str): The currency code (e.g., "NGN").
        tx_ref (str): Your unique transaction reference.
        customer_info (dict): Customer details (e.g., name, email, phone).

    Returns:
        dict: Flutterwave's response which should include 'flw_ref' if successful.
    """
    payload = {
        "amount": str(amount),
        "currency": currency,
        "tx_ref": tx_ref,
        "preauthorize": True,  # Ensures the charge is preauthorized (manual capture)
        "card": card_details,
        "customer": customer_info,
        # You may add or update additional required parameters here
    }
    endpoint = "https://api.flutterwave.com/v3/charges?type=card"
    headers = {
        "Authorization": f"Bearer FLWSECK_TEST-SANDBOXDEMOKEY-X",
        "Content-Type": "application/json"
    }
    response = requests.post(endpoint, json=payload, headers=headers)
    print(f"DEBUG: create_preauth_charge response status: {response.status_code}")
    print(f"DEBUG: create_preauth_charge response: {response.text}")
    return response.json()

def capture_funds(flw_ref):
    """
    Captures funds for a preauthorized charge.
    """
    endpoint = f"https://api.flutterwave.com/v3/charges/{flw_ref}/capture"
    headers = {
        "Authorization": f"Bearer FLWSECK_TEST-SANDBOXDEMOKEY-X",  # Replace with your API key
    }
    response = requests.post(endpoint, headers=headers)
    print(f"DEBUG: capture_funds response status: {response.status_code}")
    print(f"DEBUG: capture_funds response: {response.text}")
    return response.json()


def verify_transaction(txid):
    try:
        url = f"https://api.flutterwave.com/v3/transactions/{txid}/verify"
        headers = {
            "Authorization": f"Bearer {FLW_SECRET_KEY}"
        }
        response = requests.get(url, headers=headers)

        print(f"DEBUG: Verify transaction response: {response.status_code}")
        print(f"DEBUG: Response body: {response.text}")

        if response.status_code == 200:
            result = response.json()
            return result.get("data", {})  # Return the 'data' section directly
        else:
            return {"status": "error", "message": "Failed to verify transaction"}
    except Exception as e:
        print(f"DEBUG: Error verifying transaction: {e}")
        return {"status": "error", "message": str(e)}


def verify_bank_details(bank_code, account_number, account_name, FLW_SECRET_KEY, test_mode=False):
    """
    Verifies bank details using the Flutterwave API.
    In test mode, the account number and account name are overridden with test values.
    Returns a tuple (is_valid, error_message).
    """
    if test_mode:
        # Use Flutterwave test values.
        account_number = "0690000030"
        account_name = "Tony Blair"

    payload = {
        "account_number": account_number,
        "account_bank": bank_code
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {FLW_SECRET_KEY}"
    }

    try:
        url = "https://api.flutterwave.com/v3/accounts/resolve"
        response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        print("DEBUG: Flutterwave response:", response.text)
        response_data = response.json()

        if response_data.get("status") == "success":
            resolved_account_name = response_data.get("data", {}).get("account_name", "")
            if resolved_account_name.lower().strip() == account_name.lower().strip():
                return True, None
            else:
                print("DEBUG: Resolved account name:", repr(resolved_account_name))
                print("DEBUG: Provided account name:", repr(account_name))
                return False, "Provided account name does not match the bank records. 😕"
        else:
            return False, response_data.get("message", "Verification failed 😕")
    except requests.exceptions.RequestException as e:
        return False, f"Request failed: {str(e)} 😕"


def search_banks(bank_name, FLW_SECRET_KEY, test_mode=False):
    """
    Searches for Nigerian banks using the Flutterwave API.
    Uses both a direct substring search and fuzzy matching.
    Returns a list of bank dictionaries.
    """
    url = "https://api.flutterwave.com/v3/banks/NG"
    headers = {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
        response_data = response.json()

        # Debug: Print full API response for inspection.
        print("DEBUG: Full API response:", response_data)

        print(f"DEBUG: Bank List Status: {response.status_code}")
        for bank in response_data.get("data", []):
            print(f"    - {bank['name']} ({bank.get('code', 'No code')})")

        if response_data.get("status") == "success":
            banks = response_data.get("data", [])
            normalized_input = bank_name.lower().replace(" ", "")
            print(f"DEBUG: Normalized bank input: {normalized_input}")

            # Step 1: Substring search on normalized names
            matched = [
                bank for bank in banks
                if normalized_input in bank["name"].lower().replace(" ", "")
            ]
            print(f"DEBUG: Substring match found {len(matched)} banks for '{bank_name}'")

            # Step 2: Fuzzy matching if no substring match is found
            if not matched:
                bank_names = [bank["name"].lower() for bank in banks]
                close_matches = difflib.get_close_matches(bank_name.lower(), bank_names, n=5, cutoff=0.6)
                print(f"DEBUG: Fuzzy close matches found: {close_matches}")
                matched = [bank for bank in banks if bank["name"].lower() in close_matches]
                print(f"DEBUG: Fuzzy match found {len(matched)} banks for '{bank_name}'")

            return matched
        else:
            print(f"DEBUG: API Error: {response_data.get('message', 'No message')}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"Error calling Flutterwave API: {e}")
        return []


def generate_bank_keyboard(matching_banks, test_mode=False):
    """
    Generates an InlineKeyboardMarkup from a list of bank dictionaries.
    In test mode, forces the bank code to "044"; otherwise uses the actual code.
    Each button includes a bank emoji (🏦) and the bank name.
    Only the first three banks from the matching list are used.
    """
    keyboard = []
    # Limit to at most 3 bank options.
    for bank in matching_banks[:3]:
        bank_name = bank.get("name", "Unknown Bank")
        bank_code = "044" if test_mode else bank.get("code", "")
        button_text = f"🏦 {bank_name}"
        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"select_bank_{bank_code}")])
    return InlineKeyboardMarkup(keyboard)


def handle_bank_selection(user_input, chat_id, FLW_SECRET_KEY, test_mode=False):
    """
    Handles bank name input.
      - Searches for banks using the provided input.
      - Generates an inline keyboard (with up to 3 options) for user selection.
      - Always forces the user to select one of the shown options.
    Emojis are used to improve the interface.
    """
    matching_banks = search_banks(user_input, FLW_SECRET_KEY, test_mode=test_mode)

    if not matching_banks:
        send_message(chat_id, "😕 No matching banks found. Please try again.")
        return

    # Always present an inline keyboard so that the user must select one of the options.
    keyboard = generate_bank_keyboard(matching_banks, test_mode=test_mode)
    send_message(chat_id, "Please select your bank from the options below: 🏦👇",
                 parse_mode="Markdown", reply_markup=keyboard)



def create_chat_session(buyer_id, seller_id):
    session_id = str(uuid4())
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=3)  # Expires in 3 days
    session_data = {
        "buyer_id": buyer_id,
        "seller_id": seller_id,
        "active": False,  # Becomes True when the buyer clicks "Start Chat"
        "created_at": firestore.SERVER_TIMESTAMP,
        "last_activity": firestore.SERVER_TIMESTAMP,
        "expires_at": expires_at  # Useful for TTL or scheduled cleanup
    }
    db.collection('chat_sessions').document(session_id).set(session_data)
    print(f"DEBUG: Chat session {session_id} created for buyer {buyer_id} and seller {seller_id}")
    return session_id


def buyer_message_handler(update, context):
    buyer_id = str(update.effective_user.id)
    # Query for an active chat session for this buyer.
    query = db.collection('chat_sessions') \
        .where("buyer_id", "==", buyer_id) \
        .where("active", "==", True).stream()
    session = None
    for s in query:
        session = s  # Assuming one active session per buyer.
        break
    if not session:
        send_message(buyer_id, "No active chat session found. Please click the 'Start Chat' button.")
        return

    session_data = session.to_dict()
    seller_id = session_data.get("seller_id")
    context.bot.send_message(chat_id=seller_id,
                             text=f"Buyer: {update.message.text}")

    # Update the last activity timestamp to extend the session.
    session.reference.update({
        "last_activity": firestore.SERVER_TIMESTAMP
    })


def seller_message_handler(update, context):
    seller_id = str(update.effective_user.id)
    # Query for an active session for this seller.
    query = db.collection('chat_sessions') \
        .where("seller_id", "==", seller_id) \
        .where("active", "==", True).stream()
    session = None
    for s in query:
        session = s
        break
    if not session:
        send_message(seller_id, "No active chat session found.")
        return

    session_data = session.to_dict()
    buyer_id = session_data.get("buyer_id")
    session_id = session.id  # Use this in the callback data for the “End Chat” button.

    # Build inline keyboard for ending the chat.
    keyboard = {
        "inline_keyboard": [
            [{"text": "End Chat", "callback_data": f"end_chat:{session_id}"}]
        ]
    }
    context.bot.send_message(chat_id=buyer_id,
                             text=f"Seller: {update.message.text}",
                             reply_markup=keyboard)

    # Update last_activity timestamp.
    session.reference.update({
        "last_activity": firestore.SERVER_TIMESTAMP
    })


def message_handler(update, context):
    if update.message.text:
        update.message.reply_text("Received text: " + update.message.text)
    elif update.message.photo:
        update.message.reply_text("Received an image!")
    elif update.message.video:
        update.message.reply_text("Received a video!")
    else:
        update.message.reply_text("Received something else.")




def cleanup_inactive_sessions(event, context):
    db = firestore.client()
    threshold = datetime.utcnow() - timedelta(days=3)
    sessions_ref = db.collection('chat_sessions')
    inactive_sessions = sessions_ref.where("last_activity", "<", threshold).stream()

    for session in inactive_sessions:
        print(f"Deleting inactive session: {session.id}")
        session.reference.delete()


# Helper function to send a message with a cancel onboarding button
def send_text_with_cancel(chat_id, text):
    keyboard = [
        [InlineKeyboardButton(text='❌ Cancel Onboarding', callback_data='cancel_onboarding')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Use your existing send_message function to send the message with the inline keyboard
    send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)


def handle_onboarding_message(message):
    chat_id = str(message['chat']['id'])

    # If onboarding hasn't been started for this chat, do nothing.
    if chat_id not in pending_onboarding:
        return False

    # Allow cancellation from any step using a text command.
    text = message.get("text", "").strip()
    if text.lower() == "/cancel":
        if chat_id in pending_onboarding:
            del pending_onboarding[chat_id]
        send_message(chat_id, "🚫 Onboarding cancelled. Returning to main menu.")
        show_main_menu(chat_id)
        return True

    current_state = pending_onboarding[chat_id].get("state")

    if current_state == "awaiting_business_name":
        pending_onboarding[chat_id]["business_name"] = text
        pending_onboarding[chat_id]["state"] = "awaiting_bank_name"
        send_text_with_cancel(chat_id, "🏢 Great! Now, please enter your Bank Name:")

    elif current_state == "awaiting_bank_name":
        pending_onboarding[chat_id]["bank_name"] = text
        # Instead of directly retrieving a bank code, call your bank selection flow.
        handle_bank_selection(text, chat_id, FLW_SECRET_KEY, test_mode=False)
        # Wait here for the user to select one of the inline buttons.
        return True

    elif current_state == "awaiting_account_number":
        if not text.isdigit() or len(text) < 10:
            send_text_with_cancel(chat_id, "❌ Invalid account number. Please enter a valid number:")
            return True
        pending_onboarding[chat_id]["account_number"] = text
        pending_onboarding[chat_id]["state"] = "awaiting_account_name"
        send_text_with_cancel(chat_id, "📝 Please enter your Account Name (as shown on your bank account):")

    elif current_state == "awaiting_account_name":
        pending_onboarding[chat_id]["account_name"] = text
        pending_onboarding[chat_id]["state"] = "awaiting_business_mobile"
        send_text_with_cancel(chat_id, "📱 Please provide your Business Mobile Number (digits only):")

    elif current_state == "awaiting_business_mobile":
        # Validate that the business mobile is digits and is at least 10 digits long.
        if not text.isdigit() or len(text) < 10:
            send_text_with_cancel(chat_id,
                                  "❌ Invalid business mobile number. Please enter a valid number (digits only):")
            return True
        pending_onboarding[chat_id]["business_mobile"] = text
        pending_onboarding[chat_id]["state"] = "awaiting_contact_details"
        send_text_with_cancel(chat_id, "📧 Finally, please provide your Contact Email:")

    elif current_state == "awaiting_contact_details":
        pending_onboarding[chat_id]["business_email"] = text
        seller_details = pending_onboarding[chat_id]

        # Check if there's already a seller document in Firestore.
        seller_doc_ref = db.collection('sellers').document(chat_id)
        seller_doc = seller_doc_ref.get()
        if seller_doc.exists:
            seller_data = seller_doc.to_dict()
            existing_subaccount = seller_data.get("subaccount_id", "")
            if existing_subaccount and verify_subaccount_id(existing_subaccount):
                send_message(chat_id, f"ℹ️ You are already onboarded with subaccount ID: {existing_subaccount}.")
                del pending_onboarding[chat_id]
                show_main_menu(chat_id)
                return True
            else:
                subaccount_id, error = create_seller_subaccount(seller_details)
                if subaccount_id:
                    seller_doc_ref.update({
                        "subaccount_id": subaccount_id,
                        "business_name": seller_details.get("business_name"),
                        "bank_code": seller_details.get("bank_code"),
                        "account_number": seller_details.get("account_number"),
                        "account_name": seller_details.get("account_name"),
                        "business_mobile": seller_details.get("business_mobile"),
                        "business_email": seller_details.get("business_email"),
                        "onboarded": True
                    })
                    send_message(chat_id,
                                 f"✅ Onboarding complete! Your new subaccount has been created with ID: {subaccount_id}.")
                else:
                    send_message(chat_id, f"❌ Onboarding error: {error}. Please try again.")
                del pending_onboarding[chat_id]
                show_main_menu(chat_id)
                return True
        else:
            subaccount_id, error = create_seller_subaccount(seller_details)
            if subaccount_id:
                db.collection('sellers').document(chat_id).set({
                    "subaccount_id": subaccount_id,
                    "business_name": seller_details.get("business_name"),
                    "bank_code": seller_details.get("bank_code"),
                    "account_number": seller_details.get("account_number"),
                    "account_name": seller_details.get("account_name"),
                    "business_mobile": seller_details.get("business_mobile"),
                    "business_email": seller_details.get("business_email"),
                    "onboarded": True
                }, merge=True)
                send_message(chat_id,
                             f"✅ Onboarding complete! Your account has been created with ID: {subaccount_id}.")
            else:
                send_message(chat_id, f"❌ Onboarding error: {error}. Please try again.")
            del pending_onboarding[chat_id]
            show_main_menu(chat_id)

    return True


def create_seller_subaccount(seller_details):
    url = "https://api.flutterwave.com/v3/subaccounts"

    # Optional: In production, first verify bank details (using your helper).
    if not test_mode:
        print("DEBUG: Verifying bank details before creating subaccount...")
        is_valid, error_msg = verify_bank_details(
            seller_details.get("bank_code"),
            seller_details.get("account_number"),
            seller_details.get("account_name"),
            FLW_SECRET_KEY,
            test_mode=False
        )
        if not is_valid:
            print(f"DEBUG: Bank details verification failed: {error_msg}")
            return None, f"Bank details verification failed: {error_msg}"

    # Choose payload based on mode.
    if test_mode:
        # Use hardcoded test values.
        payload = {
            "account_bank": "044",
            "account_number": "0690000037",
            "business_name": "Eternal Blue",
            "country": "NG",
            "split_value": seller_details.get("split_value", 0.05),
            "business_mobile": "090890382",
            "business_email": "petya@stux.net",
            "business_contact": "Richard Hendrix",
            "business_contact_mobile": "090890382",
            "split_type": "percentage"
        }
    else:
        payload = {
            "account_bank": seller_details.get("bank_code"),
            "account_number": seller_details.get("account_number"),
            "business_name": seller_details.get("business_name"),
            "country": "NG",
            "split_value": seller_details.get("split_value", 0.05),
            "business_mobile": seller_details.get("business_mobile"),
            "business_email": seller_details.get("business_email"),
            "split_type": "percentage"
        }

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    # Debug: Print the payload that we're using to create the subaccount.
    print("DEBUG: Creating subaccount with payload:", payload)

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        response_data = response.json()
        # Debug: Print the full response from Flutterwave.
        print("DEBUG: Flutterwave subaccount creation response:", response_data)

        if response_data.get("status") == "success":
            # Retrieve the actual Flutterwave subaccount ID.
            subaccount_id = response_data.get("data", {}).get("subaccount_id")
            print("DEBUG: Retrieved subaccount_id:", subaccount_id)
            return subaccount_id, None
        else:
            error_message = response_data.get("message", "Unknown error")
            print("DEBUG: Flutterwave subaccount creation failed with message:", error_message)
            return None, error_message
    except requests.exceptions.RequestException as e:
        print("DEBUG: Exception occurred while creating subaccount:", e)
        return None, f"Request failed: {str(e)}"


def verify_subaccount_id(seller_subaccount_id):
    """
    Verifies that the seller's subaccount exists on Flutterwave.
    Returns True if it exists (valid), False otherwise.
    """
    # Append the secret key using the query parameter name 'seckey'
    url = f"https://api.ravepay.co/v2/gpx/subaccounts/get/{seller_subaccount_id}?seckey={FLW_SECRET_KEY}"
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json"
    }

    print("DEBUG: Verifying subaccount with URL:", url)
    try:
        response = requests.get(url, headers=headers, timeout=30)
        print("DEBUG: HTTP status code:", response.status_code)
        print("DEBUG: Full verify response text:", response.text)
        data = response.json()
        print("DEBUG: Decoded verify response data:", data)

        if response.status_code == 200 and data.get("status") == "success":
            return True
        else:
            print("DEBUG: Subaccount verification failed:", data.get("message"))
    except Exception as e:
        print("DEBUG: Exception verifying subaccount ID:", e)

    return False


def get_product_by_id(chat_id, product_id_input):
    # Remove extra whitespace from the input.
    product_id_input = product_id_input.strip()

    # Query the 'products' collection using the 'product_id' field.
    query = db.collection('products').where('product_id', '==', product_id_input)
    docs = list(query.stream())

    if docs:
        doc = docs[0]
        product_data = doc.to_dict()
        description = product_data.get("description") or "No description provided."
        photo_file_id = product_data.get("photo_file_id")
        total_price = product_data.get("total_price", "N/A")

        # Retrieve other fields from product_data.
        prod_id = product_data.get("product_id", "Unknown ID")
        product_name = product_data.get("product_name", "No product name available")
        category = product_data.get("category", "No category specified")
        actual_price = product_data.get("actual_price", "Unknown")
        delivery_price = product_data.get("delivery_price", "Unknown")

        # Retrieve the seller's chat ID and sales count.
        seller_chat_id = product_data.get("chat_id")
        sales_count_text = ""
        if seller_chat_id:
            sales_count = get_sales_count(seller_chat_id)
            if sales_count and sales_count > 0:
                sales_count_text = f"\n⭐ Seller Sales: {sales_count}"

        # Build the product details message with emojis and (optionally) the seller's sales count.
        msg = (
            f"🆔 Product ID: `{prod_id}`\n"  # Product ID formatted as inline code
            f"📛 Product Name: {product_name}\n"
            f"📂 Category: {category}\n"
            f"🏷️ Actual Price: ₦{actual_price}\n"
            f"🚚 Delivery Price: ₦{delivery_price}\n"
            f"💵 Total Price: ₦{total_price}\n"
            f"📝 Description: {description}"
            f"{sales_count_text}\n"  # Append seller sales if available
            "ℹ️ Tip: For more info, please chat with the seller using the *Chat With Seller* button before payment."
        )

        # Precaution: Truncate the message if it exceeds Telegram's max message length.
        if len(msg) > MAX_MESSAGE_LENGTH:
            truncation_notice = "\n...[truncated]"
            msg = msg[:MAX_MESSAGE_LENGTH - len(truncation_notice)] + truncation_notice

        # Build the inline keyboard with a "Buy" button using the Firestore document ID.
        keyboard = {
            "inline_keyboard": [
                [{"text": f"🛒 Buy for ₦{total_price}", "callback_data": f"buy_{doc.id}"}]
            ]
        }

        # If there's a photo, send it with the inline keyboard.
        if photo_file_id:
            MAX_CAPTION_LENGTH = 1024
            caption_to_use = description if len(description) <= MAX_CAPTION_LENGTH else description[:MAX_CAPTION_LENGTH]
            send_photo(chat_id, photo_file_id, caption=caption_to_use, reply_markup=keyboard)
            # Send the product details message as a follow-up with Markdown formatting.
            send_message(chat_id, msg, parse_mode="Markdown")
        else:
            # If no photo is available, send a text message with the inline keyboard.
            send_message_with_keyboard(chat_id, msg, keyboard, parse_mode="Markdown")
    else:
        send_message(chat_id, "No approved product found with that Product ID. Please double-check and try again.")









def split_message(text, limit=MAX_MESSAGE_LENGTH):
    # Splits the text along newlines without breaking paragraphs if possible.
    lines = text.split('\n')
    chunks = []
    current_chunk = ""
    for line in lines:
        if len(current_chunk) + len(line) + 1 > limit:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = current_chunk + "\n" + line if current_chunk else line
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def show_policy_and_terms(chat_id):
    policy_text = (
        "# The Markit Bot – Privacy Policy and Terms of Service\n\n"
        "Last Updated: [01/06/2025]\n\n"
        "---\n\n"
        "## 1. Introduction\n\n"
        "Welcome to *The Markit Bot*, an automated service designed to connect sellers and buyers while facilitating secure escrow-based transactions using Flutterwave. By interacting with this Bot, you acknowledge and agree to abide by these Privacy Policy and Terms of Service (collectively, the “Terms”). This document governs your use of the Bot, which is provided via our Telegram channel, **The Markit**, operated by an individual. All users are encouraged to read this document carefully before using the service.\n\n"
        "---\n\n"
        "## 2. Definitions\n\n"
        "- *Bot:* Refers to The Markit Bot, the automated Telegram service facilitating trade and escrow functions.\n"
        "- *Developer/Operator:* The individuals operating through the Telegram channel named The Markit.\n"
        "- *User:* Any person (buyer or seller) accessing or interacting with The Markit Bot.\n"
        "- *Service:* All functionalities provided by The Markit Bot, including trade facilitation, escrow management, reporting, and post-payment chat.\n\n"
        "## 3. Contact Information\n\n"
        "The primary mode for contact and support is via The Markit Bot itself. Users are encouraged to use the Bot’s integrated contact functionality for any inquiries, support issues, or legal notices regarding the Bot.\n\n"
        "## 4. Data Collection & Privacy\n\n"
        "### 4.1 Information Collected\n\n"
        "When you interact with The Markit Bot, we may collect and process the following data:\n"
        "- *For Buyers:*\n"
        "  - Email address\n"
        "- *For Sellers:*\n"
        "  - Business name, email, and contact information\n"
        "- *General Data:*\n"
        "  - Timestamps for transactions and interactions\n"
        "  - Telegram user ID and username (as available through Telegram)\n"
        "- *Transaction Specifics:*\n"
        "  - Details related to monetary transactions that are processed through Flutterwave escrow functionality\n\n"
        "### 4.2 Use of Data\n\n"
        "The collected data is used to:\n"
        "- Facilitate safe and verified transactions between buyers and sellers  \n"
        "- Improve the functionality and user experience of The Markit Bot  \n"
        "- Enable post-payment communications between buyers and sellers  \n"
        "- Comply with legal and regulatory obligations as required\n\n"
        "### 4.3 Third-Party Data Handling\n\n"
        "- *Flutterwave:* All monetary transactions and escrow-related activities are managed using Flutterwave's escrow functionality.\n"
        "- *Firestore:* The Bot uses Firestore as its database to securely store non-sensitive transactional and user data.\n"
        "- *Deployment:* The service is hosted on fly.io.\n\n"
        "Your data is handled securely, and any sharing with third-party services (i.e., Flutterwave) is strictly for the purpose of processing transactions. Users are advised to review the privacy policies of these third-party providers for further details on how your data is managed.\n\n"
        "## 5. Escrow Functionality Specifics\n\n"
        "- *Transaction Process:*  \n"
        "  When a buyer initiates a payment, funds are held via Flutterwave’s escrow system. The Bot offers both “refund” and “release” options:\n"
        "  - *Refund:* Enabled in cases where the seller delivers a product or service that differs from what was proposed.\n"
        "  - *Release:* Allows the buyer to release the funds once the seller meets the agreed conditions.\n"
        "- *Responsibility and Verification:*  \n"
        "  The Markit Bot facilitates trust by confirming payment initiation; however, users are solely responsible for verifying transactions. The Bot’s responsibilities end once funds are either released or refunded via Flutterwave.\n"
        "- *Disclaimer:*  \n"
        "  The Bot’s function is limited to digital trade facilitation within Telegram. Any physical or off-platform transactions remain outside its responsibility.\n\n"
        "## Transaction Fees and Charges\n\n"
        "As of the effective date of these Terms (Last Updated: [01/06/2025]), please note the following fee structure:\n"
        "- Every refund transaction processed through Flutterwave may attract a deduction of 1.4% (capped) on the refunded amount.\n"
        "- Additionally, sellers incur a deduction of 5% from each sale, plus an extra 1.4% (capped).\n"
        "These charges/deductions are subject to change. However, before any transaction or product listing, the applicable fee details will always be shared with you.\n\n"
        "## 6. User Interaction & Acceptable Use\n\n"
        "- *Reporting Functionality:*  \n"
        "  Both buyers and sellers can report problematic counterparts through the Bot. When a report is filed, the Bot will notify the involved party about the report before or during any subsequent transaction.\n"
        "- *User Obligations:*  \n"
        "  Users must use The Markit Bot solely for legitimate trade and transaction activities and refrain from any form of fraudulent or abusive behavior. Abuse or repeated misuse of the system may result in termination of access or further actions as deemed necessary.\n"
        "- *Transparency and Trust:*  \n"
        "  The Bot is designed to help both parties make informed decisions by flagging reported users, thereby promoting transparency and trust throughout every transaction.\n\n"
        "## 7. Third-Party Integrations\n\n"
        "The Markit Bot integrates with the following third-party services:\n"
        "- *Flutterwave:* Used exclusively for handling all monetary transactions and escrow functions.\n"
        "- *Firestore:* Serves as the primary database for recording transaction details and user data.\n"
        "- *Fly.io:* The platform used for launching and hosting the Bot.\n\n"
        "By using The Markit Bot, you acknowledge that your transactions are additionally governed by the terms and conditions of these third-party services. It is recommended that you review their policies for further information.\n\n"
        "## 8. Disclaimers & Limitation of Liability\n\n"
        "- *As-Is Provision:*  \n"
        "  The Markit Bot is provided “as-is” without any express or implied warranty regarding continuous, error-free operation.\n"
        "- *Limited Responsibility:*  \n"
        "  Our responsibility is strictly limited to the functions performed within the Telegram environment. The Markit Bot and its operator are not liable for any occurrences arising from physical transactions or issues outside the digital platform.\n"
        "- *User Verification:*  \n"
        "  Users are solely responsible for the verification of transactions. Once funds are released or refunded via Flutterwave, the Bot’s responsibility is considered to have ended.\n"
        "- *Indemnification:*  \n"
        "  By using the Bot, you agree to indemnify and hold harmless the operator from any claims, damages, or losses arising from your use or misuse of the Bot.\n\n"
        "## 9. Modifications to the Terms\n\n"
        "We reserve the right to modify or update these Terms at any time. Any changes will be posted via the Bot and will become effective immediately upon posting. Your continued use of The Markit Bot after such changes constitutes your acceptance of the revised Terms.\n\n"
        "## 10. Termination\n\n"
        "We reserve the right to suspend or terminate your access to The Markit Bot at our sole discretion if you are found to be in violation of these Terms or if such actions are deemed necessary to protect the service or other users.\n\n"
        "## 11. Governing Law and Jurisdiction\n\n"
        "These Terms are governed by and construed in accordance with the laws of Nigeria. Any disputes related to The Markit Bot shall be subject to the exclusive jurisdiction of the courts located in the applicable jurisdiction in Nigeria.\n\n"
        "## 12. Contact and Support\n\n"
        "For any queries, support issues, or further information regarding these Terms, please use the contact functionality provided within The Markit Bot."
    )

    # Split the policy text into chunks to avoid exceeding Telegram's character limit.
    chunks = split_message(policy_text, limit=MAX_MESSAGE_LENGTH)

    # Send each chunk sequentially.
    for index, chunk in enumerate(chunks):
        # For the final chunk, attach the inline keyboard with three buttons.
        if index == len(chunks) - 1:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "I Agree", "callback_data": "agree_terms"}],
                    [{"text": "I Disagree", "callback_data": "disagree_terms"}],
                    [{"text": "Contact & Support", "callback_data": "contact_support"}]
                ]
            }
            send_message_with_keyboard(chat_id, chunk, keyboard)
        else:
            send_message(chat_id, chunk)


def update_user_terms_agreement(sender_id, accepted):
    db.collection('sellers').document(sender_id).set(
        {"terms_accepted": accepted},
        merge=True
    )

def update_buyer_terms_agreement(sender_id, accepted):
    db.collection('buyers').document(sender_id).set(
        {"terms_accepted": accepted},
        merge=True
    )


def is_user_blocked(user_id):
    """
    Checks whether a given user (by their Telegram unique user ID) is currently blocked.
    Returns a tuple (True, unblock_timestamp) if blocked,
    or (False, None) if not blocked.
    Cleans up expired blocks from the in-memory dictionary as well as Firestore.
    """
    if user_id in user_blocks:
        unblock_timestamp = user_blocks[user_id]
        now = int(time.time())
        if now < unblock_timestamp:
            return True, unblock_timestamp
        else:
            # Expired block – clean up both in-memory and Firestore.
            del user_blocks[user_id]
            try:
                db.collection("user_blocks").document(str(user_id)).delete()
            except Exception as e:
                print(f"Error deleting block record for user {user_id}: {e}")
    return False, None





def send_seller_notification(sale_id, product_id):
    """
    Retrieves the sale record and sends the seller a notification message with
    "Start Chat with Buyer" and "Report Buyer" buttons.
    """
    sale_doc_ref = db.collection('products').document(product_id).collection('sales').document(sale_id)
    sale_data = sale_doc_ref.get().to_dict()
    if not sale_data:
        print(f"DEBUG: Sale record {sale_id} not found for product {product_id}.")
        return

    seller_chat_id = sale_data.get("seller_chat_id")
    buyer_chat_id = sale_data.get("buyer_chat_id")
    session_id = sale_data.get("session_id")
    if not (seller_chat_id and session_id):
        print(f"DEBUG: Missing seller_chat_id or session_id in sale record {sale_id}.")
        return

    # Retrieve buyer_username from the sale record; fall back to buyer_chat_id if not present.
    buyer_username = sale_data.get("buyer_username") or buyer_chat_id
    buyer_report_count = user_reports.get(buyer_username, 0)
    buyer_report_info = f"⚠️ Note: This buyer has been reported {buyer_report_count} time(s).\n" if buyer_report_count > 0 else ""

    seller_prompt = (
        "🔔 *Notification:* The buyer's payment has been confirmed "
        "and funds are securely held by the bot. You can now make delivery and ensure the buyer clicks on the release"
        " funds button for your money to be received and forwarded to your account 💰.\n"
        f"{buyer_report_info}"
        "💬 Click below to start chatting with the buyer or 🚩 report the buyer if necessary."
    )

    seller_keyboard = {
        "inline_keyboard": [
            [
                {"text": "💬 Start Chat with Buyer", "callback_data": f"start_chat_seller:{session_id}"},
                {"text": "🚩 Report Buyer", "callback_data": f"report_user_{buyer_username}"}
            ]
        ]
    }
    send_message_with_keyboard_retry(seller_chat_id, seller_prompt, seller_keyboard)

    # Mark the seller as notified.
    sale_doc_ref.update({"seller_notified": True})
    print(f"DEBUG: Seller {seller_chat_id} notified for sale {sale_id}.")

    

def load_user_reports():
    try:
        reports = db.collection('user_reports').stream()
        for report_doc in reports:
            data = report_doc.to_dict()
            user_reports[report_doc.id] = data.get('report_count', 0)
        print("DEBUG: Loaded user reports from Firestore.")
    except Exception as e:
        print(f"DEBUG: Error loading user reports: {e}")


def load_user_recommendations():
    try:
        recommendations = db.collection('user_recommendations').stream()
        for rec_doc in recommendations:
            data = rec_doc.to_dict()
            user_recommendations[rec_doc.id] = data.get('recommendation_count', 0)
        print("DEBUG: Loaded user recommendations from Firestore.")
    except Exception as e:
        print(f"DEBUG: Error loading user recommendations: {e}")

# Call these functions on startup, before handling any incoming events.
load_user_reports()
load_user_recommendations()


def get_all_products():
    """
    Retrieve all products from the 'products' collection and include
    their Firestore document ID as 'id' in the returned list.
    """
    products_ref = db.collection('products')
    docs = products_ref.stream()
    products = []
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id
        products.append(data)
    return products



def post_single_product():
    all_products = get_all_products()

    if not all_products:
        print("No products available. Waiting for products to be added...")
        return

    # Pick one product randomly.
    product = random.choice(all_products)

    # Extract product details.
    prod_id        = product.get("product_id", "N/A")
    product_name   = product.get("product_name", "No product name provided.")
    category       = product.get("category", "N/A")
    price_range    = product.get("price_range", "N/A")
    actual_price   = product.get("actual_price", "N/A")
    delivery_price = product.get("delivery_price", "N/A")
    total_price    = product.get("total_price", "N/A")
    description    = product.get("description", "No description provided.")

    # Retrieve the seller's chat ID and sales count.
    seller_chat_id = product.get("chat_id")
    sales_count_text = ""
    if seller_chat_id:
        sales_count = get_sales_count(seller_chat_id)
        if sales_count and sales_count > 0:
            sales_count_text = f"\n\n⭐ Seller Sales: {sales_count}"

    # Define hashtags for product posts.
    hashtags = "\n\n#Marketplace #NewArrival #DealAlert #TheMarkit"

    # Build a combined caption message using Markdown, including hashtags.
    full_caption = (
        "🔔 Marketplace Update:\n"
        "🌟 New Product Spotlight! 🌟\n"
        f"🆔 Product ID: {prod_id}\n"
        f"📛 Product Name: {product_name}\n"
        f"📂 Category: {category}\n"
        f"🏷️ Actual Price: ₦{actual_price}\n"
        f"🚚 Delivery Price: ₦{delivery_price}\n"
        f"💵 Total Price: ₦{total_price}\n"
        f"📝 Description: {description}"
        f"{sales_count_text}\n"  # Append sales count if available.
        "ℹ️ Tip: For more info, please chat with the seller using the *Chat With Seller* button before payment."
        + hashtags
    )

    # Precaution: Truncate full_caption if it exceeds Telegram's overall message limit.
    if len(full_caption) > MAX_MESSAGE_LENGTH:
        truncation_notice = "\n...[truncated]"
        full_caption = full_caption[:MAX_MESSAGE_LENGTH - len(truncation_notice)] + truncation_notice

    # Build the inline keyboard with a "Buy" button.
    keyboard = {
        "inline_keyboard": [
            [{"text": f"Buy for ₦{total_price}", "callback_data": f"buy_{product['id']}"}]
        ]
    }

    # Send the update as a single message.
    if product.get("photo_file_id"):
        MAX_CAPTION_LENGTH = 1024
        # For photo captions, ensure they do not exceed the 1024-character limit.
        if len(full_caption) > MAX_CAPTION_LENGTH:
            truncation_notice = "\n...[truncated]"
            caption_to_use = full_caption[:MAX_CAPTION_LENGTH - len(truncation_notice)] + truncation_notice
        else:
            caption_to_use = full_caption
        send_photo(CHANNEL_ID, product.get("photo_file_id"),
                   caption=caption_to_use, reply_markup=keyboard, parse_mode="Markdown")
    else:
        send_message_with_keyboard(CHANNEL_ID, full_caption, keyboard, parse_mode="Markdown")






def get_product_by_docid(chat_id, product_doc_id):
    """
    Retrieve a product using its Firestore document ID and send its details with its photo
    as a single message if available.
    """
    doc_ref = db.collection('products').document(product_doc_id)
    doc = doc_ref.get()
    if doc.exists:
        product_data = doc.to_dict()
        description = product_data.get("description") or "No description provided."
        total_price = product_data.get("total_price", "N/A")
        photo_file_id = product_data.get("photo_file_id")

        msg = (
        f"🆔 Product ID: `{prod_id}`\n"  # Product ID formatted as inline code
        f"📛 Product Name: {product_name}\n"
        f"📂 Category: {category}\n"
        f"🏷️ Actual Price: ₦{actual_price}\n"
        f"🚚 Delivery Price: ₦{delivery_price}\n"
        f"💵 Total Price: ₦{total_price}\n"
        f"📝 Description: {description}\n"
        "ℹ️ Tip: For more info, please chat with the seller using the *Chat With Seller* button before payment."
        )

        keyboard = {
            "inline_keyboard": [
                [{"text": f"Buy for ₦{total_price}", "callback_data": f"buy_{product_doc_id}"}]
            ]
        }

        if photo_file_id:
            MAX_CAPTION_LENGTH = 1024
            caption_to_use = msg if len(msg) <= MAX_CAPTION_LENGTH else msg[:MAX_CAPTION_LENGTH]

            payload = {
                "chat_id": chat_id,
                "photo": photo_file_id,
                "caption": caption_to_use,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }
            try:
                requests.post(f"{TELEGRAM_API_URL}/sendPhoto", json=payload, timeout=TIMEOUT)
            except Exception as e:
                print("Error sending product photo:", e)
        else:
            send_message_with_keyboard(chat_id, msg, keyboard, parse_mode="Markdown")
    else:
        send_message(chat_id, "No approved product found with that Product ID. Please double-check and try again.")


def has_accepted_policy(user_id):
    """
    Check if the user (buyer or seller) has accepted the policy.
    Returns True if there’s a record in either collection with terms_accepted True.
    """
    user_id = str(user_id)  # standardize user ID as a string

    # Check seller collection
    seller_ref = db.collection('sellers').document(user_id)
    seller_doc = seller_ref.get()
    if seller_doc.exists:
        seller_data = seller_doc.to_dict()
        if seller_data.get("terms_accepted", False):
            return True

    # Check buyer collection
    buyer_ref = db.collection('buyers').document(user_id)
    buyer_doc = buyer_ref.get()
    if buyer_doc.exists:
        buyer_data = buyer_doc.to_dict()
        if buyer_data.get("terms_accepted", False):
            return True

    return False


def fetch_random_global_business_news(page_size=20):
    """
    Fetch global business news articles using NewsAPI's 'everything' endpoint,
    then return one randomly selected article.

    Args:
        page_size (int): The number of articles to retrieve (default is 20).

    Returns:
        dict or None: A randomly selected article dictionary if available, otherwise None.
    """
    params = {
        "q": "business OR market OR trade OR economy",
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": page_size,
        "apiKey": NEWS_API_KEY
    }

    response = requests.get(NEWS_API_URL, params=params)
    if response.status_code == 200:
        data = response.json()
        articles = data.get("articles", [])
        if articles:
            return random.choice(articles)
        else:
            print("No business news articles found.")
            return None
    else:
        print(f"Error fetching news: {response.status_code} - {response.text}")
        return None


def start_background_loop(loop):
    """Set and run the event loop in a dedicated thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Create a dedicated event loop
async_loop = asyncio.new_event_loop()
# Start it in a separate daemon thread
bg_thread = threading.Thread(target=start_background_loop, args=(async_loop,), daemon=True)
bg_thread.start()


def post_global_business_news():
    """
    Retrieves a random business news article and posts it to the Telegram channel.
    """
    async def inner():
        article = fetch_random_global_business_news(page_size=20)
        if article:
            title = article.get("title", "No title available")
            url = article.get("url", "")
            # Define your hashtags for business news.
            hashtags = "#BusinessNews #MarketUpdate #GlobalEconomy #TheMarkit"
            # Format the message using Markdown for a clickable title and appended hashtags.
            news_message = (
                f"📰 *Global Business News Update:*\n\n"
                f"• [{title}]({url})\n\n"
                f"{hashtags}"
            )
            try:
                # Await sending the message asynchronously.
                await bot.send_message(chat_id=CHANNEL_ID, text=news_message, parse_mode="Markdown")
                print("Business news posted successfully!")
            except Exception as e:
                print(f"Error posting business news: {e}")
        else:
            print("No article available to post.")

    # Schedule the coroutine on the always-running background loop.
    future = asyncio.run_coroutine_threadsafe(inner(), async_loop)
    try:
        # Optionally wait for the coroutine to finish.
        future.result()
    except Exception as e:
        print("Exception running coroutine in background loop:", e)







if __name__ == "__main__":
    set_webhook()
    # send_start_button(CHANNEL_ID)  # Optionally send the start button to the channel once

    # Existing background tasks for other functionalities.
    threading.Thread(target=delete_old_products, daemon=True).start()
    threading.Thread(target=reset_counters_at_midnight, daemon=True).start()

    # Set up and start the background scheduler.
    scheduler = BackgroundScheduler()

    # Job for posting products every 20 minutes.
    scheduler.add_job(
        post_single_product,  # Your existing product posting function.
        'interval',
        minutes=20,
        misfire_grace_time=3600,  # 1 hour misfire grace time.
        coalesce=True,
        id="product_post_job"
    )

    # Job for posting global business news every 70 minutes.
    scheduler.add_job(
        post_global_business_news,  # Function for fetching and posting business news.
        'interval',
        minutes=70,
        misfire_grace_time=3600,
        coalesce=True,
        id="news_post_job"
    )

    scheduler.start()
    print("Scheduler started. Posting a product every 20 minutes and business news every 70 minutes...")

    # Get the port from the system's environment variables; default to 8080 for local development.
    port = int(os.environ.get("PORT", 8080))

    # Run the Flask app on all available interfaces.
    app.run(host="0.0.0.0", port=port)





