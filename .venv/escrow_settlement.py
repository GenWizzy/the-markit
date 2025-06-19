import requests
import json

# Your Flutterwave secret key.
FLW_SECRET_KEY = 'FLWSECK-a0f963583f37d331615e19b6f9dd2200-196cec4827bvt-X'


def verify_transaction(tx_ref):
    """
    Verifies a Flutterwave transaction by its reference.

    Args:
        tx_ref (str): The transaction reference (tx_ref).

    Returns:
        dict: The verification response JSON.
    """
    url = f"https://api.ravepay.co/v3/transactions/verify_by_reference?tx_ref={tx_ref}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {FLW_SECRET_KEY}"
    }

    print(f"DEBUG: Verifying transaction with tx_ref: {tx_ref}")
    response = requests.get(url, headers=headers)
    print(f"DEBUG: Verification HTTP Status Code: {response.status_code}")
    print(f"DEBUG: Verification Raw Response: {response.text}")

    if response.status_code == 200:
        return response.json()
    else:
        return {"status": "error",
                "message": f"Verification failed with status {response.status_code}: {response.text}"}


def release_funds(transaction_id):
    """
    Releases funds from escrow on Flutterwave using the verified transaction id.

    Args:
        transaction_id (int or str): The verified transaction id.

    Returns:
        dict: The JSON response from Flutterwave.
    """
    try:
        url = "https://api.ravepay.co/v2/gpx/transactions/escrow/settle"
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json"
        }
        payload = {
            "id": transaction_id,
            "secret_key": FLW_SECRET_KEY
        }
        print(f"DEBUG: Sending POST to {url} for transaction id: {transaction_id}")
        print(f"DEBUG: Payload: {payload}")

        response = requests.post(url, json=payload, headers=headers)
        print(f"DEBUG: Settlement HTTP Status Code: {response.status_code}")
        print(f"DEBUG: Settlement Raw Response: {response.text}")

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


if __name__ == '__main__':
    # The transaction reference for the new transaction you wish to settle:
    tx_ref = "6334679159_BuPny-YZRY_1748359965017_214a2704"

    # First, verify the transaction to get its verified transaction id.
    verification_response = verify_transaction(tx_ref)

    if verification_response.get("status") == "success":
        transaction_data = verification_response.get("data")
        if transaction_data:
            verified_transaction_id = transaction_data.get("id")
            print(f"Verified Transaction ID: {verified_transaction_id}")

            # Now, release the funds using the verified transaction id.
            settlement_response = release_funds(verified_transaction_id)
            print("Settlement Response:")
            print(json.dumps(settlement_response, indent=2))
        else:
            print("ERROR: No transaction data found in verification response.")
    else:
        print("ERROR: Transaction verification failed.")
