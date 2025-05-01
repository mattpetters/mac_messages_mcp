"""
Core functionality for interacting with macOS Messages app
"""
import os
import re
import sqlite3
import subprocess
import json
import time
import difflib
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Tuple
import glob

def run_applescript(script: str) -> str:
    """Run an AppleScript and return the result."""
    proc = subprocess.Popen(['osascript', '-e', script], 
                            stdout=subprocess.PIPE, 
                            stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        return f"Error: {err.decode('utf-8')}"
    return out.decode('utf-8').strip()

def get_chat_mapping() -> Dict[str, str]:
    """
    Get mapping from room_name to display_name in chat table
    """
    conn = sqlite3.connect(get_messages_db_path())
    cursor = conn.cursor()

    cursor.execute("SELECT room_name, display_name FROM chat")
    result_set = cursor.fetchall()

    mapping = {room_name: display_name for room_name, display_name in result_set}

    conn.close()

    return mapping

def extract_body_from_attributed(attributed_body):
    """
    Extract message content from attributedBody binary data
    """
    if attributed_body is None:
        return None
        
    try:
        # Try to decode attributedBody 
        decoded = attributed_body.decode('utf-8', errors='replace')
        
        # Extract content using pattern matching
        if "NSNumber" in decoded:
            decoded = decoded.split("NSNumber")[0]
            if "NSString" in decoded:
                decoded = decoded.split("NSString")[1]
                if "NSDictionary" in decoded:
                    decoded = decoded.split("NSDictionary")[0]
                    decoded = decoded[6:-12]
                    return decoded
    except Exception as e:
        print(f"Error extracting from attributedBody: {e}")
    
    return None


def get_messages_db_path() -> str:
    """Get the path to the Messages database."""
    home_dir = os.path.expanduser("~")
    return os.path.join(home_dir, "Library/Messages/chat.db")

def query_messages_db(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Query the Messages database and return results as a list of dictionaries."""
    try:
        db_path = get_messages_db_path()
        
        # Check if the database file exists and is accessible
        if not os.path.exists(db_path):
            return [{"error": f"Messages database not found at {db_path}"}]
            
        # Try to connect to the database
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.OperationalError as e:
            return [{"error": f"Cannot access Messages database. Please grant Full Disk Access permission to your terminal application in System Preferences > Security & Privacy > Privacy > Full Disk Access. Error: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."}]
            
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results
    except Exception as e:
        return [{"error": str(e)}]
    
def normalize_phone_number(phone: str) -> str:
    """
    Normalize a phone number by removing all non-digit characters.
    If it's a US number (10 digits) and doesn't have a country code,
    prepend +1 to ensure proper iMessage delivery.
    """
    if not phone:
        return ""
        
    # Remove all non-digit characters
    digits = ''.join(c for c in phone if c.isdigit())
    
    # If it's a US 10-digit number without country code, add +1
    if len(digits) == 10:
        return f"+1{digits}"
    # If it already has country code but no + prefix
    elif len(digits) > 10 and not phone.startswith('+'):
        return f"+{digits}"
    # If it already has + prefix, keep original format
    elif phone.startswith('+'):
        return phone
    else:
        return digits

# Global cache for contacts map
_CONTACTS_CACHE = None
_LAST_CACHE_UPDATE = 0
_CACHE_TTL = 300  # 5 minutes in seconds

def clean_name(name: str) -> str:
    """
    Clean a name by removing emojis and extra whitespace.
    """
    # Remove emoji and other non-alphanumeric characters except spaces, hyphens, and apostrophes
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\U00002702-\U000027B0"  # Dingbats
        "\U000024C2-\U0001F251" 
        "]+"
    )
    
    name = emoji_pattern.sub(r'', name)
    
    # Keep alphanumeric, spaces, apostrophes, and hyphens
    name = re.sub(r'[^\w\s\'\-]', '', name, flags=re.UNICODE)
    
    # Remove extra whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name

def fuzzy_match(query: str, candidates: List[Tuple[str, Any]], threshold: float = 0.6) -> List[Tuple[str, Any, float]]:
    """
    Find fuzzy matches between query and a list of candidates.
    
    Args:
        query: The search string
        candidates: List of (name, value) tuples to search through
        threshold: Minimum similarity score (0-1) to consider a match
        
    Returns:
        List of (name, value, score) tuples for matches, sorted by score
    """
    query = clean_name(query).lower()
    results = []
    
    for name, value in candidates:
        clean_candidate = clean_name(name).lower()
        
        # Try exact match first (case insensitive)
        if query == clean_candidate:
            results.append((name, value, 1.0))
            continue
            
        # Check if query is a substring of the candidate
        if query in clean_candidate:
            # Longer substring matches get higher scores
            score = len(query) / len(clean_candidate) * 0.9  # max 0.9 for substring
            if score >= threshold:
                results.append((name, value, score))
                continue
                
        # Otherwise use difflib for fuzzy matching
        score = difflib.SequenceMatcher(None, query, clean_candidate).ratio()
        if score >= threshold:
            results.append((name, value, score))
    
    # Sort results by score (highest first)
    return sorted(results, key=lambda x: x[2], reverse=True)

def query_addressbook_db(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Query the AddressBook database and return results as a list of dictionaries."""
    try:
        # Find the AddressBook database paths
        home_dir = os.path.expanduser("~")
        sources_path = os.path.join(home_dir, "Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")
        db_paths = glob.glob(sources_path)
        
        if not db_paths:
            return [{"error": f"AddressBook database not found at {sources_path} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."}]
        
        # Try each database path until one works
        all_results = []
        for db_path in db_paths:
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query, params)
                results = [dict(row) for row in cursor.fetchall()]
                conn.close()
                all_results.extend(results)
            except sqlite3.OperationalError as e:
                # If we can't access this one, try the next database
                print(f"Warning: Cannot access {db_path}: {str(e)}")
                continue
        
        if not all_results and len(db_paths) > 0:
            return [{"error": f"Could not access any AddressBook databases. Please grant Full Disk Access permission. PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."}]
            
        return all_results
    except Exception as e:
        return [{"error": str(e)}]

def get_addressbook_contacts() -> Dict[str, str]:
    """
    Query the macOS AddressBook database to get contacts and their phone numbers.
    Returns a dictionary mapping normalized phone numbers to contact names.
    """
    contacts_map = {}
    
    # Define the query to get contact names and phone numbers
    query = """
    SELECT 
        ZABCDRECORD.ZFIRSTNAME as first_name,
        ZABCDRECORD.ZLASTNAME as last_name,
        ZABCDPHONENUMBER.ZFULLNUMBER as phone
    FROM
        ZABCDRECORD
        LEFT JOIN ZABCDPHONENUMBER ON ZABCDRECORD.Z_PK = ZABCDPHONENUMBER.ZOWNER
    WHERE
        ZABCDPHONENUMBER.ZFULLNUMBER IS NOT NULL
    ORDER BY
        ZABCDRECORD.ZLASTNAME,
        ZABCDRECORD.ZFIRSTNAME,
        ZABCDPHONENUMBER.ZORDERINGINDEX ASC
    """
    
    try:
        # For testing/fallback, parse the user-provided examples in cases where direct DB access fails
        # This is a temporary workaround until full disk access is granted
        if 'USE_TEST_DATA' in os.environ and os.environ['USE_TEST_DATA'].lower() == 'true':
            contacts = [
                {"first_name":"TEST", "last_name":"TEST", "phone":"+11111111111"}
            ]
            return process_contacts(contacts)
        
        # Try to query database directly
        results = query_addressbook_db(query)
        
        if results and "error" in results[0]:
            print(f"Error getting AddressBook contacts: {results[0]['error']}")
            # Fall back to subprocess method if direct DB access fails
            return get_addressbook_contacts_subprocess()
        
        return process_contacts(results)
    except Exception as e:
        print(f"Error getting AddressBook contacts: {str(e)}")
        return {}

def process_contacts(contacts) -> Dict[str, str]:
    """Process contact records into a normalized phone -> name map"""
    contacts_map = {}
    name_to_numbers = {}  # For reverse lookup
    
    for contact in contacts:
        try:
            first_name = contact.get("first_name", "")
            last_name = contact.get("last_name", "")
            phone = contact.get("phone", "")
            
            # Skip entries without phone numbers
            if not phone:
                continue
            
            # Clean up phone number and remove any image metadata
            if "X-IMAGETYPE" in phone:
                phone = phone.split("X-IMAGETYPE")[0]
            
            # Create full name
            full_name = " ".join(filter(None, [first_name, last_name]))
            if not full_name.strip():
                continue
            
            # Normalize phone number and add to map
            normalized_phone = normalize_phone_number(phone)
            if normalized_phone:
                contacts_map[normalized_phone] = full_name
                
                # Add to reverse lookup
                if full_name not in name_to_numbers:
                    name_to_numbers[full_name] = []
                name_to_numbers[full_name].append(normalized_phone)
        except Exception as e:
            # Skip individual entries that fail to process
            print(f"Error processing contact: {str(e)}")
            continue
    
    # Store the reverse lookup in a global variable for later use
    global _NAME_TO_NUMBERS_MAP
    _NAME_TO_NUMBERS_MAP = name_to_numbers
    
    return contacts_map

def get_addressbook_contacts_subprocess() -> Dict[str, str]:
    """
    Legacy method to get contacts using subprocess.
    Only used as fallback when direct database access fails.
    """
    contacts_map = {}
    
    try:
        # Form the SQL query to execute via command line
        cmd = """
        sqlite3 ~/Library/"Application Support"/AddressBook/Sources/*/AddressBook-v22.abcddb<<EOF
        .mode json
        SELECT DISTINCT
            ZABCDRECORD.ZFIRSTNAME [FIRST NAME],
            ZABCDRECORD.ZLASTNAME [LAST NAME],
            ZABCDPHONENUMBER.ZFULLNUMBER [FULL NUMBER]
        FROM
            ZABCDRECORD
            LEFT JOIN ZABCDPHONENUMBER ON ZABCDRECORD.Z_PK = ZABCDPHONENUMBER.ZOWNER
        ORDER BY
            ZABCDRECORD.ZLASTNAME,
            ZABCDRECORD.ZFIRSTNAME,
            ZABCDPHONENUMBER.ZORDERINGINDEX ASC;
        EOF
        """
        
        # Execute the command
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            # Parse the JSON output line by line (it's a series of JSON objects)
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                
                # Remove trailing commas that might cause JSON parsing errors
                line = line.rstrip(',')
                
                try:
                    contact = json.loads(line)
                    first_name = contact.get("FIRST NAME", "")
                    last_name = contact.get("LAST NAME", "")
                    phone = contact.get("FULL NUMBER", "")
                    
                    # Process contact as in the main method
                    if not phone:
                        continue
                        
                    if "X-IMAGETYPE" in phone:
                        phone = phone.split("X-IMAGETYPE")[0]
                    
                    full_name = " ".join(filter(None, [first_name, last_name]))
                    if not full_name.strip():
                        continue
                    
                    normalized_phone = normalize_phone_number(phone)
                    if normalized_phone:
                        contacts_map[normalized_phone] = full_name
                except json.JSONDecodeError:
                    # Skip individual lines that fail to parse
                    continue
    except Exception as e:
        print(f"Error getting AddressBook contacts via subprocess: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE.")
    
    return contacts_map

# Global variable for reverse contact lookup
_NAME_TO_NUMBERS_MAP = {}

def get_cached_contacts() -> Dict[str, str]:
    """Get cached contacts map or refresh if needed"""
    global _CONTACTS_CACHE, _LAST_CACHE_UPDATE
    
    current_time = time.time()
    if _CONTACTS_CACHE is None or (current_time - _LAST_CACHE_UPDATE) > _CACHE_TTL:
        _CONTACTS_CACHE = get_addressbook_contacts()
        _LAST_CACHE_UPDATE = current_time
    
    return _CONTACTS_CACHE

def find_contact_by_name(name: str) -> List[Dict[str, Any]]:
    """
    Find contacts by name using fuzzy matching.
    
    Args:
        name: The name to search for
    
    Returns:
        List of matching contacts (may be multiple if ambiguous)
    """
    contacts = get_cached_contacts()
    
    # Build a list of (name, phone) pairs to search through
    candidates = [(contact_name, phone) for phone, contact_name in contacts.items()]
    
    # Perform fuzzy matching
    matches = fuzzy_match(name, candidates)
    
    # Convert to a list of contact dictionaries
    results = []
    for contact_name, phone, score in matches:
        results.append({
            "name": contact_name,
            "phone": phone,
            "score": score
        })
    
    return results

def send_message(recipient: str, message: str, group_chat: bool = False) -> str:
    """
    Send a message using the Messages app with improved contact resolution.
    
    Args:
        recipient: Phone number, email, contact name, or special format for contact selection
                  Use "contact:N" to select the Nth contact from a previous ambiguous match
        message: Message text to send
        group_chat: Whether this is a group chat (uses chat ID instead of buddy)
    
    Returns:
        Success or error message
    """
    # Convert to string to ensure phone numbers work properly
    recipient = str(recipient).strip()
    
    # Handle contact selection format (contact:N)
    if recipient.lower().startswith("contact:"):
        try:
            # Get the selected index (1-based)
            index = int(recipient.split(":", 1)[1].strip()) - 1
            
            # Get the most recent contact matches from global cache
            if not hasattr(send_message, "recent_matches") or not send_message.recent_matches:
                return "No recent contact matches available. Please search for a contact first."
            
            if index < 0 or index >= len(send_message.recent_matches):
                return f"Invalid selection. Please choose a number between 1 and {len(send_message.recent_matches)}."
            
            # Get the selected contact
            contact = send_message.recent_matches[index]
            # Use the phone number directly instead of the contact name
            formatted_number = normalize_phone_number(contact['phone'])
            return _send_message_to_recipient(formatted_number, message, contact['name'], group_chat)
        except (ValueError, IndexError) as e:
            return f"Error selecting contact: {str(e)}"
    
    # Enhanced check for phone number format
    # Check for common phone number patterns with better matching
    is_phone_number = False
    
    # Check if it's a phone number with various formats
    if (
        # Basic digit check
        all(c.isdigit() or c in '+- ().' for c in recipient) and 
        # Has enough digits to be a phone number (at least 7)
        sum(c.isdigit() for c in recipient) >= 7
    ):
        is_phone_number = True
    
    # If it looks like a phone number, use it directly
    if is_phone_number:
        # Normalize the phone number to proper format for iMessage
        formatted_number = normalize_phone_number(recipient)
        return _send_message_to_recipient(formatted_number, message, None, group_chat)
    
    # Check for email format
    if '@' in recipient and '.' in recipient.split('@')[1]:
        # It's likely an email address, use it directly
        return _send_message_to_recipient(recipient, message, None, group_chat)
    
    # If we're here, try to find the contact by name (last resort)
    contacts = find_contact_by_name(recipient)
    
    if not contacts:
        return f"Error: Could not find any contact matching '{recipient}'"
    
    if len(contacts) == 1:
        # Single match, use phone number directly
        contact = contacts[0]
        formatted_number = normalize_phone_number(contact['phone'])
        return _send_message_to_recipient(formatted_number, message, contact['name'], group_chat)
    else:
        # Store the matches for later selection
        send_message.recent_matches = contacts
        
        # Multiple matches, return them all
        contact_list = "\n".join([f"{i+1}. {c['name']} ({c['phone']})" for i, c in enumerate(contacts[:10])])
        return f"Multiple contacts found matching '{recipient}'. Please specify which one using 'contact:N' where N is the number:\n{contact_list}"

# Initialize the static variable for recent matches
send_message.recent_matches = []

def _send_message_to_recipient(recipient: str, message: str, contact_name: str = None, group_chat: bool = False) -> str:
    """
    Internal function to send a message to a specific recipient using file-based approach.
    
    Args:
        recipient: Phone number or email
        message: Message text to send
        contact_name: Optional contact name for the success message
        group_chat: Whether this is a group chat
    
    Returns:
        Success or error message
    """
    try:
        # Create a temporary file with the message content
        file_path = os.path.abspath('imessage_tmp.txt')
        
        with open(file_path, 'w') as f:
            f.write(message)
        
        # Adjust the AppleScript command based on whether this is a group chat
        if not group_chat:
            command = f'tell application "Messages" to send (read (POSIX file "{file_path}") as «class utf8») to buddy "{recipient}"'
        else:
            command = f'tell application "Messages" to send (read (POSIX file "{file_path}") as «class utf8») to chat "{recipient}"'
        
        # Run the AppleScript
        result = run_applescript(command)
        
        # Clean up the temporary file
        try:
            os.remove(file_path)
        except:
            pass
        
        # Check result
        if result.startswith("Error:"):
            # Try fallback to direct method
            return _send_message_direct(recipient, message, contact_name, group_chat)
        
        # Message sent successfully
        display_name = contact_name if contact_name else recipient
        return f"Message sent successfully to {display_name}"
    except Exception as e:
        # Try fallback method
        return _send_message_direct(recipient, message, contact_name, group_chat)

def get_contact_name(handle_id: int) -> str:
    """
    Get contact name from handle_id with improved contact lookup.
    """
    if handle_id is None:
        return "Unknown"
        
    # First, get the phone number or email
    handle_query = """
    SELECT id FROM handle WHERE ROWID = ?
    """
    handles = query_messages_db(handle_query, (handle_id,))
    
    if not handles or "error" in handles[0]:
        return "Unknown"
    
    handle_id_value = handles[0]["id"]
    
    # Try to match with AddressBook contacts
    contacts = get_cached_contacts()
    normalized_handle = normalize_phone_number(handle_id_value)
    
    # Try different variations of the number for matching
    if normalized_handle in contacts:
        return contacts[normalized_handle]
    
    # Sometimes numbers in the addressbook have the country code, but messages don't
    if normalized_handle.startswith('1') and len(normalized_handle) > 10:
        # Try without country code
        if normalized_handle[1:] in contacts:
            return contacts[normalized_handle[1:]]
    elif len(normalized_handle) == 10:  # US number without country code
        # Try with country code
        if '1' + normalized_handle in contacts:
            return contacts['1' + normalized_handle]
    
    # If no match found in AddressBook, fall back to display name from chat
    contact_query = """
    SELECT 
        c.display_name 
    FROM 
        handle h
    JOIN 
        chat_handle_join chj ON h.ROWID = chj.handle_id
    JOIN 
        chat c ON chj.chat_id = c.ROWID
    WHERE 
        h.id = ? 
    LIMIT 1
    """
    
    contacts = query_messages_db(contact_query, (handle_id_value,))
    
    if contacts and len(contacts) > 0 and "display_name" in contacts[0] and contacts[0]["display_name"]:
        return contacts[0]["display_name"]
    
    # If no contact name found, return the phone number or email
    return handle_id_value

def get_recent_messages(hours: int = 24, contact: Optional[str] = None) -> str:
    """
    Get recent messages from the Messages app using attributedBody for content.
    
    Args:
        hours: Number of hours to look back (default: 24)
        contact: Filter by contact name, phone number, or email (optional)
                Use "contact:N" to select a specific contact from previous matches
    
    Returns:
        Formatted string with recent messages
    """
    handle_id = None
    
    # If contact is specified, try to resolve it
    if contact:
        # Convert to string to ensure phone numbers work properly
        contact = str(contact).strip()
        
        # Handle contact selection format (contact:N)
        if contact.lower().startswith("contact:"):
            try:
                # Get the selected index (1-based)
                index = int(contact.split(":", 1)[1].strip()) - 1
                
                # Get the most recent contact matches from global cache
                if not hasattr(get_recent_messages, "recent_matches") or not get_recent_messages.recent_matches:
                    return "No recent contact matches available. Please search for a contact first."
                
                if index < 0 or index >= len(get_recent_messages.recent_matches):
                    return f"Invalid selection. Please choose a number between 1 and {len(get_recent_messages.recent_matches)}."
                
                # Get the selected contact's phone number directly
                contact = get_recent_messages.recent_matches[index]['phone']
            except (ValueError, IndexError) as e:
                return f"Error selecting contact: {str(e)}"
        
        # Enhanced check for phone number format
        is_phone_number = False
        
        # Check if it's a phone number with various formats
        if (
            # Basic digit check
            all(c.isdigit() or c in '+- ().' for c in contact) and 
            # Has enough digits to be a phone number (at least 7)
            sum(c.isdigit() for c in contact) >= 7
        ):
            is_phone_number = True
        
        # Handle email addresses directly
        is_email = '@' in contact and '.' in contact.split('@')[1]
            
        # Only do contact name lookup if it's not a phone number or email
        if not is_phone_number and not is_email:
            # Try fuzzy matching
            matches = find_contact_by_name(contact)
            
            if not matches:
                return f"No contacts found matching '{contact}'."
            
            if len(matches) == 1:
                # Single match, use its phone number directly
                contact = matches[0]['phone']
            else:
                # Store the matches for later selection
                get_recent_messages.recent_matches = matches
                
                # Multiple matches, return them all
                contact_list = "\n".join([f"{i+1}. {c['name']} ({c['phone']})" for i, c in enumerate(matches[:10])])
                return f"Multiple contacts found matching '{contact}'. Please specify which one using 'contact:N' where N is the number:\n{contact_list}"
        
        # Now contact should be a phone number or email - normalize it
        if is_phone_number:
            contact = normalize_phone_number(contact)
        
        # Try to find handle_id with improved phone number matching
        if is_email:
            # This is an email
            query = "SELECT ROWID FROM handle WHERE id = ?"
            results = query_messages_db(query, (contact,))
            if results and not "error" in results[0] and len(results) > 0:
                handle_id = results[0]["ROWID"]
        else:
            # This is a phone number - try various formats
            handle_id = find_handle_by_phone(contact)
            
        if not handle_id:
            # Try a direct search in message table to see if any messages exist
            normalized = normalize_phone_number(contact) if is_phone_number else contact
            query = """
            SELECT COUNT(*) as count 
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE h.id LIKE ?
            """
            results = query_messages_db(query, (f"%{normalized}%",))
            
            if results and not "error" in results[0] and results[0].get("count", 0) == 0:
                # No messages found but the query was valid
                return f"No message history found with '{contact}'."
            else:
                # Could not find the handle at all
                return f"Could not find any messages with contact '{contact}'. Verify the phone number or email is correct."
    
    # Calculate the timestamp for X hours ago
    current_time = datetime.now(timezone.utc)
    hours_ago = current_time - timedelta(hours=hours)
    
    # Convert to Apple's timestamp format (seconds since 2001-01-01)
    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    seconds_since_apple_epoch = int((hours_ago - apple_epoch).total_seconds())
    
    # Make sure we're using a string representation for the timestamp
    # to avoid integer overflow issues when binding to SQLite
    timestamp_str = str(seconds_since_apple_epoch)
    
    # Build the SQL query - use attributedBody field and text
    query = """
    SELECT 
        m.ROWID,
        m.date, 
        m.text, 
        m.attributedBody,
        m.is_from_me,
        m.handle_id,
        m.cache_roomnames
    FROM 
        message m
    WHERE 
        CAST(m.date AS TEXT) > ? 
    """
    
    params = (timestamp_str,)
    
    # Add contact filter if handle_id was found
    if handle_id:
        query += "AND m.handle_id = ? "
        params = (timestamp_str, handle_id)
    
    query += "ORDER BY m.date DESC LIMIT 100"
    
    # Execute the query
    messages = query_messages_db(query, params)
    
    # Format the results
    if not messages:
        return "No messages found in the specified time period."
    
    if "error" in messages[0]:
        return f"Error accessing messages: {messages[0]['error']}"
    
    # Get chat mapping for group chat names
    chat_mapping = get_chat_mapping()
    
    formatted_messages = []
    for msg in messages:
        # Get the message content from text or attributedBody
        if msg.get('text'):
            body = msg['text']
        elif msg.get('attributedBody'):
            body = extract_body_from_attributed(msg['attributedBody'])
            if not body:
                # Skip messages with no content
                continue
        else:
            # Skip empty messages
            continue
        
        # Convert Apple timestamp to readable date
        try:
            # Convert Apple timestamp to datetime
            date_string = '2001-01-01'
            mod_date = datetime.strptime(date_string, '%Y-%m-%d')
            unix_timestamp = int(mod_date.timestamp()) * 1000000000
            
            # Handle both nanosecond and second format timestamps
            msg_timestamp = int(msg["date"])
            if len(str(msg_timestamp)) > 10:  # It's in nanoseconds
                new_date = int((msg_timestamp + unix_timestamp) / 1000000000)
            else:  # It's already in seconds
                new_date = mod_date.timestamp() + msg_timestamp
                
            date_str = datetime.fromtimestamp(new_date).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OverflowError) as e:
            # If conversion fails, use a placeholder
            date_str = "Unknown date"
            print(f"Date conversion error: {e} for timestamp {msg['date']}")
        
        direction = "You" if msg["is_from_me"] else get_contact_name(msg["handle_id"])
        
        # Check if this is a group chat
        group_chat_name = None
        if msg.get('cache_roomnames'):
            group_chat_name = chat_mapping.get(msg['cache_roomnames'])
        
        message_prefix = f"[{date_str}]"
        if group_chat_name:
            message_prefix += f" [{group_chat_name}]"
        
        formatted_messages.append(
            f"{message_prefix} {direction}: {body}"
        )
    
    if not formatted_messages:
        return "No messages found in the specified time period."
        
    return "\n".join(formatted_messages)

# Initialize the static variable for recent matches
get_recent_messages.recent_matches = []

def _send_message_direct(recipient: str, message: str, contact_name: str = None, group_chat: bool = False) -> str:
    """
    Fallback direct AppleScript method for sending messages.
    """
    # Clean the inputs for AppleScript
    safe_message = message.replace('"', '\\"').replace('\\', '\\\\')
    safe_recipient = recipient.replace('"', '\\"')
    
    # Different script based on group_chat flag
    if not group_chat:
        script = f'''
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            
            try
                -- Activate Messages to ensure it's in the foreground
                activate
                
                -- Try to get the existing buddy if possible
                set targetBuddy to buddy "{safe_recipient}" of targetService
                
                -- Send the message
                send "{safe_message}" to targetBuddy
                
                -- Wait briefly to check for immediate errors
                delay 2
                
                -- Return success
                return "success"
            on error errMsg
                -- If getting buddy fails, try to create a new conversation
                try
                    -- Make sure recipient is in the correct format
                    set formattedRecipient to "{safe_recipient}"
                    
                    -- Try alternative sending method
                    set newMessage to send "{safe_message}" to formattedRecipient
                    
                    -- Wait a bit longer to ensure delivery attempt
                    delay 2
                    
                    return "success"
                on error errMsg2
                    -- Both methods failed
                    return "error:" & errMsg2
                end try
            end try
        end tell
        '''
    else:
        script = f'''
        tell application "Messages"
            try
                -- Activate Messages to ensure it's in the foreground
                activate
                
                -- Try to get the existing chat
                set targetChat to chat "{safe_recipient}"
                
                -- Send the message
                send "{safe_message}" to targetChat
                
                -- Wait briefly to check for immediate errors
                delay 2
                
                -- Return success
                return "success"
            on error errMsg
                -- Chat method failed
                return "error:" & errMsg
            end try
        end tell
        '''
    
    try:
        result = run_applescript(script)
        if result.startswith("error:"):
            return f"Error sending message: {result[6:]}. Please check that the recipient number/email is correct and Messages app is properly set up."
        elif result.strip() == "success":
            display_name = contact_name if contact_name else recipient
            return f"Message sent successfully to {display_name}. Note: A 'Not Delivered' status may appear if there are network issues or if the recipient is not registered with iMessage."
        else:
            return f"Unknown result: {result}. Message may still be in sending state; please check Messages app for status."
    except Exception as e:
        return f"Error sending message: {str(e)}"
    
def check_messages_db_access() -> str:
    """Check if the Messages database is accessible and return detailed information."""
    try:
        db_path = get_messages_db_path()
        status = []
        
        # Check if the file exists
        if not os.path.exists(db_path):
            return f"ERROR: Messages database not found at {db_path} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
        
        status.append(f"Database file exists at: {db_path}")
        
        # Check file permissions
        try:
            with open(db_path, 'rb') as f:
                # Just try to read a byte to confirm access
                f.read(1)
            status.append("File is readable")
        except PermissionError:
            return f"ERROR: Permission denied when trying to read {db_path}. Please grant Full Disk Access permission to your terminal application. PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
        except Exception as e:
            return f"ERROR: Unknown error reading file: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
        
        # Try to connect to the database
        try:
            conn = sqlite3.connect(db_path)
            status.append("Successfully connected to database")
            
            # Test a simple query
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM sqlite_master")
            count = cursor.fetchone()[0]
            status.append(f"Database contains {count} tables")
            
            # Check if the necessary tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('message', 'handle', 'chat')")
            tables = [row[0] for row in cursor.fetchall()]
            if 'message' in tables and 'handle' in tables:
                status.append("Required tables (message, handle) are present")
            else:
                status.append(f"WARNING: Some required tables are missing. Found: {', '.join(tables)}")
            
            conn.close()
        except sqlite3.OperationalError as e:
            return f"ERROR: Database connection error: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
        
        return "\n".join(status)
    except Exception as e:
        return f"ERROR: Unexpected error during database access check: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
    
def find_handle_by_phone(phone: str) -> Optional[int]:
    """
    Find a handle ID by phone number, trying various formats.
    
    Args:
        phone: Phone number in any format
        
    Returns:
        handle_id if found, None otherwise
    """
    # Normalize the phone number (remove all non-digit characters)
    normalized = normalize_phone_number(phone)
    if not normalized:
        return None
    
    # Try various formats for numbers
    formats_to_try = []
    
    # Add the original normalized format (with + if it exists)
    formats_to_try.append(normalized)
    
    # For numbers with country code, try with and without it
    if normalized.startswith('+'):
        # Remove the + but keep the country code
        formats_to_try.append(normalized[1:])
        
        # For US/Canada numbers (+1), also try without country code
        if normalized.startswith('+1') and len(normalized) > 10:
            formats_to_try.append(normalized[2:])
    
    # If it doesn't have a + but appears to have a country code
    elif len(normalized) > 10:
        # Add version with +
        formats_to_try.append('+' + normalized)
        
        # For US/Canada numbers, try without country code
        if normalized.startswith('1') and len(normalized) > 10:
            formats_to_try.append(normalized[1:])
    
    # If it's a standard 10-digit number (US/Canada)
    elif len(normalized) == 10:
        # Try with country code +1
        formats_to_try.append('+1' + normalized)
        formats_to_try.append('1' + normalized)
    
    # Remove duplicates
    formats_to_try = list(set(formats_to_try))
    
    # Prepare query placeholders
    placeholders = ', '.join(['?' for _ in formats_to_try])
    
    # First try direct matches
    query = f"""
    SELECT ROWID FROM handle 
    WHERE id IN ({placeholders})
    """
    
    results = query_messages_db(query, tuple(formats_to_try))
    
    if results and not "error" in results[0] and len(results) > 0:
        return results[0]["ROWID"]
    
    # If no exact match, try LIKE matches with wildcards
    placeholders = ', '.join(['?' for _ in formats_to_try])
    like_params = [f"%{f}%" for f in formats_to_try]
    
    query = f"""
    SELECT ROWID FROM handle 
    WHERE {' OR '.join([f'id LIKE ?' for _ in formats_to_try])}
    """
    
    results = query_messages_db(query, tuple(like_params))
    
    if not results or "error" in results[0] or len(results) == 0:
        return None
    
    return results[0]["ROWID"]

def check_addressbook_access() -> str:
    """Check if the AddressBook database is accessible and return detailed information."""
    try:
        home_dir = os.path.expanduser("~")
        sources_path = os.path.join(home_dir, "Library/Application Support/AddressBook/Sources")
        status = []
        
        # Check if the directory exists
        if not os.path.exists(sources_path):
            return f"ERROR: AddressBook Sources directory not found at {sources_path} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
        
        status.append(f"AddressBook Sources directory exists at: {sources_path}")
        
        # Find database files
        db_paths = glob.glob(os.path.join(sources_path, "*/AddressBook-v22.abcddb"))
        
        if not db_paths:
            return f"ERROR: No AddressBook database files found in {sources_path} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
        
        status.append(f"Found {len(db_paths)} AddressBook database files:")
        for path in db_paths:
            status.append(f" - {path}")
        
        # Check file permissions for each database
        for db_path in db_paths:
            try:
                with open(db_path, 'rb') as f:
                    # Just try to read a byte to confirm access
                    f.read(1)
                status.append(f"File is readable: {db_path}")
            except PermissionError:
                status.append(f"ERROR: Permission denied when trying to read {db_path} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE.")
                continue
            except Exception as e:
                status.append(f"ERROR: Unknown error reading file {db_path}: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE.")
                continue
            
            # Try to connect to the database
            try:
                conn = sqlite3.connect(db_path)
                status.append(f"Successfully connected to database: {db_path}")
                
                # Test a simple query
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM sqlite_master")
                count = cursor.fetchone()[0]
                status.append(f"Database contains {count} tables")
                
                # Check if the necessary tables exist
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ZABCDRECORD', 'ZABCDPHONENUMBER')")
                tables = [row[0] for row in cursor.fetchall()]
                if 'ZABCDRECORD' in tables and 'ZABCDPHONENUMBER' in tables:
                    status.append("Required tables (ZABCDRECORD, ZABCDPHONENUMBER) are present")
                else:
                    status.append(f"WARNING: Some required tables are missing. Found: {', '.join(tables)}")
                
                # Get a count of contacts
                try:
                    cursor.execute("SELECT COUNT(*) FROM ZABCDRECORD")
                    contact_count = cursor.fetchone()[0]
                    status.append(f"Database contains {contact_count} contacts")
                except sqlite3.OperationalError:
                    status.append("Could not query contact count PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE.")
                
                conn.close()
            except sqlite3.OperationalError as e:
                status.append(f"ERROR: Database connection error for {db_path}: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE.")
        
        # Try to get actual contacts
        contacts = get_addressbook_contacts()
        if contacts:
            status.append(f"Successfully retrieved {len(contacts)} contacts with phone numbers")
        else:
            status.append("WARNING: No contacts with phone numbers found. PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE.")
        
        return "\n".join(status)
    except Exception as e:
        return f"ERROR: Unexpected error during database access check: {str(e)} PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."

def get_unread_messages() -> str:
    """
    Get all unread messages from the Messages app.
    
    Returns:
        Formatted string with unread messages, including sender, timestamp, and content
    """
    # Build the SQL query - use is_read=0 to filter for unread messages
    query = """
    SELECT 
        m.ROWID,
        m.date, 
        m.text, 
        m.attributedBody,
        m.is_from_me,
        m.handle_id,
        m.cache_roomnames
    FROM 
        message m
    WHERE 
        m.is_read = 0
        AND m.is_from_me = 0
    ORDER BY m.date ASC
    """
    
    # Execute the query
    messages = query_messages_db(query)
    
    # Format the results
    if not messages:
        return "No unread messages found."
    
    if "error" in messages[0]:
        return f"Error accessing messages: {messages[0]['error']}"
    
    # Get chat mapping for group chat names
    chat_mapping = get_chat_mapping()
    
    formatted_messages = []
    for msg in messages:
        # Get the message content from text or attributedBody
        if msg.get('text'):
            body = msg['text']
        elif msg.get('attributedBody'):
            body = extract_body_from_attributed(msg['attributedBody'])
            if not body:
                # Skip messages with no content
                continue
        else:
            # Skip empty messages
            continue
        
        # Convert Apple timestamp to readable date
        try:
            # Convert Apple timestamp to datetime
            date_string = '2001-01-01'
            mod_date = datetime.strptime(date_string, '%Y-%m-%d')
            unix_timestamp = int(mod_date.timestamp()) * 1000000000
            
            # Handle both nanosecond and second format timestamps
            msg_timestamp = int(msg["date"])
            if len(str(msg_timestamp)) > 10:  # It's in nanoseconds
                new_date = int((msg_timestamp + unix_timestamp) / 1000000000)
            else:  # It's already in seconds
                new_date = mod_date.timestamp() + msg_timestamp
                
            date_str = datetime.fromtimestamp(new_date).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OverflowError) as e:
            # If conversion fails, use a placeholder
            date_str = "Unknown date"
            msg_datetime = datetime.now()  # Fallback for sorting
            print(f"Date conversion error: {e} for timestamp {msg['date']}")
        
        sender = get_contact_name(msg["handle_id"])
        
        # Check if this is a group chat
        group_chat_name = None
        if msg.get('cache_roomnames'):
            group_chat_name = chat_mapping.get(msg['cache_roomnames'])
        
        message_prefix = f"[{date_str}]"
        if group_chat_name:
            message_prefix += f" [{group_chat_name}]"
        
        formatted_messages.append(
            f"{message_prefix} {sender}: {body}"
        )
    
    if not formatted_messages:
        return "No unread messages found."
        
    return "\n".join(formatted_messages)

def analyze_response_times(hours: int = 168, contact: Optional[str] = None) -> str:
    """
    Analyze response time patterns between you and your contacts.
    
    Args:
        hours: Number of hours to look back (default: 168, which is 1 week)
        contact: Optional specific contact to analyze
        
    Returns:
        Formatted string with response time analysis
    """
    handle_id = None
    
    # If contact is specified, try to resolve it (similar to get_recent_messages)
    if contact:
        # Logic to find handle_id (simplified for brevity)
        if '@' in contact:
            query = "SELECT ROWID FROM handle WHERE id = ?"
            results = query_messages_db(query, (contact,))
            if results and not "error" in results[0] and len(results) > 0:
                handle_id = results[0]["ROWID"]
        else:
            handle_id = find_handle_by_phone(contact)
    
    # Calculate the timestamp for X hours ago
    current_time = datetime.now(timezone.utc)
    hours_ago = current_time - timedelta(hours=hours)
    
    # Convert to Apple's timestamp format (seconds since 2001-01-01)
    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    seconds_since_apple_epoch = int((hours_ago - apple_epoch).total_seconds())
    timestamp_str = str(seconds_since_apple_epoch)
    
    # Build the SQL query
    query = """
    SELECT 
        m.ROWID,
        m.date, 
        m.is_from_me,
        m.handle_id,
        m.date_read,
        h.id as contact_id,
        c.display_name as chat_name
    FROM 
        message m
    JOIN 
        handle h ON m.handle_id = h.ROWID
    LEFT JOIN 
        chat_handle_join chj ON h.ROWID = chj.handle_id
    LEFT JOIN 
        chat c ON chj.chat_id = c.ROWID
    WHERE 
        CAST(m.date AS TEXT) > ? 
    """
    
    params = (timestamp_str,)
    
    # Add contact filter if handle_id was found
    if handle_id:
        query += "AND m.handle_id = ? "
        params = (timestamp_str, handle_id)
    
    query += "ORDER BY h.id, m.date ASC"
    
    # Execute the query
    messages = query_messages_db(query, params)
    
    if not messages:
        return "No messages found in the specified time period."
    
    if "error" in messages[0]:
        return f"Error accessing messages: {messages[0]['error']}"
    
    # Process messages by conversation
    conversations = {}
    contacts = get_cached_contacts()
    
    for msg in messages:
        contact_id = msg.get('contact_id', 'unknown')
        
        # Skip system messages or corrupted entries
        if not contact_id or contact_id == 'unknown':
            continue
            
        # Initialize conversation if not seen before
        if contact_id not in conversations:
            # Try to get contact name
            normalized = normalize_phone_number(contact_id)
            contact_name = "Unknown"
            
            # Try different variations of the number for matching
            if normalized in contacts:
                contact_name = contacts[normalized]
            elif normalized.startswith('1') and len(normalized) > 10 and normalized[1:] in contacts:
                contact_name = contacts[normalized[1:]]
            elif len(normalized) == 10 and '1' + normalized in contacts:
                contact_name = contacts['1' + normalized]
            else:
                # Use display name from chat if available
                chat_name = msg.get('chat_name')
                if chat_name:
                    contact_name = chat_name
                else:
                    contact_name = contact_id
            
            conversations[contact_id] = {
                'name': contact_name,
                'messages': [],
                'your_response_times': [],
                'their_response_times': [],
            }
        
        # Add message to conversation with converted timestamp
        try:
            # Convert Apple timestamp to datetime
            msg_timestamp = int(msg["date"])
            mod_date = datetime.strptime('2001-01-01', '%Y-%m-%d')
            
            if len(str(msg_timestamp)) > 10:  # It's in nanoseconds
                unix_timestamp = int(mod_date.timestamp()) * 1000000000
                new_date = int((msg_timestamp + unix_timestamp) / 1000000000)
                msg_datetime = datetime.fromtimestamp(new_date)
            else:  # It's already in seconds
                msg_datetime = mod_date + timedelta(seconds=msg_timestamp)
            
            conversations[contact_id]['messages'].append({
                'timestamp': msg_datetime,
                'is_from_me': msg['is_from_me'],
                'date_read': msg.get('date_read', 0)
            })
        except (ValueError, TypeError, OverflowError) as e:
            # Skip messages with invalid timestamps
            print(f"Timestamp conversion error: {e} for message {msg['ROWID']}")
            continue
    
    # Calculate response times for each conversation
    for contact_id, conversation in conversations.items():
        messages = conversation['messages']
        
        # Need at least 2 messages to calculate response time
        if len(messages) < 2:
            continue
            
        # Sort by timestamp (should already be sorted, but just in case)
        messages.sort(key=lambda x: x['timestamp'])
        
        # Calculate response times
        for i in range(1, len(messages)):
            current_msg = messages[i]
            prev_msg = messages[i-1]
            
            # Skip if same sender (not a response)
            if current_msg['is_from_me'] == prev_msg['is_from_me']:
                continue
                
            # Calculate response time in seconds
            response_time = (current_msg['timestamp'] - prev_msg['timestamp']).total_seconds()
            
            # Add to appropriate list
            if current_msg['is_from_me']:
                # You responded to them
                conversation['your_response_times'].append(response_time)
            else:
                # They responded to you
                conversation['their_response_times'].append(response_time)
    
    # Format the results
    results = []
    results.append(f"Response Time Analysis (Past {hours} hours)")
    results.append("=" * 50)
    
    # Sort conversations by total message count
    sorted_conversations = sorted(
        conversations.items(),
        key=lambda x: len(x[1]['messages']), 
        reverse=True
    )
    
    for contact_id, convo in sorted_conversations:
        your_times = convo['your_response_times']
        their_times = convo['their_response_times']
        
        # Skip conversations with no response data
        if not your_times and not their_times:
            continue
            
        results.append(f"\n## {convo['name']} ({contact_id})")
        results.append(f"Total messages: {len(convo['messages'])}")
        
        if your_times:
            avg_your_time = sum(your_times) / len(your_times)
            results.append(f"Your average response time: {format_duration(avg_your_time)}")
            if len(your_times) > 1:
                fastest = min(your_times)
                slowest = max(your_times)
                results.append(f"Your fastest response: {format_duration(fastest)}")
                results.append(f"Your slowest response: {format_duration(slowest)}")
        else:
            results.append("No responses from you in this time period")
            
        if their_times:
            avg_their_time = sum(their_times) / len(their_times)
            results.append(f"Their average response time: {format_duration(avg_their_time)}")
            if len(their_times) > 1:
                fastest = min(their_times)
                slowest = max(their_times)
                results.append(f"Their fastest response: {format_duration(fastest)}")
                results.append(f"Their slowest response: {format_duration(slowest)}")
        else:
            results.append("No responses from them in this time period")
        
        # Compare response times if both exist
        if your_times and their_times:
            avg_your = sum(your_times) / len(your_times)
            avg_their = sum(their_times) / len(their_times)
            
            if avg_your < avg_their:
                results.append(f"You respond {format_duration(avg_their - avg_your)} faster on average")
            else:
                results.append(f"They respond {format_duration(avg_your - avg_their)} faster on average")
    
    if len(results) <= 2:
        return "No response pattern data found for the specified time period."
        
    return "\n".join(results)

def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)} seconds"
    elif seconds < 3600:
        return f"{int(seconds / 60)} minutes"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        return f"{hours} hours, {minutes} minutes"
    else:
        days = int(seconds / 86400)
        hours = int((seconds % 86400) / 3600)
        return f"{days} days, {hours} hours"

def get_unread_messages_detailed() -> str:
    """
    Get a detailed analysis of unread messages, grouped by conversation.
    
    Returns:
        Formatted string with unread message analysis by conversation
    """
    # Query to get unread messages with additional context
    query = """
    SELECT 
        m.ROWID,
        m.text, 
        m.attributedBody,
        m.date,
        m.handle_id,
        m.cache_roomnames,
        m.has_unseen_mention,
        h.id as contact_id,
        c.display_name as chat_name
    FROM 
        message m
    JOIN 
        handle h ON m.handle_id = h.ROWID
    LEFT JOIN 
        chat_handle_join chj ON h.ROWID = chj.handle_id
    LEFT JOIN 
        chat c ON chj.chat_id = c.ROWID
    WHERE 
        m.is_read = 0
        AND m.is_from_me = 0
    ORDER BY 
        h.id, m.date ASC
    """
    
    # Execute the query
    messages = query_messages_db(query)
    
    # Format the results
    if not messages:
        return "No unread messages found."
    
    if "error" in messages[0]:
        return f"Error accessing messages: {messages[0]['error']}"
    
    # Group messages by conversation
    conversations = {}
    chat_mapping = get_chat_mapping()
    contacts = get_cached_contacts()
    
    for msg in messages:
        # Extract message details
        contact_id = msg.get('contact_id', 'unknown')
        
        # Skip system messages or corrupted entries
        if not contact_id or contact_id == 'unknown':
            continue
        
        # Get message content
        if msg.get('text'):
            body = msg['text']
        elif msg.get('attributedBody'):
            body = extract_body_from_attributed(msg['attributedBody'])
            if not body:
                # Skip messages with no content
                continue
        else:
            # Skip empty messages
            continue
        
        # Convert timestamp
        try:
            # Convert Apple timestamp to datetime
            date_string = '2001-01-01'
            mod_date = datetime.strptime(date_string, '%Y-%m-%d')
            unix_timestamp = int(mod_date.timestamp()) * 1000000000
            
            # Handle both nanosecond and second format timestamps
            msg_timestamp = int(msg["date"])
            if len(str(msg_timestamp)) > 10:  # It's in nanoseconds
                new_date = int((msg_timestamp + unix_timestamp) / 1000000000)
            else:  # It's already in seconds
                new_date = mod_date.timestamp() + msg_timestamp
                
            msg_datetime = datetime.fromtimestamp(new_date)
            date_str = msg_datetime.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OverflowError) as e:
            # If conversion fails, use a placeholder
            date_str = "Unknown date"
            msg_datetime = datetime.now()  # Fallback for sorting
            print(f"Date conversion error: {e} for timestamp {msg['date']}")
        
        # Get sender name or group chat name
        chat_name = None
        if msg.get('cache_roomnames'):
            chat_name = chat_mapping.get(msg.get('cache_roomnames'))
        
        # Try to get contact name
        normalized = normalize_phone_number(contact_id)
        contact_name = "Unknown"
        
        # Try different variations of the number for matching
        if normalized in contacts:
            contact_name = contacts[normalized]
        elif normalized.startswith('1') and len(normalized) > 10 and normalized[1:] in contacts:
            contact_name = contacts[normalized[1:]]
        elif len(normalized) == 10 and '1' + normalized in contacts:
            contact_name = contacts['1' + normalized]
        else:
            # Use display name from chat if available
            if msg.get('chat_name'):
                contact_name = msg.get('chat_name')
            else:
                contact_name = contact_id
        
        # Key for grouping: use group chat name if available, otherwise use contact
        conversation_key = chat_name if chat_name else contact_id
        
        # Initialize conversation if not seen before
        if conversation_key not in conversations:
            conversations[conversation_key] = {
                'name': chat_name if chat_name else contact_name,
                'is_group': chat_name is not None,
                'messages': [],
                'mentions': 0,
                'latest_timestamp': msg_datetime,
                'earliest_timestamp': msg_datetime
            }
        
        # Add message to conversation
        conversations[conversation_key]['messages'].append({
            'body': body,
            'date': date_str,
            'sender': contact_name if chat_name else contact_name,
            'has_mention': msg.get('has_unseen_mention', 0) == 1
        })
        
        # Update conversation metadata
        if msg.get('has_unseen_mention', 0) == 1:
            conversations[conversation_key]['mentions'] += 1
            
        # Update timestamps
        if msg_datetime > conversations[conversation_key]['latest_timestamp']:
            conversations[conversation_key]['latest_timestamp'] = msg_datetime
            
        if msg_datetime < conversations[conversation_key]['earliest_timestamp']:
            conversations[conversation_key]['earliest_timestamp'] = msg_datetime
    
    # Sort conversations by priority:
    # 1. Mentions first
    # 2. Then by recency (latest message)
    sorted_conversations = sorted(
        conversations.items(),
        key=lambda x: (-x[1]['mentions'], -int(x[1]['latest_timestamp'].timestamp()))
    )
    
    # Format the results
    results = []
    total_unread = sum(len(c['messages']) for _, c in sorted_conversations)
    results.append(f"Unread Messages Analysis: {total_unread} total unread messages")
    results.append("=" * 50)
    
    for key, convo in sorted_conversations:
        is_group = convo['is_group']
        message_count = len(convo['messages'])
        mention_count = convo['mentions']
        
        # Calculate time since first unread message
        time_since = datetime.now() - convo['earliest_timestamp']
        days_since = time_since.days
        hours_since = time_since.seconds // 3600
        
        # Format header with priority indicators
        header = f"\n## {convo['name']}"
        if mention_count > 0:
            header += f" [⚠️ {mention_count} mention{'s' if mention_count > 1 else ''}]"
        if days_since > 0:
            header += f" [{days_since}d {hours_since}h old]"
        else:
            header += f" [{hours_since}h old]"
            
        results.append(header)
        results.append(f"{message_count} unread message{'s' if message_count > 1 else ''} in {'group chat' if is_group else 'conversation'}")
        
        # Add message previews (limit to 5 for readability)
        results.append("\nRecent messages:")
        for msg in convo['messages'][-5:]:
            mention_indicator = " [@mentioned]" if msg['has_mention'] else ""
            if is_group:
                results.append(f"[{msg['date']}] {msg['sender']}: {msg['body']}{mention_indicator}")
            else:
                results.append(f"[{msg['date']}] {msg['body']}{mention_indicator}")
    
    if not results:
        return "No unread messages found."
        
    return "\n".join(results)

def find_action_items(hours: int = 72) -> str:
    """
    Scan messages for action items and commitments made by the user.
    
    Looks for phrases that indicate the user has committed to doing something
    and organizes these by recipient.
    
    Args:
        hours: Number of hours to look back (default: 72)
        
    Returns:
        Formatted string with action items grouped by recipient
    """
    # Calculate the timestamp for X hours ago
    current_time = datetime.now(timezone.utc)
    hours_ago = current_time - timedelta(hours=hours)
    
    # Convert to Apple's timestamp format (seconds since 2001-01-01)
    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    seconds_since_apple_epoch = int((hours_ago - apple_epoch).total_seconds())
    timestamp_str = str(seconds_since_apple_epoch)
    
    # Build the SQL query - only get messages sent by user
    query = """
    SELECT 
        m.ROWID,
        m.date, 
        m.text, 
        m.attributedBody,
        m.handle_id,
        m.cache_roomnames,
        h.id as contact_id,
        c.display_name as chat_name
    FROM 
        message m
    JOIN 
        handle h ON m.handle_id = h.ROWID
    LEFT JOIN 
        chat_handle_join chj ON h.ROWID = chj.handle_id
    LEFT JOIN 
        chat c ON chj.chat_id = c.ROWID
    WHERE 
        CAST(m.date AS TEXT) > ? 
        AND m.is_from_me = 1
    ORDER BY 
        h.id, m.date ASC
    """
    
    # Execute the query
    messages = query_messages_db(query, (timestamp_str,))
    
    if not messages:
        return "No messages found in the specified time period."
    
    if "error" in messages[0]:
        return f"Error accessing messages: {messages[0]['error']}"
    
    # Patterns that indicate commitments or action items
    commitment_patterns = [
        r"I'?ll\s+(?:try\s+to\s+)?([^.,;!?]*)",
        r"I\s+will\s+(?:try\s+to\s+)?([^.,;!?]*)",
        r"I\s+can\s+(?:try\s+to\s+)?([^.,;!?]*)",
        r"let\s+me\s+([^.,;!?]*)",
        r"I\s+should\s+(?:probably\s+)?([^.,;!?]*)",
        r"I'm\s+going\s+to\s+([^.,;!?]*)",
        r"I\s+promise\s+(?:to\s+)?([^.,;!?]*)",
        r"I\s+need\s+to\s+([^.,;!?]*)",
        r"I\s+owe\s+you\s+([^.,;!?]*)",
        r"remind\s+me\s+to\s+([^.,;!?]*)",
        r"I\s+have\s+to\s+([^.,;!?]*)",
        r"I\s+must\s+([^.,;!?]*)",
        r"I'?m\s+supposed\s+to\s+([^.,;!?]*)",
        r"I\s+said\s+I'?(?:d|ll)\s+([^.,;!?]*)",
        r"won'?t\s+forget\s+to\s+([^.,;!?]*)",
    ]
    
    # Group messages by conversation
    conversations = {}
    chat_mapping = get_chat_mapping()
    contacts = get_cached_contacts()
    
    for msg in messages:
        # Extract message details
        contact_id = msg.get('contact_id', 'unknown')
        
        # Skip system messages or corrupted entries
        if not contact_id or contact_id == 'unknown':
            continue
        
        # Get message content
        if msg.get('text'):
            body = msg['text']
        elif msg.get('attributedBody'):
            body = extract_body_from_attributed(msg['attributedBody'])
            if not body:
                # Skip messages with no content
                continue
        else:
            # Skip empty messages
            continue
        
        # Convert timestamp
        try:
            # Convert Apple timestamp to datetime
            date_string = '2001-01-01'
            mod_date = datetime.strptime(date_string, '%Y-%m-%d')
            unix_timestamp = int(mod_date.timestamp()) * 1000000000
            
            # Handle both nanosecond and second format timestamps
            msg_timestamp = int(msg["date"])
            if len(str(msg_timestamp)) > 10:  # It's in nanoseconds
                new_date = int((msg_timestamp + unix_timestamp) / 1000000000)
            else:  # It's already in seconds
                new_date = mod_date.timestamp() + msg_timestamp
                
            msg_datetime = datetime.fromtimestamp(new_date)
            date_str = msg_datetime.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OverflowError) as e:
            # If conversion fails, use a placeholder
            date_str = "Unknown date"
            msg_datetime = datetime.now()  # Fallback for sorting
            print(f"Date conversion error: {e} for timestamp {msg['date']}")
        
        # Get chat name or group chat name
        chat_name = None
        if msg.get('cache_roomnames'):
            chat_name = chat_mapping.get(msg.get('cache_roomnames'))
        
        # Try to get contact name
        normalized = normalize_phone_number(contact_id)
        contact_name = "Unknown"
        
        # Try different variations of the number for matching
        if normalized in contacts:
            contact_name = contacts[normalized]
        elif normalized.startswith('1') and len(normalized) > 10 and normalized[1:] in contacts:
            contact_name = contacts[normalized[1:]]
        elif len(normalized) == 10 and '1' + normalized in contacts:
            contact_name = contacts['1' + normalized]
        else:
            # Use display name from chat if available
            if msg.get('chat_name'):
                contact_name = msg.get('chat_name')
            else:
                contact_name = contact_id
        
        # Key for grouping: use group chat name if available, otherwise use contact
        conversation_key = chat_name if chat_name else contact_id
        
        # Initialize conversation if not seen before
        if conversation_key not in conversations:
            conversations[conversation_key] = {
                'name': chat_name if chat_name else contact_name,
                'is_group': chat_name is not None,
                'action_items': []
            }
        
        # Look for commitment patterns in the message
        for pattern in commitment_patterns:
            matches = re.finditer(pattern, body, re.IGNORECASE)
            for match in matches:
                action_item = match.group(1).strip()
                # Skip if empty or too short (likely false positive)
                if len(action_item) < 3:
                    continue
                    
                # Add context by including the surrounding text
                context = body
                if len(context) > 100:
                    # Truncate for readability but keep relevant part
                    start = max(0, match.start() - 50)
                    end = min(len(body), match.end() + 50)
                    context = "..." + body[start:end] + "..." if start > 0 else body[start:end] + "..."
                
                conversations[conversation_key]['action_items'].append({
                    'action': action_item,
                    'context': context,
                    'date': date_str,
                    'timestamp': msg_datetime
                })
    
    # Format the results
    results = []
    total_actions = sum(len(c['action_items']) for _, c in conversations.items())
    
    results.append(f"Action Items and Commitments (Past {hours} hours)")
    results.append("=" * 50)
    
    if total_actions == 0:
        results.append("\nNo action items or commitments found in your recent messages.")
        return "\n".join(results)
    
    results.append(f"\nFound {total_actions} potential action items across {len(conversations)} conversations.")
    
    # Sort conversations by most recent action item
    sorted_conversations = sorted(
        [(k, v) for k, v in conversations.items() if v['action_items']],
        key=lambda x: max(item['timestamp'] for item in x[1]['action_items']),
        reverse=True
    )
    
    for key, convo in sorted_conversations:
        if not convo['action_items']:
            continue
            
        results.append(f"\n## {convo['name']}")
        
        # Sort action items by date (newest first)
        sorted_items = sorted(convo['action_items'], key=lambda x: x['timestamp'], reverse=True)
        
        for item in sorted_items:
            results.append(f"- [{item['date']}] {item['action']}")
            results.append(f"  Context: \"{item['context']}\"")
            results.append("")
    
    return "\n".join(results)