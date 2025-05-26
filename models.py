"""
Copyright (C) 2025 Michael Yingbull

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

"""Data models, configuration, and constants for the Rover Connector.

This module contains all the data structures, enumerations, and constants used
throughout the Rover Connector application. It includes:
- Configuration dataclass for connection settings
- HL7 message representation
- Session state management
- Various enumerations for API states
- Constants for API responses and HL7 processing
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
from enum import Enum

# Version and identification constants
VERSION = "1.0.0"
USER_AGENT = f"Mozilla/5.0 (Windows NT 6.2; WOW64; rv:32.0) Gecko/20100101 Firefox/32.0 (Open-Rover-Connector/{VERSION})"

# HL7 escape sequences mapping
# These sequences are used in HL7 messages and need to be converted to their actual characters
HL7_ESCAPE_SEQUENCES = {
    "\\R\\": "~",        # Repetition separator
    "\\F\\": "|",        # Field separator
    "\\S\\": "^",        # Component separator
    "\\T\\": "&",        # Subcomponent separator
    "\\E\\": "\\",       # Escape character
    "\\.br\\": "\n",     # Line break
    "\\.Zt1\\": "\t",    # Single tab
    "\\.Zt2\\": "\t\t",  # Double tab
    "\\.Zt3\\": "\t\t\t",    # Triple tab
    "\\.Zt4\\": "\t\t\t\t"   # Quadruple tab
}

# API Response constants - these are the expected XML responses from the API
AUTH_GRANTED_RESPONSE = '<Authentication>AccessGranted</Authentication>'
AUTH_DENIED_RESPONSE = '<Authentication>AccessDenied</Authentication>'
EMPTY_HL7_RESPONSE = '<HL7Messages/>'
ACK_ERROR_RESPONSE = '<HL7Messages ReturnCode="1"/>'


class AuthenticationResult(Enum):
    """Possible authentication results from the API.
    
    The API returns specific XML responses that we parse into these states.
    """
    GRANTED = "granted"  # Authentication successful
    DENIED = "denied"    # Invalid credentials
    ERROR = "error"      # Network or parsing error


class Environment(Enum):
    """Available deployment environments.
    
    Each environment has different API endpoints.
    """
    TEST = "test"
    PRODUCTION = "production"


class Province(Enum):
    """Canadian provinces with Excelleris API support.
    
    Each province has its own API subdomain.
    """
    ON = "ON"  # Ontario
    BC = "BC"  # British Columbia


@dataclass
class HL7Message:
    """Represents a single HL7 message downloaded from the API.
    
    HL7 messages are medical data interchange format messages. This class
    handles the storage and processing of these messages.
    
    Attributes:
        id: Unique message identifier from the API
        content: Raw HL7 message content with escape sequences
        version: HL7 version (typically "2.3.1")
    """
    id: str
    content: str
    version: str
    
    def to_file_content(self) -> str:
        """Convert message content to file-ready format.
        
        Processes HL7 escape sequences and returns the message
        ready to be written to a file.
        
        Returns:
            Processed message content with escape sequences replaced
        """
        result = self.content
        # Replace each escape sequence with its actual character
        for escape, replacement in HL7_ESCAPE_SEQUENCES.items():
            result = result.replace(escape, replacement)
        return result


@dataclass
class SessionState:
    """Manages API session state and authentication status.
    
    Tracks the current session's authentication state, timing information,
    and provides methods to check session validity.
    
    Attributes:
        cookie: Session cookie from the API
        authenticated: Whether currently authenticated
        auth_time: When authentication occurred
        last_activity: Last API interaction time
    """
    cookie: Optional[Any] = None
    authenticated: bool = False
    auth_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    
    def is_expired(self, timeout_seconds: int) -> bool:
        """Check if the session has expired based on timeout.
        
        Args:
            timeout_seconds: Session timeout duration in seconds
            
        Returns:
            True if session is expired or invalid, False otherwise
        """
        if not self.authenticated or not self.last_activity:
            return True
        
        elapsed = datetime.now() - self.last_activity
        return elapsed.total_seconds() > timeout_seconds
    
    def update_activity(self):
        """Update the last activity timestamp to current time.
        
        Should be called after each successful API interaction.
        """
        self.last_activity = datetime.now()
    
    def reset(self):
        """Reset session to initial state.
        
        Clears all session data, effectively logging out.
        """
        self.cookie = None
        self.authenticated = False
        self.auth_time = None
        self.last_activity = None


@dataclass
class RoverConfig:
    """Configuration for Rover API connection.
    
    Contains all settings needed to connect to and interact with the
    Excelleris API. Supports both file-based and environment variable
    configuration.
    
    Required fields must be provided either through constructor or
    environment variables (ROVER_USERNAME, ROVER_PASSWORD, etc).
    
    Attributes:
        username: API username
        password: API password
        certificate_path: Path to PFX certificate file
        certificate_password: Password for the certificate
        environment: Deployment environment (test/production)
        province: Province code (ON/BC)
        base_url: Base URL for API (auto-set if not provided)
        api_url: Full API endpoint URL (auto-set if not provided)
        incoming_dir: Directory for saving downloaded messages
        log_dir: Directory for log files
        poll_interval: Seconds between API polls
        pause_after_no_messages: Seconds to pause when no messages found
        session_timeout: Seconds before session expires
        file_permissions: Unix permissions for created files
    
    Example:
        config = RoverConfig(
            username="user123",
            password="pass456",
            certificate_path="/certs/client.pfx",
            certificate_password="certpass",
            environment="production",
            province="ON"
        )
    """
    # Required authentication fields
    username: str
    password: str
    certificate_path: str
    certificate_password: str
    
    # Environment configuration with defaults
    environment: str = "test"
    province: str = "ON"
    base_url: Optional[str] = None
    api_url: Optional[str] = None
    
    # Directory settings
    incoming_dir: str = "./incoming"
    log_dir: str = "./logs"
    
    # Timing settings (in seconds)
    poll_interval: int = 1800  # 30 minutes - how often to check for new messages
    pause_after_no_messages: int = 600  # 10 minutes - pause when no messages available
    session_timeout: int = 900  # 15 minutes - re-authenticate after this time
    
    # File settings
    file_permissions: int = 0o644  # Read/write for owner, read for group/others
    
    def __post_init__(self):
        """Initialize URLs based on province and environment if not provided.
        
        This method runs after the dataclass __init__ and sets up the
        API URLs based on the province and environment settings.
        """
        if not self.base_url or not self.api_url:
            try:
                # Get the appropriate URL strategy for the province
                from .handlers import URLStrategyFactory
                
                province_enum = Province(self.province)
                env_enum = Environment(self.environment)
                strategy = URLStrategyFactory.create(province_enum)
                
                # Set URLs if not explicitly provided
                if not self.base_url:
                    self.base_url = strategy.get_base_url(env_enum)
                if not self.api_url:
                    self.api_url = strategy.get_api_url(env_enum)
            except ValueError:
                # Fall back to Ontario test environment if invalid values
                self.base_url = "https://api.ontest.excelleris.com/"
                self.api_url = "https://api.ontest.excelleris.com/hl7pull.aspx"
    
    def __repr__(self):
        """String representation with sensitive data hidden.
        
        Returns:
            String representation safe for logging
        """
        return (f"RoverConfig(username='{self.username}', "
                f"environment='{self.environment}', province='{self.province}', "
                f"base_url='{self.base_url}')")
