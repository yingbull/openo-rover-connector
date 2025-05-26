"""Open Rover Connector - Connects to Excelleris API to download HL7 messages.

This package provides a client for connecting to the Rover/Excelleris API to download
HL7 messages following their 4-step process:
1. Initial HTTPS connection with certificate
2. Authentication with credentials
3. Query for pending messages
4. Acknowledge receipt of messages

Example:
    from rover_connector import RoverAPIClient, RoverConfig
    
    config = RoverConfig(
        username="your_username",
        password="your_password",
        certificate_path="/path/to/cert.pfx",
        certificate_password="cert_password"
    )
    
    client = RoverAPIClient(config)
    client.run()
"""

from .models import RoverConfig, HL7Message, SessionState
from .client import RoverAPIClient

__version__ = "1.0.0"
__author__ = "Your Organization"
__all__ = ["RoverAPIClient", "RoverConfig", "HL7Message", "SessionState"]
