#!/usr/bin/env python3
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

"""Main Rover API client and entry point.

This module contains the main RoverAPIClient class that orchestrates all
components to download HL7 messages from the Excelleris API. It also
includes the configuration loading logic and main entry point.

The client follows a 4-step process:
1. Authenticate with the API using certificates and credentials
2. Query for pending messages
3. Download and save messages
4. Acknowledge receipt of messages

Example usage:
    # From command line
    python -m rover_connector -c config.yaml
    
    # From Python
    from rover_connector import RoverAPIClient, RoverConfig
    
    config = RoverConfig(username="user", password="pass", ...)
    client = RoverAPIClient(config)
    client.run()
"""

import os
import sys
import time
import logging
import logging.handlers
import yaml
import argparse
from typing import Optional, List
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from .models import RoverConfig, SessionState, HL7Message, VERSION, USER_AGENT
from .handlers import (
    CertificateManager, HTTPClient, AuthenticationHandler,
    MessageProcessor, ResponseParser
)


class RoverAPIClient:
    """Main API client for Rover/Excelleris integration.
    
    This class orchestrates all components to provide a complete solution
    for downloading HL7 messages from the Excelleris API. It handles:
    - Session management
    - Authentication and re-authentication
    - Message downloading and processing
    - Error recovery and retries
    - Logging
    
    The client runs in a continuous loop, polling for messages at
    configured intervals.
    
    Attributes:
        config: Configuration settings
        logger: Logger instance
        cert_manager: Handles certificate operations
        session_state: Tracks authentication state
        session: Requests session for HTTP
        http_client: HTTP client with retry logic
        auth_handler: Handles authentication flow
        message_processor: Processes and saves messages
        parser: Parses API responses
    """
    
    def __init__(self, config: RoverConfig):
        """Initialize the Rover API client.
        
        Sets up all components needed for API interaction.
        
        Args:
            config: Configuration object with all settings
        """
        self.config = config
        self.logger = self._setup_logging()
        
        # Initialize all components
        self.cert_manager = CertificateManager(config, self.logger)
        self.session_state = SessionState()
        self.session = self._create_session()
        self.http_client = HTTPClient(self.session, self.cert_manager, self.logger)
        self.auth_handler = AuthenticationHandler(
            config, self.http_client, self.session_state, self.logger
        )
        self.message_processor = MessageProcessor(config, self.logger)
        self.parser = ResponseParser()
        
        self.logger.info(f"Rover API Client initialized - Version {VERSION}")
        self.logger.info(f"Configuration: {config}")
        
    def _setup_logging(self) -> logging.Logger:
        """Configure logging with console and rotating file handlers.
        
        Sets up:
        - Console logging at INFO level
        - File logging at DEBUG level with rotation
        - Appropriate formatting for each handler
        
        Returns:
            Configured logger instance
        """
        logger = logging.getLogger('rover_api')
        logger.setLevel(logging.DEBUG)
        
        # Remove any existing handlers to avoid duplicates
        logger.handlers = []
        
        # Console handler - INFO and above
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
        
        # File handler with rotation - DEBUG and above
        # Ensure log directory exists
        os.makedirs(self.config.log_dir, exist_ok=True)
        
        log_file = os.path.join(self.config.log_dir, 'rover_api.log')
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB per file
            backupCount=5           # Keep 5 backup files
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        
        return logger
        
    def _create_session(self) -> requests.Session:
        """Create and configure requests session with retry logic.
        
        Configures:
        - User agent header
        - Retry strategy for transient failures
        - Connection pooling
        
        Returns:
            Configured requests Session
        """
        session = requests.Session()
        
        # Set user agent
        session.headers.update({"User-Agent": USER_AGENT})
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,                                    # Total retry attempts
            backoff_factor=1,                          # Wait 1, 2, 4 seconds between retries
            status_forcelist=[429, 500, 502, 503, 504] # Retry on these HTTP status codes
        )
        
        # Mount retry adapter for HTTPS
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        
        return session
        
    def authenticate(self) -> bool:
        """Authenticate with the API.
        
        Loads certificates and performs the two-step authentication process.
        
        Returns:
            True if authentication successful, False otherwise
        """
        # Load certificates first
        if not self.cert_manager.load_certificates():
            self.logger.error("Failed to load certificates")
            return False
            
        # Perform authentication
        return self.auth_handler.authenticate()
        
    def query_messages(self) -> Optional[List[HL7Message]]:
        """Query the API for pending messages.
        
        Checks session validity and re-authenticates if needed before
        querying for messages.
        
        Returns:
            List of HL7Message objects or None if error
        """
        # Check authentication status
        if not self.session_state.authenticated:
            self.logger.error("Not authenticated")
            return None
            
        # Check session timeout
        if self.session_state.is_expired(self.config.session_timeout):
            self.logger.warning("Session expired, re-authenticating")
            if not self.authenticate():
                return None
                
        try:
            # Query for new messages - Pending=Yes is mandatory
            query_data = {
                'Page': 'HL7',
                'Query': 'NewRequests',
                'Pending': 'Yes'  # Required parameter
            }
            
            response = self.http_client.post(self.config.api_url, data=query_data)
            self.session_state.update_activity()
            
            # Parse response into message objects
            messages = self.parser.parse_messages(response.text)
            self.logger.info(f"Retrieved {len(messages)} messages")
            
            return messages
            
        except Exception as e:
            self.logger.error(f"Error querying messages: {e}")
            return None
            
    def send_acknowledgment(self, positive: bool = True) -> bool:
        """Send acknowledgment for processed messages.
        
        The API requires acknowledgment after messages are downloaded.
        Positive ACK confirms successful processing, negative ACK
        indicates processing errors.
        
        Args:
            positive: True for positive ACK, False for negative
            
        Returns:
            True if acknowledgment successful, False otherwise
        """
        try:
            ack_data = {
                'Page': 'HL7',
                'ACK': 'Positive' if positive else 'Negative'
            }
            
            response = self.http_client.post(self.config.api_url, data=ack_data)
            success = self.parser.parse_acknowledgment(response.text)
            
            if success:
                self.logger.info(f"Sent {'positive' if positive else 'negative'} acknowledgment")
            else:
                self.logger.error("Acknowledgment failed")
                
            return success
            
        except Exception as e:
            self.logger.error(f"Error sending acknowledgment: {e}")
            return False
            
    def logout(self) -> bool:
        """Logout from the API.
        
        Sends logout request to cleanly terminate the session.
        
        Returns:
            True if logout successful, False otherwise
        """
        return self.auth_handler.logout()
        
    def download_cycle(self) -> bool:
        """Execute one complete download cycle.
        
        A download cycle consists of:
        1. Query for messages
        2. Process and save messages
        3. Send acknowledgment
        
        Returns:
            True if cycle completed successfully, False if errors occurred
        """
        try:
            # Query for available messages
            messages = self.query_messages()
            if messages is None:
                return False
                
            # If no messages available, pause before next attempt
            if not messages:
                self.logger.info(
                    f"No messages available, pausing for "
                    f"{self.config.pause_after_no_messages} seconds"
                )
                time.sleep(self.config.pause_after_no_messages)
                return True
                
            # Process messages
            success_count, failure_count = self.message_processor.process_messages(messages)
            
            # Send appropriate acknowledgment
            if failure_count == 0:
                # All messages processed successfully
                self.send_acknowledgment(positive=True)
            else:
                # Some messages failed - send negative ACK
                self.logger.warning(
                    f"Failed to process {failure_count} of {len(messages)} messages"
                )
                self.send_acknowledgment(positive=False)
                
            return failure_count == 0
            
        except Exception as e:
            self.logger.error(f"Error in download cycle: {e}")
            return False
            
    def run(self):
        """Main run loop - continuously polls for messages.
        
        Runs indefinitely until interrupted (Ctrl+C). Handles:
        - Initial authentication
        - Periodic polling for messages
        - Error recovery
        - Clean shutdown
        """
        self.logger.info("Starting Rover API Client")
        self.logger.info(f"Poll interval: {self.config.poll_interval} seconds")
        self.logger.info(f"Session timeout: {self.config.session_timeout} seconds")
        
        try:
            # Initial authentication
            if not self.authenticate():
                self.logger.error("Initial authentication failed")
                return
                
            # Main polling loop
            while True:
                try:
                    # Execute one download cycle
                    self.download_cycle()
                    
                    # Wait for next poll
                    self.logger.info(
                        f"Waiting {self.config.poll_interval} seconds until next poll "
                        f"(next poll at {datetime.now().strftime('%H:%M:%S')})"
                    )
                    time.sleep(self.config.poll_interval)
                    
                except KeyboardInterrupt:
                    # User pressed Ctrl+C - exit gracefully
                    self.logger.info("Received interrupt signal")
                    break
                except Exception as e:
                    # Unexpected error - log and retry after delay
                    self.logger.error(f"Error in main loop: {e}", exc_info=True)
                    self.logger.info("Waiting 60 seconds before retry")
                    time.sleep(60)
                    
        finally:
            # Cleanup on exit
            self.logger.info("Shutting down Rover API Client")
            try:
                self.logout()
            except:
                pass  # Best effort logout
            self.cert_manager.cleanup()
            self.logger.info("Shutdown complete")


def load_config(config_path: Optional[str] = None) -> RoverConfig:
    """Load configuration from file or environment variables.
    
    Configuration is loaded in the following priority order:
    1. Environment variables (highest priority)
    2. Configuration file
    3. Default values in RoverConfig
    
    Environment variables use the prefix ROVER_ and uppercase names:
    - ROVER_USERNAME
    - ROVER_PASSWORD
    - ROVER_CERT_PATH
    - ROVER_CERT_PASSWORD
    - ROVER_ENVIRONMENT
    - ROVER_PROVINCE
    - etc.
    
    Args:
        config_path: Path to YAML configuration file
        
    Returns:
        Configured RoverConfig object
        
    Raises:
        ValueError: If required configuration is missing
    """
    config_data = {}
    
    # Load from YAML file if provided and exists
    if config_path and os.path.exists(config_path):
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f) or {}
            
    # Override with environment variables
    env_mapping = {
        'ROVER_USERNAME': 'username',
        'ROVER_PASSWORD': 'password',
        'ROVER_CERT_PATH': 'certificate_path',
        'ROVER_CERT_PASSWORD': 'certificate_password',
        'ROVER_ENVIRONMENT': 'environment',
        'ROVER_PROVINCE': 'province',
        'ROVER_BASE_URL': 'base_url',
        'ROVER_API_URL': 'api_url',
        'ROVER_INCOMING_DIR': 'incoming_dir',
        'ROVER_LOG_DIR': 'log_dir',
        'ROVER_POLL_INTERVAL': 'poll_interval',
        'ROVER_PAUSE_AFTER_NO_MESSAGES': 'pause_after_no_messages',
        'ROVER_SESSION_TIMEOUT': 'session_timeout',
        'ROVER_FILE_PERMISSIONS': 'file_permissions'
    }
    
    for env_key, config_key in env_mapping.items():
        if env_key in os.environ:
            value = os.environ[env_key]
            # Convert numeric values
            if config_key in ['poll_interval', 'pause_after_no_messages', 
                            'session_timeout', 'file_permissions']:
                value = int(value)
            config_data[config_key] = value
    
    # Validate required fields
    required_fields = ['username', 'password', 'certificate_path', 'certificate_password']
    missing_fields = [f for f in required_fields if f not in config_data]
    
    if missing_fields:
        raise ValueError(
            f"Missing required configuration fields: {', '.join(missing_fields)}. "
            f"Provide via config file or environment variables."
        )
    
    return RoverConfig(**config_data)


def main():
    """Main entry point for command-line usage.
    
    Parses command-line arguments, loads configuration, and starts
    the Rover API client.
    """
    parser = argparse.ArgumentParser(
        description='Open Rover Connector - Download HL7 messages from Excelleris API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # Use default config.yaml
  python -m rover_connector
  
  # Use custom config file
  python -m rover_connector -c /path/to/config.yaml
  
  # Use environment variables (no config file)
  export ROVER_USERNAME=myuser
  export ROVER_PASSWORD=mypass
  export ROVER_CERT_PATH=/path/to/cert.pfx
  export ROVER_CERT_PASSWORD=certpass
  python -m rover_connector
        """
    )
    
    parser.add_argument(
        '-c', '--config',
        help='Configuration file path (default: config.yaml)',
        default='config.yaml'
    )
    
    parser.add_argument(
        '-v', '--version',
        action='version',
        version=f'%(prog)s {VERSION}'
    )
    
    args = parser.parse_args()
    
    try:
        # Load configuration
        config = load_config(args.config)
        
        # Create and run client
        client = RoverAPIClient(config)
        client.run()
        
    except ValueError as e:
        # Configuration error
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        # Unexpected error
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
