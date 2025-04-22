import os
import requests
import json
import hmac
import hashlib
from datetime import datetime, timedelta, timezone, date # Added date
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Make sure FLASK_SECRET_KEY is set in your .env file
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "a_default_secret_key_for_development")

# --- Supabase Configuration ---
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")
if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env file")
supabase: Client = create_client(supabase_url, supabase_key)

# --- Nebius/OpenAI Client Configuration ---
NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL")
NEBIUS_API_KEY = os.environ.get("NEBIUS_API_KEY")
NEBIUS_MODEL = os.environ.get("NEBIUS_MODEL")
nebius_client = None
if NEBIUS_BASE_URL and NEBIUS_API_KEY and NEBIUS_MODEL:
    try:
        nebius_client = OpenAI(
            base_url=NEBIUS_BASE_URL,
            api_key=NEBIUS_API_KEY
        )
    except Exception as e:
        print(f"Error initializing Nebius client: {e}")
else:
    print("Warning: Nebius client configuration missing in .env file.")

# --- Lemon Squeezy Configuration ---
LEMONSQUEEZY_API_KEY = os.environ.get("LEMONSQUEEZY_API_KEY")
LEMONSQUEEZY_STORE_ID = os.environ.get("LEMONSQUEEZY_STORE_ID")
# Variant IDs MUST be correctly set in .env for the webhook to identify plans
LEMONSQUEEZY_STANDARD_VARIANT_ID = os.environ.get("LEMONSQUEEZY_STANDARD_VARIANT_ID")
LEMONSQUEEZY_PRO_VARIANT_ID = os.environ.get("LEMONSQUEEZY_PRO_VARIANT_ID")
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET")
LEMONSQUEEZY_API_URL = "https://api.lemonsqueezy.com/v1"
# --- THIS IS THE SINGLE CHECKOUT LINK BASE URL ---
# --- Replace YOUR_SINGLE_CHECKOUT_LINK_HERE with your actual link ---
LEMONSQUEEZY_CHECKOUT_LINK_BASE = os.environ.get("LEMONSQUEEZY_CHECKOUT_LINK", "YOUR_SINGLE_CHECKOUT_LINK_HERE")

if not all([LEMONSQUEEZY_API_KEY, LEMONSQUEEZY_STORE_ID, LEMONSQUEEZY_STANDARD_VARIANT_ID, LEMONSQUEEZY_PRO_VARIANT_ID, LEMONSQUEEZY_WEBHOOK_SECRET]):
    print("Warning: One or more Lemon Squeezy configuration variables missing in .env file.")

# --- Constants ---
FREE_PLAN_HOURLY_LIMIT = 2
STANDARD_PLAN_MONTHLY_LIMIT = 100
# Pro plan is effectively unlimited, checked by the is_pro_plan flag

# --- Helper Function: Create Lemon Squeezy Customer ---
def create_lemon_squeezy_customer(email, name):
    """Creates a customer in Lemon Squeezy."""
    if not LEMONSQUEEZY_API_KEY or not LEMONSQUEEZY_STORE_ID:
        print("Lemon Squeezy API Key or Store ID not configured.")
        return None, "Lemon Squeezy integration not configured."

    customer_url = f"{LEMONSQUEEZY_API_URL}/customers"
    headers = {
        'Accept': 'application/vnd.api+json',
        'Content-Type': 'application/vnd.api+json',
        'Authorization': f'Bearer {LEMONSQUEEZY_API_KEY}'
    }
    payload = {
        "data": {
            "type": "customers",
            "attributes": {
                "name": name,
                "email": email,
            },
            "relationships": {
                "store": {
                    "data": {
                        "type": "stores",
                        "id": str(LEMONSQUEEZY_STORE_ID)
                    }
                }
            }
        }
    }
    try:
        response = requests.post(customer_url, headers=headers, json=payload)
        response.raise_for_status()
        customer_data = response.json()
        customer_id = customer_data.get("data", {}).get("id")
        if customer_id:
            return customer_id, None
        else:
            print(f"Lemon Squeezy response missing customer ID: {customer_data}")
            return None, "Could not extract customer ID from Lemon Squeezy response."

    except requests.exceptions.RequestException as e:
        print(f"Error creating Lemon Squeezy customer: {e}")
        error_details = f"API Error: {e}"
        try:
            error_response = e.response.json()
            if 'errors' in error_response:
                error_details = f"API Error: {error_response['errors'][0]['detail']}"
        except:
            pass # Ignore if response body isn't JSON or doesn't have expected structure
        return None, f"Failed to connect to Lemon Squeezy. {error_details}"
    except Exception as e:
        print(f"Unexpected error during Lemon Squeezy customer creation: {e}")
        return None, f"An unexpected error occurred during signup ({type(e).__name__})."

# --- Routes ---

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Handles user signup."""
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        if not email or not password:
            flash('Email and password are required.', 'error')
            return redirect(url_for('signup'))

        # Basic name generation from email for Lemon Squeezy
        name = email.split('@')[0].replace('.', '').replace('+', '') # Simple name cleaning

        # 1. Check if user already exists in Supabase
        try:
            res = supabase.table('users').select('id', count='exact').eq('email', email).execute()
            if (hasattr(res, 'count') and res.count > 0) or (not hasattr(res, 'count') and res.data):
                flash('Email address already registered.', 'error')
                return redirect(url_for('signup'))
        except Exception as e:
            flash(f'Database error checking user: {e}', 'error')
            return redirect(url_for('signup'))

        # 2. Try to create Lemon Squeezy Customer FIRST
        ls_customer_id, ls_error = create_lemon_squeezy_customer(email, name)

        if ls_error:
            flash(f'Signup failed: {ls_error}', 'error')
            return redirect(url_for('signup'))

        if not ls_customer_id:
            flash('Signup failed: Could not create payment profile.', 'error')
            return redirect(url_for('signup'))

        # 3. If Lemon Squeezy creation succeeded, create user in Supabase
        password_hash = generate_password_hash(password)
        try:
            user_data = {
                'email': email,
                'password_hash': password_hash,
                'lemonsqueezy_customer_id': ls_customer_id,
                'is_free_plan': True,
                'is_standard_plan': False,
                'is_pro_plan': False,
                'message_count': 0,          # Overall lifetime messages
                'messages_this_hour': 0,     # For free plan rate limiting
                'last_message_timestamp': None,# For free plan rate limiting
                'messages_this_month': 0,    # For standard plan monthly limit
                'usage_reset_date': None     # For standard/pro plan reset
            }
            res = supabase.table('users').insert(user_data).execute()

            if res.data:
                flash('Account created successfully! Please login.', 'success')
                return redirect(url_for('login'))
            else:
                print("Supabase insert failed, response:", res)
                # TODO: Consider deleting the LS customer here if Supabase fails?
                flash('Failed to create account in our database after payment profile creation. Please contact support.', 'error')
                return redirect(url_for('signup'))

        except Exception as e:
            flash(f'Database error creating user: {e}', 'error')
            print(f"Error inserting user: {e}")
            # TODO: Consider deleting the LS customer here too.
            return redirect(url_for('signup'))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles user login."""
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        if not email or not password:
            flash('Email and password are required.', 'error')
            return redirect(url_for('login'))

        try:
            # Fetch user data including the password hash and ID
            res = supabase.table('users').select('id, email, password_hash').eq('email', email).single().execute()
            user = res.data # single() throws error if not found

            # Check password hash
            if check_password_hash(user['password_hash'], password):
                session['user_email'] = user['email'] # Store email in session
                session['user_id'] = user['id']       # Store user id in session
                flash('Login successful!', 'success')
                return redirect(url_for('home'))
            else:
                flash('Invalid email or password.', 'error')
                return redirect(url_for('login'))

        except Exception as e:
            # Handle case where user not found or other issues
            print(f"Login error: {e}") # Log for debugging
            flash('Invalid email or password.', 'error') # Generic message for security
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    """Handles user logout."""
    session.pop('user_email', None) # Remove email from session
    session.pop('user_id', None)    # Remove user id from session
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/', methods=['GET', 'POST'])
def home():
    """Displays the main chatbot page and handles message sending."""
    if 'user_id' not in session: # Check for user_id
        flash('Please login to access the chatbot.', 'error')
        return redirect(url_for('login'))

    user_id = session['user_id']
    bot_response = None
    user_message = None
    message_limit_reached = False
    plan_name = "Free" # Default
    current_limit_info = f"{FREE_PLAN_HOURLY_LIMIT}/hour limit" # Default display

    # --- Fetch User Data ---
    try:
        # Select all relevant columns
        res = supabase.table('users').select(
            'email, message_count, is_free_plan, is_standard_plan, is_pro_plan, '
            'messages_this_hour, last_message_timestamp, messages_this_month, usage_reset_date'
        ).eq('id', user_id).single().execute()
        user_data = res.data
    except Exception as e:
        flash(f'Error fetching user data: {e}', 'error')
        print(f"Error fetching user data for {user_id}: {e}")
        return redirect(url_for('logout')) # Log out on error

    # --- Assign local variables from fetched data ---
    user_email = user_data['email']
    message_count = user_data['message_count']
    is_free_plan = user_data['is_free_plan']
    is_standard_plan = user_data['is_standard_plan']
    is_pro_plan = user_data['is_pro_plan']
    messages_this_hour = user_data['messages_this_hour']
    last_message_timestamp_str = user_data['last_message_timestamp']
    messages_this_month = user_data['messages_this_month']
    usage_reset_date_str = user_data['usage_reset_date'] # YYYY-MM-DD string or None

    # Convert timestamps/dates
    last_message_timestamp = datetime.fromisoformat(last_message_timestamp_str) if last_message_timestamp_str else None
    usage_reset_date = date.fromisoformat(usage_reset_date_str) if usage_reset_date_str else None

    # --- Monthly Usage Reset Logic ---
    db_needs_update = False
    reset_update_payload = {}
    today = date.today()

    # Check if user is on a paid plan AND reset date exists AND today is on or after reset date
    if (is_standard_plan or is_pro_plan) and usage_reset_date and today >= usage_reset_date:
        print(f"Resetting monthly usage for user {user_id}. Today ({today}) >= Reset Date ({usage_reset_date})")
        messages_this_month = 0 # Reset local counter first
        # Calculate next reset date (approx 30 days, ideally use renews_at from LS webhook)
        # For simplicity, we'll rely on the webhook to set the *next* accurate date.
        # Here, we just ensure the reset happens. We could recalculate ~30 days, but
        # the webhook logic setting it based on 'renews_at' is more robust.
        # Let's just clear the local counter and prepare DB update.
        # The *next* reset date will be set correctly by the next successful webhook event.

        reset_update_payload['messages_this_month'] = 0
        # Optionally, if webhook might be unreliable, calculate a fallback next date:
        # next_reset_date = today + timedelta(days=30) # Example fallback
        # reset_update_payload['usage_reset_date'] = next_reset_date.isoformat()
        db_needs_update = True # Mark that DB needs update

    # --- Update DB if reset occurred ---
    if db_needs_update:
        try:
            # Update only the reset fields
            supabase.table('users').update(reset_update_payload).eq('id', user_id).execute()
            print(f"Successfully reset monthly usage count for user {user_id}")
        except Exception as e:
            print(f"ERROR: Failed to update monthly reset info for user {user_id}: {e}")
            # Non-critical for immediate user experience, but should be logged.
            flash("Could not update usage reset information in database.", "warning")


    # --- Determine Plan Name & Limits for Display & Logic ---
    if is_pro_plan:
        plan_name = "Pro"
        current_limit_info = "Unlimited messages"
    elif is_standard_plan:
        plan_name = "Standard"
        current_limit_info = f"{messages_this_month}/{STANDARD_PLAN_MONTHLY_LIMIT} messages this month"
    else: # Free plan
        plan_name = "Free"
        current_limit_info = f"{FREE_PLAN_HOURLY_LIMIT}/hour message limit"


    # --- Handle POST Request (Sending Message) ---
    if request.method == 'POST':
        user_message = request.form.get('user_input')

        if user_message and nebius_client and NEBIUS_MODEL:
            allow_message = True
            now_dt = datetime.now(timezone.utc) # Use timezone-aware datetime for comparisons
            usage_update_payload = {} # Store fields to update in Supabase this request

            # --- Check Limits Based on Plan ---
            if is_pro_plan:
                # Pro plan has no message limit check
                pass # Allow message
            elif is_standard_plan:
                # Check monthly limit (use the potentially reset 'messages_this_month')
                if messages_this_month >= STANDARD_PLAN_MONTHLY_LIMIT:
                    allow_message = False
                    message_limit_reached = True
                    flash(f'Standard plan limit reached ({STANDARD_PLAN_MONTHLY_LIMIT} messages this month).', 'warning')
            elif is_free_plan:
                # Check hourly limit
                if last_message_timestamp and (now_dt - last_message_timestamp < timedelta(hours=1)):
                    # Within the last hour
                    if messages_this_hour >= FREE_PLAN_HOURLY_LIMIT:
                        allow_message = False
                        message_limit_reached = True
                        flash(f'Free plan limit reached ({FREE_PLAN_HOURLY_LIMIT} messages this hour). Please wait.', 'warning')
                    else:
                        # Limit not reached yet this hour, increment local counter
                        messages_this_hour += 1
                else:
                    # First message in a new hour window (or first message ever)
                    messages_this_hour = 1
                # Update last message timestamp (will be saved if message allowed)
                last_message_timestamp = now_dt


            # --- Process Message if Allowed ---
            if allow_message:
                # Prepare database update payload BEFORE making the API call
                message_count += 1 # Increment lifetime count
                usage_update_payload['message_count'] = message_count

                if is_standard_plan:
                    messages_this_month += 1 # Increment monthly count
                    usage_update_payload['messages_this_month'] = messages_this_month
                elif is_free_plan:
                    # Add hourly tracking updates to payload
                    usage_update_payload['messages_this_hour'] = messages_this_hour
                    usage_update_payload['last_message_timestamp'] = last_message_timestamp.isoformat() # Save ISO format

                # --- Call Nebius API ---
                try:
                    print(f"User {user_id} ({plan_name}) sending message. Current counts: H={messages_this_hour}, M={messages_this_month}, Total={message_count}")
                    response = nebius_client.chat.completions.create(
                        model=NEBIUS_MODEL,
                        messages=[{"role": "user", "content": user_message}]
                    )
                    bot_response = response.choices[0].message.content.strip()

                    # --- Update user record in Supabase with usage changes ---
                    # This happens only if the API call was successful
                    try:
                        update_res = supabase.table('users').update(usage_update_payload).eq('id', user_id).execute()
                        if not update_res.data:
                            print(f"Warning: Failed to update usage stats for user {user_id} after successful API call. Payload: {usage_update_payload}, Response: {update_res}")
                            flash('Could not save message usage stats.', 'warning')
                        else:
                            print(f"Successfully updated usage stats for user {user_id}")
                            # Update display string immediately if Standard plan
                            if is_standard_plan:
                                current_limit_info = f"{messages_this_month}/{STANDARD_PLAN_MONTHLY_LIMIT} messages this month"


                    except Exception as e:
                        print(f"Error updating Supabase usage for user {user_id} after successful API call: {e}")
                        flash('Error saving message usage progress.', 'error')
                        # Note: The message was sent, but the count might be off now.

                except Exception as e:
                    print(f"Error calling Nebius API or processing response: {e}")
                    bot_response = "Sorry, I encountered an error processing your request."
                    # IMPORTANT: Do not update usage counts in DB if API failed.

            # End of allow_message block

        elif not nebius_client:
             bot_response = "Chatbot service is currently unavailable."
        elif not user_message:
             flash("Please enter a message.", "info") # Handle empty submission

    # --- Prepare THE SINGLE Checkout Link with Prefill ---
    # Use the base link defined near the top
    upgrade_checkout_link = LEMONSQUEEZY_CHECKOUT_LINK_BASE # Default to base link
    if LEMONSQUEEZY_CHECKOUT_LINK_BASE != "YOUR_SINGLE_CHECKOUT_LINK_HERE":
        try:
            upgrade_checkout_link = f"{LEMONSQUEEZY_CHECKOUT_LINK_BASE}?checkout[email]={user_email}"
        except Exception as e:
            print(f"Error encoding email for checkout link: {e}")
            upgrade_checkout_link = LEMONSQUEEZY_CHECKOUT_LINK_BASE # Fallback to base link


    # --- Render Template ---
    return render_template('home.html',
                           user_email=user_email,
                           plan_name=plan_name,
                           current_limit_info=current_limit_info, # Pass combined limit string
                           is_free_plan=is_free_plan,
                           is_standard_plan=is_standard_plan,
                           is_pro_plan=is_pro_plan,
                           user_message=user_message,
                           bot_response=bot_response,
                           message_limit_reached=message_limit_reached,
                           upgrade_checkout_link=upgrade_checkout_link, # Pass the single link
                           STANDARD_PLAN_MONTHLY_LIMIT=STANDARD_PLAN_MONTHLY_LIMIT # Pass limit constant
                           )

# --- Lemon Squeezy Webhook Handler ---
@app.route('/webhook/lemonsqueezy', methods=['POST'])
def lemonsqueezy_webhook():
    """Handles incoming webhooks from Lemon Squeezy."""
    # 1. Verify Signature
    secret = LEMONSQUEEZY_WEBHOOK_SECRET
    if not secret:
        print("Webhook Error: LEMONSQUEEZY_WEBHOOK_SECRET not configured.")
        abort(500) # Internal Server Error

    signature = request.headers.get('X-Signature')
    if not signature:
        print("Webhook Error: X-Signature header missing.")
        abort(400) # Bad Request

    # Get raw body data (important for correct signature verification)
    payload = request.get_data()
    try:
        # Calculate expected signature
        computed_hash = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256)
        digest = computed_hash.hexdigest()

        # Compare signatures securely
        if not hmac.compare_digest(digest, signature):
            print(f"Webhook Error: Signature mismatch. Received: {signature}, Computed: {digest}")
            abort(403) # Forbidden
        print("Webhook signature verified successfully.")

    except Exception as e:
        print(f"Webhook Error: Exception during signature verification: {e}")
        abort(500)

    # 2. Process the event payload (now that it's verified)
    try:
        event_data = json.loads(payload.decode('utf-8'))
    except json.JSONDecodeError:
        print("Webhook Error: Invalid JSON payload received.")
        abort(400)

    event_name = request.headers.get('X-Event-Name')
    print(f"--- Received Lemon Squeezy Webhook ---")
    print(f"Event Name: {event_name}")
    # print(f"Payload: {json.dumps(event_data, indent=2)}") # Uncomment for detailed debugging

    # 3. Handle Specific Events based on event_name
    try:
        meta = event_data.get('meta', {})
        webhook_id = meta.get('webhook_id') # Useful for logging/debugging
        # Ensure data and attributes exist before accessing them deeply
        data_obj = event_data.get('data')
        if not data_obj:
             print(f"Webhook Warning ({webhook_id}): 'data' object missing in payload for event {event_name}.")
             return 'Warning: Missing data object', 202 # Acknowledge but can't process

        attributes = data_obj.get('attributes', {})
        if not attributes:
            print(f"Webhook Warning ({webhook_id}): 'attributes' object missing in payload for event {event_name}.")
            return 'Warning: Missing attributes object', 202 # Acknowledge but can't process

        ls_customer_id = attributes.get('customer_id')
        if not ls_customer_id:
            print(f"Webhook Warning ({webhook_id}): Missing 'customer_id' in attributes for event {event_name}.")
            # Depending on the event, might still be processable if other IDs are present (e.g., subscription_id for cancellation)
            # For now, let's require customer_id for linking to our users table.
            return 'Warning: Missing customer ID', 202

        ls_customer_id_str = str(ls_customer_id) # Ensure it's a string for comparison/DB lookup

        # --- Subscription Created or Updated ---
        # Check for events indicating an active subscription change
        # 'subscription_payment_success' might also trigger resets if needed
        # For simplicity, focus on 'created' and 'updated' for plan changes/starts
        if event_name in ['subscription_created', 'subscription_updated']:
            variant_id = attributes.get('variant_id')
            renews_at_str = attributes.get('renews_at') # e.g., "2025-05-20T17:26:09.000000Z"
            status = attributes.get('status') # e.g., "active", "past_due", "cancelled", etc.

            # We only care about *active* subscriptions triggering plan upgrades/updates
            if status != 'active':
                print(f"Webhook Info ({webhook_id}): Ignoring non-active subscription status '{status}' in {event_name} for LS Customer {ls_customer_id_str}")
                # If status is 'cancelled', let the 'subscription_cancelled' handler deal with it
                return f'Ignoring non-active status: {status}', 200

            if not variant_id:
                print(f"Webhook Warning ({webhook_id}): Missing 'variant_id' in active {event_name} for LS Customer {ls_customer_id_str}")
                return 'Warning: Missing variant ID', 202

            variant_id_str = str(variant_id)

            # Determine next reset date from 'renews_at'
            next_reset_date = None
            if renews_at_str:
                try:
                    # Parse ISO string, convert to UTC, then get date part
                    next_reset_date = datetime.fromisoformat(renews_at_str.replace('Z', '+00:00')).astimezone(timezone.utc).date()
                except ValueError:
                    print(f"Webhook Warning ({webhook_id}): Could not parse renews_at date: {renews_at_str}. Using fallback.")
                    next_reset_date = date.today() + timedelta(days=30) # Fallback
            else:
                print(f"Webhook Warning ({webhook_id}): 'renews_at' is missing in {event_name}. Using fallback reset date.")
                next_reset_date = date.today() + timedelta(days=30) # Fallback


            # Prepare base update payload for database
            update_payload = {
                'is_free_plan': False,           # No longer free if they have an active subscription
                'messages_this_month': 0,        # Reset monthly count
                'messages_this_hour': 0,         # Reset hourly tracking
                'last_message_timestamp': None,  # Clear hourly timestamp
                'usage_reset_date': next_reset_date.isoformat() if next_reset_date else None
            }

            # Set plan flags based on the purchased variant_id
            if variant_id_str == str(LEMONSQUEEZY_STANDARD_VARIANT_ID):
                print(f"Webhook Processing ({webhook_id}): Setting plan to Standard ({variant_id_str}) for LS Customer {ls_customer_id_str}")
                update_payload['is_standard_plan'] = True
                update_payload['is_pro_plan'] = False
            elif variant_id_str == str(LEMONSQUEEZY_PRO_VARIANT_ID):
                print(f"Webhook Processing ({webhook_id}): Setting plan to Pro ({variant_id_str}) for LS Customer {ls_customer_id_str}")
                update_payload['is_standard_plan'] = False
                update_payload['is_pro_plan'] = True
            else:
                print(f"Webhook Info ({webhook_id}): Ignoring {event_name} for unknown variant ID: {variant_id_str} for LS Customer {ls_customer_id_str}")
                return 'Event for unknown variant ignored', 200 # Acknowledge, but don't process

            # --- Find user in DB via LS Customer ID and apply update ---
            try:
                # Use maybe_single() to handle cases where user might not exist gracefully
                res = supabase.table('users').update(update_payload).eq('lemonsqueezy_customer_id', ls_customer_id_str).execute()

                # Check if update affected any rows (res.data usually contains the updated rows)
                if res.data and len(res.data) > 0:
                    print(f"Webhook Success ({webhook_id}): Successfully updated user plan for LS Customer {ls_customer_id_str}")
                    return 'Webhook processed successfully', 200
                elif res.data and len(res.data) == 0: # Update executed but found no matching rows
                    print(f"Webhook Warning ({webhook_id}): User not found in Supabase for LS Customer ID: {ls_customer_id_str} during plan update.")
                    return 'User not found in DB', 202 # Acknowledge, but couldn't process
                else: # Potential error in response structure or execution
                    print(f"Webhook Error ({webhook_id}): Failed Supabase update for LS Customer {ls_customer_id_str}. Unexpected response: {res}")
                    abort(500) # Signal internal error

            except Exception as e:
                 print(f"Webhook Error ({webhook_id}): Exception during Supabase update for LS Customer {ls_customer_id_str}: {e}")
                 abort(500)


        # --- Subscription Cancelled ---
        # Handle events that mean the user should be downgraded
        elif event_name in ['subscription_cancelled', 'subscription_expired']: # Check LS docs for exact event names
            print(f"Webhook Processing ({webhook_id}): Handling cancellation/expiry for LS Customer {ls_customer_id_str}")
            # Downgrade user back to the Free plan
            downgrade_payload = {
                'is_free_plan': True,
                'is_standard_plan': False,
                'is_pro_plan': False,
                'messages_this_month': 0,        # Reset monthly count
                'messages_this_hour': 0,         # Reset hourly tracking
                'last_message_timestamp': None,  # Clear hourly timestamp
                'usage_reset_date': None         # Clear reset date
            }
            try:
                res = supabase.table('users').update(downgrade_payload).eq('lemonsqueezy_customer_id', ls_customer_id_str).execute()

                if res.data and len(res.data) > 0:
                    print(f"Webhook Success ({webhook_id}): Successfully downgraded user to Free plan for LS Customer {ls_customer_id_str}")
                    return 'Webhook processed successfully', 200
                elif res.data and len(res.data) == 0:
                    print(f"Webhook Warning ({webhook_id}): User not found for LS Customer ID: {ls_customer_id_str} during cancellation.")
                    return 'User not found in DB', 202
                else:
                    print(f"Webhook Error ({webhook_id}): Failed Supabase downgrade for LS Customer {ls_customer_id_str}. Response: {res}")
                    abort(500)
            except Exception as e:
                 print(f"Webhook Error ({webhook_id}): Exception during Supabase downgrade for LS Customer {ls_customer_id_str}: {e}")
                 abort(500)

        else:
            # Acknowledge other events we aren't explicitly handling
            print(f"Webhook Info ({webhook_id}): Ignoring unhandled event: {event_name}")
            return 'Event ignored', 200

    except Exception as e:
        # Catch-all for errors during event processing logic
        webhook_id = event_data.get('meta', {}).get('webhook_id', 'UNKNOWN')
        print(f"Webhook Error ({webhook_id}): Unhandled exception processing event {event_name}: {e}")
        import traceback
        traceback.print_exc() # Print full traceback for debugging
        abort(500) # Signal internal error

    # Fallback (should ideally be handled by specific event logic above)
    return 'Webhook received but not processed', 200


if __name__ == '__main__':
    # Use 0.0.0.0 to make it accessible on your network if needed
    # Debug=True is helpful during development but MUST be False in production
    # Use a proper WSGI server like Gunicorn or Waitress in production
    app.run(debug=True, host='0.0.0.0', port=5000)