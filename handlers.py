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

"""Business logic handlers and utilities for the Rover Connector.

This module contains all the core business logic for interacting with the
Excelleris API, including:
- HTTP communication with retry logic
- Certificate management
- Authentication handling
- Message processing and storage
- Response parsing
- URL strategy pattern for different provinces
"""

import os
import time
import logging
import tempfile
import shutil
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any
from functools import wraps
from abc import ABC, abstractmethod
from datetime import datetime

# Certificate handling imports
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from .models import (
    HL7Message, AuthenticationResult, Environment, 
    Province, SessionState, RoverConfig,
    AUTH_GRANTED_RESPONSE, AUTH_DENIED_RESPONSE,
    EMPTY_HL7_RESPONSE, ACK_ERROR_RESPONSE
)


def with_retry(max_attempts=3, backoff_factor=1.0, exceptions=(requests.RequestException,)):
    """Decorator for retrying failed operations with exponential backoff.
    
    This decorator will retry a function call if it raises one of the specified
    exceptions. The delay between retries grows exponentially.
    
    Args:
        max_attempts: Maximum number of attempts (including initial)
        backoff_factor: Base multiplier for retry delays
        exceptions: Tuple of exceptions that trigger a retry
        
    Returns:
        Decorated function that implements retry logic
        
    Example:
        @with_retry(max_attempts=3)
        def unstable_network_call():
            return requests.get("https://api.example.com")
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        # Exponential backoff: 1s, 2s, 4s, etc.
                        sleep_time = backoff_factor * (2 ** attempt)
                        time.sleep(sleep_time)
                    else:
                        raise
            raise last_exception
        return wrapper
    return decorator


# URL Strategy Pattern Implementation
class URLStrategy(ABC):
    """Abstract base class for province-specific URL strategies.
    
    Each province has different API endpoints. This pattern allows
    easy addition of new provinces without modifying existing code.
    """
    
    @abstractmethod
    def get_base_url(self, environment: Environment) -> str:
        """Get the base URL for the specified environment.
        
        Args:
            environment: Target environment (test/production)
            
        Returns:
            Base URL string
        """
        pass
    
    @abstractmethod
    def get_api_url(self, environment: Environment) -> str:
        """Get the full API endpoint URL for the specified environment.
        
        Args:
            environment: Target environment (test/production)
            
        Returns:
            Full API URL string
        """
        pass


class OntarioURLStrategy(URLStrategy):
    """URL strategy for Ontario API endpoints."""
    
    def get_base_url(self, environment: Environment) -> str:
        """Get Ontario-specific base URL."""
        if environment == Environment.TEST:
            return "https://api.ontest.excelleris.com/"
        return "https://api.on.excelleris.com/"
    
    def get_api_url(self, environment: Environment) -> str:
        """Get Ontario-specific API URL."""
        return f"{self.get_base_url(environment)}hl7pull.aspx"


class BCURLStrategy(URLStrategy):
    """URL strategy for British Columbia API endpoints."""
    
    def get_base_url(self, environment: Environment) -> str:
        """Get BC-specific base URL."""
        if environment == Environment.TEST:
            return "https://api.bctest.excelleris.com/"
        return "https://api.bc.excelleris.com/"
    
    def get_api_url(self, environment: Environment) -> str:
        """Get BC-specific API URL."""
        return f"{self.get_base_url(environment)}hl7pull.aspx"


class URLStrategyFactory:
    """Factory for creating province-specific URL strategies.
    
    This factory encapsulates the logic for selecting the appropriate
    URL strategy based on the province.
    """
    
    @staticmethod
    def create(province: Province) -> URLStrategy:
        """Create a URL strategy for the specified province.
        
        Args:
            province: Province enumeration value
            
        Returns:
            Appropriate URLStrategy implementation
        """
        strategies = {
            Province.ON: OntarioURLStrategy(),
            Province.BC: BCURLStrategy()
        }
        # Default to Ontario if unknown province
        return strategies.get(province, OntarioURLStrategy())


# Response Parsing
class ResponseParser:
    """Handles parsing of all API responses.
    
    Centralizes XML parsing logic and provides type-safe parsing methods
    for different response types.
    """
    
    @staticmethod
    def parse_auth(response_text: str) -> AuthenticationResult:
        """Parse authentication response XML.
        
        Args:
            response_text: Raw XML response from authentication request
            
        Returns:
            AuthenticationResult indicating success, denial, or error
        """
        if AUTH_GRANTED_RESPONSE in response_text:
            return AuthenticationResult.GRANTED
        elif AUTH_DENIED_RESPONSE in response_text:
            return AuthenticationResult.DENIED
        else:
            return AuthenticationResult.ERROR
    
    @staticmethod
    def parse_messages(response_text: str) -> List[HL7Message]:
        """Parse HL7 messages from XML response.
        
        Expected XML format:
        <HL7Messages Version="2.3.1">
            <Message MsgID="12345">MSH|^~\\&amp;|...</Message>
            <Message MsgID="12346">MSH|^~\\&amp;|...</Message>
        </HL7Messages>
        
        Args:
            response_text: Raw XML response containing messages
            
        Returns:
            List of parsed HL7Message objects
        """
        messages = []
        
        try:
            root = ET.fromstring(response_text)
            
            # Check for empty response
            if root.tag == 'HL7Messages' and len(root) == 0:
                return messages
            
            # Extract each message
            for msg_elem in root.findall('Message'):
                msg_id = msg_elem.get('MsgID', '')
                content = msg_elem.text or ''
                version = root.get('Version', '2.3.1')  # Default to HL7 v2.3.1
                
                if msg_id and content:
                    messages.append(HL7Message(
                        id=msg_id,
                        content=content,
                        version=version
                    ))
                    
        except ET.ParseError as e:
            # Log error but don't raise - return empty list
            # Caller should handle empty list appropriately
            pass
            
        return messages
    
    @staticmethod
    def parse_acknowledgment(response_text: str) -> bool:
        """Parse acknowledgment response.
        
        Args:
            response_text: Raw XML response from acknowledgment request
            
        Returns:
            True if acknowledgment was successful, False otherwise
        """
        # Error response has ReturnCode="1"
        if ACK_ERROR_RESPONSE in response_text:
            return False
        # Success is an empty HL7Messages element
        return EMPTY_HL7_RESPONSE in response_text


# HTTP Communication
class HTTPClient:
    """Handles all HTTP communication with the API.
    
    Provides a consistent interface for HTTP requests with:
    - Automatic retry logic
    - Certificate management
    - Session handling
    - Logging
    """
    
    def __init__(self, session: requests.Session, cert_manager: 'CertificateManager', 
                 logger: logging.Logger):
        """Initialize HTTP client.
        
        Args:
            session: Requests session to use for all HTTP calls
            cert_manager: Certificate manager for SSL/TLS
            logger: Logger instance for debugging
        """
        self.session = session
        self.cert_manager = cert_manager
        self.logger = logger
    
    def _get_request_params(self) -> Dict[str, Any]:
        """Get common request parameters including certificates.
        
        Returns:
            Dictionary of request parameters
        """
        params = {
            'verify': True,  # Verify SSL certificates
            'timeout': 30    # 30 second timeout
        }
        
        # Add client certificate if available
        ssl_context = self.cert_manager.get_ssl_context()
        if ssl_context:
            params['cert'] = ssl_context
            
        # Use custom CA certificate if available
        if self.cert_manager.ca_cert_path:
            params['verify'] = self.cert_manager.ca_cert_path
            
        return params
    
    @with_retry(max_attempts=3, backoff_factor=1.0)
    def get(self, url: str, **kwargs) -> requests.Response:
        """Perform GET request with retry logic.
        
        Args:
            url: Target URL
            **kwargs: Additional parameters for requests.get()
            
        Returns:
            Response object
            
        Raises:
            requests.RequestException: After all retries exhausted
        """
        params = self._get_request_params()
        params.update(kwargs)
        
        self.logger.debug(f"GET request to {url}")
        response = self.session.get(url, **params)
        response.raise_for_status()  # Raise exception for 4xx/5xx status
        return response
    
    @with_retry(max_attempts=3, backoff_factor=1.0)
    def post(self, url: str, data: Dict[str, str], **kwargs) -> requests.Response:
        """Perform POST request with retry logic.
        
        Args:
            url: Target URL
            data: Form data to post
            **kwargs: Additional parameters for requests.post()
            
        Returns:
            Response object
            
        Raises:
            requests.RequestException: After all retries exhausted
        """
        params = self._get_request_params()
        params.update(kwargs)
        
        self.logger.debug(f"POST request to {url} with data: {data}")
        response = self.session.post(url, data=data, **params)
        response.raise_for_status()
        return response


# Certificate Management
class CertificateManager:
    """Manages client certificates for mutual TLS authentication.
    
    Handles loading PFX/PKCS#12 certificates, extracting components,
    and providing them in the format needed by requests library.
    """
    
    def __init__(self, config: RoverConfig, logger: logging.Logger):
        """Initialize certificate manager.
        
        Args:
            config: Configuration containing certificate paths
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.client_cert_path = None
        self.client_key_path = None
        self.ca_cert_path = None
        self.temp_dir = None
        
    def load_certificates(self) -> bool:
        """Load and prepare certificates from PFX file.
        
        Extracts the private key, client certificate, and CA certificates
        from the PFX file and saves them as temporary PEM files.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Verify certificate file exists
            if not os.path.exists(self.config.certificate_path):
                self.logger.error(f"Certificate file not found: {self.config.certificate_path}")
                return False
                
            self.logger.info(f"Loading certificate from {self.config.certificate_path}")
            
            # Create temporary directory for extracted certificates
            self.temp_dir = tempfile.mkdtemp()
            
            # Read PFX file
            with open(self.config.certificate_path, 'rb') as f:
                pfx_data = f.read()
                
            # Extract certificate components from PFX
            try:
                private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
                    pfx_data,
                    self.config.certificate_password.encode(),
                    backend=default_backend()
                )
            except Exception as e:
                self.logger.error(f"Failed to load PFX file - verify certificate password: {e}")
                return False
                
            # Validate certificate expiration
            if certificate:
                days_until_expiry = (certificate.not_valid_after - datetime.now()).days
                
                if days_until_expiry < 0:
                    self.logger.error("Certificate has expired")
                    return False
                elif days_until_expiry < 30:
                    self.logger.warning(f"Certificate expires in {days_until_expiry} days")
                else:
                    self.logger.info(f"Certificate valid for {days_until_expiry} days")
                    
            # Save private key as PEM
            self.client_key_path = os.path.join(self.temp_dir, 'client.key')
            with open(self.client_key_path, 'wb') as f:
                f.write(private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()
                ))
                
            # Save client certificate as PEM
            self.client_cert_path = os.path.join(self.temp_dir, 'client.crt')
            with open(self.client_cert_path, 'wb') as f:
                f.write(certificate.public_bytes(serialization.Encoding.PEM))
                
            # Save CA certificates if present
            if additional_certs:
                self.ca_cert_path = os.path.join(self.temp_dir, 'ca.crt')
                with open(self.ca_cert_path, 'wb') as f:
                    for cert in additional_certs:
                        f.write(cert.public_bytes(serialization.Encoding.PEM))
                        
            self.logger.info("Certificates loaded successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error loading certificates: {e}")
            return False
            
    def get_ssl_context(self) -> Optional[Tuple[str, str]]:
        """Get SSL context for requests library.
        
        Returns:
            Tuple of (cert_path, key_path) or None if not loaded
        """
        if self.client_cert_path and self.client_key_path:
            return (self.client_cert_path, self.client_key_path)
        return None
        
    def cleanup(self):
        """Clean up temporary certificate files.
        
        Removes the temporary directory and all extracted certificate files.
        Should be called when the application shuts down.
        """
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            self.logger.debug("Cleaned up temporary certificate files")


# Authentication Handler
class AuthenticationHandler:
    """Handles the two-step authentication process with the API.
    
    The Excelleris API requires:
    1. Initial HTTPS connection to establish session
    2. POST request with credentials
    """
    
    def __init__(self, config: RoverConfig, http_client: HTTPClient, 
                 session_state: SessionState, logger: logging.Logger):
        """Initialize authentication handler.
        
        Args:
            config: Configuration with credentials
            http_client: HTTP client for API calls
            session_state: Session state to update
            logger: Logger instance
        """
        self.config = config
        self.http_client = http_client
        self.session_state = session_state
        self.logger = logger
        self.parser = ResponseParser()
    
    def authenticate(self) -> bool:
        """Perform two-step authentication process.
        
        Returns:
            True if authentication successful, False otherwise
        """
        try:
            # Step 1: Initial HTTPS connection to get session cookie
            self.logger.info("Starting authentication - Step 1: Initial connection")
            response = self.http_client.get(self.config.base_url, allow_redirects=True)
            
            # Update session cookies
            if response.cookies:
                self.http_client.session.cookies.update(response.cookies)
                
            # Step 2: POST credentials with silent mode
            self.logger.info("Authentication - Step 2: Sending credentials")
            auth_data = {
                'Page': 'Login',
                'Mode': 'Silent',  # Silent mode for API access
                'UserID': self.config.username,
                'Password': self.config.password
            }
            
            response = self.http_client.post(
                self.config.api_url,
                data=auth_data,
                allow_redirects=True
            )
            
            # Parse authentication response
            result = self.parser.parse_auth(response.text)
            
            if result == AuthenticationResult.GRANTED:
                self.session_state.authenticated = True
                self.session_state.auth_time = datetime.now()
                self.session_state.update_activity()
                self.logger.info("Authentication successful")
                return True
            else:
                self.logger.error(f"Authentication failed: {result.value}")
                return False
                
        except Exception as e:
            self.logger.error(f"Authentication error: {e}")
            return False
    
    def logout(self) -> bool:
        """Perform logout to cleanly end session.
        
        Returns:
            True if logout successful, False otherwise
        """
        try:
            self.logger.info("Logging out")
            response = self.http_client.post(
                self.config.api_url,
                data={'Logout': 'Yes'}
            )
            
            # Clear session state
            self.session_state.reset()
            self.logger.info("Logout successful")
            return True
            
        except Exception as e:
            self.logger.error(f"Logout error: {e}")
            return False


# Message Processing
class MessageProcessor:
    """Handles processing and storage of HL7 messages.
    
    Responsible for:
    - Creating necessary directories
    - Saving messages to files
    - Setting appropriate file permissions
    - Generating unique filenames
    """
    
    def __init__(self, config: RoverConfig, logger: logging.Logger):
        """Initialize message processor.
        
        Args:
            config: Configuration with directory settings
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self._ensure_directories()
    
    def _ensure_directories(self):
        """Ensure required directories exist.
        
        Creates incoming and log directories if they don't exist.
        """
        Path(self.config.incoming_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config.log_dir).mkdir(parents=True, exist_ok=True)
    
    def process_messages(self, messages: List[HL7Message]) -> Tuple[int, int]:
        """Process a list of messages and save to files.
        
        Args:
            messages: List of HL7Message objects to process
            
        Returns:
            Tuple of (success_count, failure_count)
        """
        success_count = 0
        failure_count = 0
        
        for message in messages:
            try:
                self.save_message(message)
                success_count += 1
                self.logger.info(f"Successfully processed message {message.id}")
            except Exception as e:
                failure_count += 1
                self.logger.error(f"Failed to process message {message.id}: {e}")
                
        return success_count, failure_count
    
    def save_message(self, message: HL7Message):
        """Save a single message to file.
        
        Creates a unique filename based on timestamp and message ID,
        processes HL7 escape sequences, and sets appropriate permissions.
        
        Args:
            message: HL7Message to save
            
        Raises:
            OSError: If file cannot be written
        """
        # Generate unique filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"HL7_{timestamp}_{message.id}.hl7"
        filepath = Path(self.config.incoming_dir) / filename
        
        # Write processed message content
        content = message.to_file_content()
        filepath.write_text(content, encoding='utf-8')
        
        # Set file permissions (Unix-like systems)
        os.chmod(filepath, self.config.file_permissions)
        
        self.logger.debug(f"Saved message {message.id} to {filepath}")
