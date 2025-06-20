import hmac
import hashlib
import json

# Your secret
secret = "Kendrickiii@911"

# Your payload (replace this with the JSON payload you are sending)
payload = {
    "event": "charge.completed",
    "data": {
        "status": "successful",
        "tx_ref": "6334679159_UQbOJkK5WCF0bvRBt9Vh_1742813215",
        "amount": 1000,
        "customer": {
            "email": "test@example.com"
        },
        "meta": [
            {
                "metaname": "rave_escrow_tx",
                "metavalue": "1"
            }
        ]
    }
}

# Convert the payload to JSON bytes
payload_bytes = json.dumps(payload).encode('utf-8')

# Compute the HMAC SHA-256 signature
signature = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

print("Computed Signature:", signature)
