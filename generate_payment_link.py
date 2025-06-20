import requests
import json
import time
import uuid

# Use your live secret key:
FLW_SECRET_KEY = 'FLWSECK-a0f963583f37d331615e19b6f9dd2200-196cec4827bvt-X'
FLW_PUBLIC_KEY = 'FLWPUBK-e165ceed81c5840ed090c7da72db6e56-X'

# Flutterwave payment initialization endpoint
url = "https://api.flutterwave.com/v3/payments"
headers = {
    "Authorization": f"Bearer {FLW_SECRET_KEY}",
    "Content-Type": "application/json"
}

# Generate a unique transaction reference
reference = f"live_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"

# Build the payload
payload = {
    "tx_ref": reference,
    "amount": "5000",  # Adjust the amount as required
    "currency": "NGN",
    "redirect_url": "https://example.com/payment-complete",  # Replace with your live redirect URL
    "payment_options": "card",
    "customer": {
        "email": "liveuser@example.com",
        "phonenumber": "08012345678",
        "name": "Live User"
    },
    "meta": {
        "rave_escrow_tx": 1  # Indicates an escrow transaction
    },
    "customizations": {
        "title": "Live Payment",
        "description": "Payment for live testing purposes",
        "logo": "https://example.com/live-logo.png"  # Replace with your logo URL if needed
    }
}

print("DEBUG: Payment API request payload:")
print(json.dumps(payload, indent=2))

print("DEBUG: Sending request to Flutterwave (Live Mode)...")
response = requests.post(url, json=payload, headers=headers)
response_data = response.json()

print("DEBUG: Payment API response from Flutterwave:")
print(json.dumps(response_data, indent=2))

# Extract and print the payment link if available
if response_data.get("status") == "success" and response_data.get("data", {}).get("link"):
    payment_link = response_data["data"]["link"]
    print("\nPayment link generated successfully:")
    print(payment_link)
else:
    print("\nFailed to generate payment link. Response:")
    print(response_data)

payload = {
        "tx_ref": reference,
        "amount": amount,
        "currency": "NGN",
        "redirect_url": redirect_url,
        "payment_options": "card",
        "customer": {
            "email": buyer_email,
            "phonenumber": product_data.get('phone_number', ''),
            "name": product_data.get('buyer_name', 'Valued Customer')
        },
        "meta": {
            "rave_escrow_tx": 1,
            "product_id": product_id,
            "buyer_chat_id": buyer_chat_id
        },
        "customizations": {
            "title": "Escrow Payment",
            "description": f"💸 Payment for product {product_id}",
            "logo": logo_url
        },