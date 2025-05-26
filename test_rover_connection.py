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

"""Unit tests for Rover Connector.

This module contains unit tests for the main components of the Rover Connector.
It uses Python's unittest framework and mock objects to test functionality
without making actual API calls.

To run tests:
    python -m unittest test_rover_connector.py
    
Or with pytest:
    pytest test_rover_connector.py -v
"""

import unittest
from unittest.mock import Mock, MagicMock, patch, mock_open, call
from datetime import datetime, timedelta
import tempfile
import os
import xml.etree.ElementTree as ET

# Import modules to test
from rover_connector.models import (
    HL7Message, SessionState, RoverConfig, 
    AuthenticationResult, Environment, Province,
    AUTH_GRANTED_RESPONSE, AUTH_DENIED_RESPONSE,
    EMPTY_HL7_RESPONSE, ACK_ERROR_RESPONSE
)
from rover_connector.handlers import (
    ResponseParser, URLStrategyFactory, OntarioURLStrategy,
    BCURLStrategy, HTTPClient, CertificateManager,
    AuthenticationHandler, MessageProcessor
)
from rover_connector.client import RoverAPIClient, load_config


class TestModels(unittest.TestCase):
    """Test data models and structures."""
    
    def test_hl7_message_escape_processing(self):
        """Test HL7 escape sequence processing."""
        message = HL7Message(
            id="12345",
            content="MSH\\F\\^~\\\\&\\F\\TEST\\R\\DATA\\S\\END",
            version="2.3.1"
        )
        
        # Process escape sequences
        processed = message.to_file_content()
        
        # Check that escapes were replaced
        self.assertIn("|", processed)  # \F\ -> |
        self.assertIn("~", processed)  # \R\ -> ~
        self.assertIn("^", processed)  # \S\ -> ^
        self.assertNotIn("\\F\\", processed)
        self.assertNotIn("\\R\\", processed)
    
    def test_session_state_expiry(self):
        """Test session expiry logic."""
        state = SessionState()
        
        # New session should be expired
        self.assertTrue(state.is_expired(900))
        
        # Authenticated session should not be expired immediately
        state.authenticated = True
        state.last_activity = datetime.now()
        self.assertFalse(state.is_expired(900))
        
        # Session should expire after timeout
        state.last_activity = datetime.now() - timedelta(seconds=1000)
        self.assertTrue(state.is_expired(900))
    
    def test_session_state_reset(self):
        """Test session reset functionality."""
        state = SessionState(
            authenticated=True,
            auth_time=datetime.now(),
            last_activity=datetime.now()
        )
        
        state.reset()
        
        self.assertFalse(state.authenticated)
        self.assertIsNone(state.auth_time)
        self.assertIsNone(state.last_activity)
    
    def test_rover_config_url_initialization(self):
        """Test automatic URL configuration based on province/environment."""
        # Ontario test environment
        config = RoverConfig(
            username="test",
            password="test",
            certificate_path="/test.pfx",
            certificate_password="test",
            province="ON",
            environment="test"
        )
        
        self.assertEqual(config.base_url, "https://api.ontest.excelleris.com/")
        self.assertEqual(config.api_url, "https://api.ontest.excelleris.com/hl7pull.aspx")
        
        # BC production environment
        config_bc = RoverConfig(
            username="test",
            password="test",
            certificate_path="/test.pfx",
            certificate_password="test",
            province="BC",
            environment="production"
        )
        
        self.assertEqual(config_bc.base_url, "https://api.bc.excelleris.com/")
        self.assertEqual(config_bc.api_url, "https://api.bc.excelleris.com/hl7pull.aspx")


class TestHandlers(unittest.TestCase):
    """Test business logic handlers."""
    
    def test_response_parser_auth(self):
        """Test authentication response parsing."""
        # Test granted response
        result = ResponseParser.parse_auth(AUTH_GRANTED_RESPONSE)
        self.assertEqual(result, AuthenticationResult.GRANTED)
        
        # Test denied response
        result = ResponseParser.parse_auth(AUTH_DENIED_RESPONSE)
        self.assertEqual(result, AuthenticationResult.DENIED)
        
        # Test error response
        result = ResponseParser.parse_auth("<Error>Something went wrong</Error>")
        self.assertEqual(result, AuthenticationResult.ERROR)
    
    def test_response_parser_messages(self):
        """Test message response parsing."""
        # Test valid message response
        xml_response = '''<HL7Messages Version="2.3.1">
            <Message MsgID="12345">MSH|^~\\&amp;|TEST</Message>
            <Message MsgID="12346">MSH|^~\\&amp;|TEST2</Message>
        </HL7Messages>'''
        
        messages = ResponseParser.parse_messages(xml_response)
        
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].id, "12345")
        self.assertEqual(messages[0].content, "MSH|^~\\&|TEST")
        self.assertEqual(messages[0].version, "2.3.1")
        
        # Test empty response
        messages = ResponseParser.parse_messages(EMPTY_HL7_RESPONSE)
        self.assertEqual(len(messages), 0)
        
        # Test malformed XML
        messages = ResponseParser.parse_messages("Not XML")
        self.assertEqual(len(messages), 0)
    
    def test_response_parser_acknowledgment(self):
        """Test acknowledgment response parsing."""
        # Test success
        self.assertTrue(ResponseParser.parse_acknowledgment(EMPTY_HL7_RESPONSE))
        
        # Test error
        self.assertFalse(ResponseParser.parse_acknowledgment(ACK_ERROR_RESPONSE))
    
    def test_url_strategy_factory(self):
        """Test URL strategy factory."""
        # Test Ontario strategy
        strategy = URLStrategyFactory.create(Province.ON)
        self.assertIsInstance(strategy, OntarioURLStrategy)
        
        # Test BC strategy
        strategy = URLStrategyFactory.create(Province.BC)
        self.assertIsInstance(strategy, BCURLStrategy)
    
    @patch('rover_connector.handlers.requests.Session')
    def test_http_client_retry(self, mock_session):
        """Test HTTP client retry logic."""
        # Create mock certificate manager
        cert_manager = Mock()
        cert_manager.get_ssl_context.return_value = None
        cert_manager.ca_cert_path = None
        
        # Create HTTP client
        logger = Mock()
        http_client = HTTPClient(mock_session, cert_manager, logger)
        
        # Test successful request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_session.get.return_value = mock_response
        
        response = http_client.get("https://test.com")
        self.assertEqual(response, mock_response)
        
        # Test retry on failure
        mock_session.get.side_effect = [
            Exception("Network error"),
            Exception("Network error"),
            mock_response
        ]
        
        response = http_client.get("https://test.com")
        self.assertEqual(response, mock_response)
        self.assertEqual(mock_session.get.call_count, 4)  # 1 success + 3 retries
    
    def test_message_processor_save(self):
        """Test message saving functionality."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create config with temp directory
            config = RoverConfig(
                username="test",
                password="test",
                certificate_path="/test.pfx",
                certificate_password="test",
                incoming_dir=temp_dir
            )
            
            # Create processor
            logger = Mock()
            processor = MessageProcessor(config, logger)
            
            # Create test message
            message = HL7Message(
                id="12345",
                content="MSH|^~\\&|TEST",
                version="2.3.1"
            )
            
            # Save message
            processor.save_message(message)
            
            # Check file was created
            files = os.listdir(temp_dir)
            self.assertEqual(len(files), 1)
            self.assertIn("HL7_", files[0])
            self.assertIn("12345", files[0])
            
            # Check file content
            with open(os.path.join(temp_dir, files[0]), 'r') as f:
                content = f.read()
                self.assertEqual(content, "MSH|^~\\&|TEST")


class TestClient(unittest.TestCase):
    """Test main client functionality."""
    
    @patch('rover_connector.client.CertificateManager')
    @patch('rover_connector.client.HTTPClient')
    @patch('rover_connector.client.AuthenticationHandler')
    @patch('rover_connector.client.MessageProcessor')
    def test_client_initialization(self, mock_processor, mock_auth, mock_http, mock_cert):
        """Test client initialization."""
        config = RoverConfig(
            username="test",
            password="test",
            certificate_path="/test.pfx",
            certificate_password="test"
        )
        
        client = RoverAPIClient(config)
        
        # Check all components were initialized
        self.assertIsNotNone(client.config)
        self.assertIsNotNone(client.logger)
        mock_cert.assert_called_once()
        mock_auth.assert_called_once()
        mock_processor.assert_called_once()
    
    @patch('rover_connector.client.CertificateManager')
    @patch('rover_connector.client.AuthenticationHandler')
    def test_authenticate(self, mock_auth_handler, mock_cert_manager):
        """Test authentication flow."""
        config = RoverConfig(
            username="test",
            password="test",
            certificate_path="/test.pfx",
            certificate_password="test"
        )
        
        # Setup mocks
        mock_cert_instance = Mock()
        mock_cert_instance.load_certificates.return_value = True
        mock_cert_manager.return_value = mock_cert_instance
        
        mock_auth_instance = Mock()
        mock_auth_instance.authenticate.return_value = True
        mock_auth_handler.return_value = mock_auth_instance
        
        client = RoverAPIClient(config)
        
        # Test successful authentication
        result = client.authenticate()
        self.assertTrue(result)
        mock_cert_instance.load_certificates.assert_called_once()
        mock_auth_instance.authenticate.assert_called_once()
        
        # Test failed certificate loading
        mock_cert_instance.load_certificates.return_value = False
        result = client.authenticate()
        self.assertFalse(result)
    
    @patch('rover_connector.client.CertificateManager')
    @patch('rover_connector.client.HTTPClient')
    def test_query_messages(self, mock_http_client, mock_cert_manager):
        """Test message querying."""
        config = RoverConfig(
            username="test",
            password="test",
            certificate_path="/test.pfx",
            certificate_password="test"
        )
        
        # Setup mocks
        mock_http_instance = Mock()
        mock_response = Mock()
        mock_response.text = '''<HL7Messages Version="2.3.1">
            <Message MsgID="12345">MSH|^~\\&amp;|TEST</Message>
        </HL7Messages>'''
        mock_http_instance.post.return_value = mock_response
        mock_http_client.return_value = mock_http_instance
        
        client = RoverAPIClient(config)
        client.session_state.authenticated = True
        client.session_state.last_activity = datetime.now()
        
        # Query messages
        messages = client.query_messages()
        
        self.assertIsNotNone(messages)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, "12345")
        
        # Check correct API call was made
        mock_http_instance.post.assert_called_with(
            config.api_url,
            data={'Page': 'HL7', 'Query': 'NewRequests', 'Pending': 'Yes'}
        )
    
    def test_load_config_from_env(self):
        """Test configuration loading from environment variables."""
        # Set environment variables
        env_vars = {
            'ROVER_USERNAME': 'env_user',
            'ROVER_PASSWORD': 'env_pass',
            'ROVER_CERT_PATH': '/env/cert.pfx',
            'ROVER_CERT_PASSWORD': 'env_cert_pass',
            'ROVER_ENVIRONMENT': 'production',
            'ROVER_PROVINCE': 'BC',
            'ROVER_POLL_INTERVAL': '3600'
        }
        
        with patch.dict(os.environ, env_vars):
            config = load_config()
            
            self.assertEqual(config.username, 'env_user')
            self.assertEqual(config.password, 'env_pass')
            self.assertEqual(config.certificate_path, '/env/cert.pfx')
            self.assertEqual(config.certificate_password, 'env_cert_pass')
            self.assertEqual(config.environment, 'production')
            self.assertEqual(config.province, 'BC')
            self.assertEqual(config.poll_interval, 3600)
    
    def test_load_config_missing_required(self):
        """Test configuration validation."""
        # No environment variables or config file
        with self.assertRaises(ValueError) as context:
            load_config('/nonexistent/config.yaml')
        
        self.assertIn('Missing required configuration fields', str(context.exception))


class TestIntegration(unittest.TestCase):
    """Integration tests for component interaction."""
    
    @patch('rover_connector.handlers.requests.Session')
    def test_full_authentication_flow(self, mock_session):
        """Test complete authentication flow."""
        # Setup config
        config = RoverConfig(
            username="test_user",
            password="test_pass",
            certificate_path="/test.pfx",
            certificate_password="cert_pass"
        )
        
        # Mock certificate loading
        with patch('rover_connector.handlers.pkcs12.load_key_and_certificates') as mock_load:
            # Mock certificate data
            mock_key = Mock()
            mock_key.private_bytes.return_value = b'PRIVATE KEY'
            
            mock_cert = Mock()
            mock_cert.public_bytes.return_value = b'CERTIFICATE'
            mock_cert.not_valid_after = datetime.now() + timedelta(days=30)
            
            mock_load.return_value = (mock_key, mock_cert, [])
            
            # Mock HTTP responses
            mock_response1 = Mock()
            mock_response1.cookies = {'session': 'abc123'}
            
            mock_response2 = Mock()
            mock_response2.text = AUTH_GRANTED_RESPONSE
            
            mock_session.return_value.get.return_value = mock_response1
            mock_session.return_value.post.return_value = mock_response2
            
            # Create components
            logger = Mock()
            cert_manager = CertificateManager(config, logger)
            session_state = SessionState()
            http_client = HTTPClient(mock_session(), cert_manager, logger)
            auth_handler = AuthenticationHandler(config, http_client, session_state, logger)
            
            # Load certificates
            self.assertTrue(cert_manager.load_certificates())
            
            # Authenticate
            result = auth_handler.authenticate()
            
            self.assertTrue(result)
            self.assertTrue(session_state.authenticated)
            self.assertIsNotNone(session_state.auth_time)


if __name__ == '__main__':
    unittest.main()
