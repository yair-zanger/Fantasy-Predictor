"""
Yahoo OAuth 2.0 Authentication Handler with PKCE Support
"""
import os
import json
import time
import webbrowser
import requests
import hashlib
import base64
import secrets
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import ssl
import socket

from config import YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, REDIRECT_URI, IS_VERCEL

# OAuth endpoints
AUTH_URL = 'https://api.login.yahoo.com/oauth2/request_auth'
TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'

TOKEN_FILE = 'yahoo_token.json'


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback"""
    auth_code = None
    
    def do_GET(self):
        """Handle GET request from OAuth callback"""
        query = parse_qs(urlparse(self.path).query)
        
        if 'code' in query:
            OAuthCallbackHandler.auth_code = query['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            response = """
            <html dir="rtl">
            <head><title>התחברות הצליחה!</title></head>
            <body style="font-family: Arial; text-align: center; padding: 50px; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white; min-height: 100vh;">
                <h1>✅ ההתחברות הצליחה!</h1>
                <p>אפשר לסגור את החלון הזה ולחזור לאפליקציה.</p>
            </body>
            </html>
            """
            self.wfile.write(response.encode())
        else:
            self.send_response(400)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            error = query.get('error', ['Unknown error'])[0]
            response = f"""
            <html dir="rtl">
            <head><title>שגיאה</title></head>
            <body style="font-family: Arial; text-align: center; padding: 50px; background: #ff6b6b; color: white;">
                <h1>❌ שגיאה בהתחברות</h1>
                <p>{error}</p>
            </body>
            </html>
            """
            self.wfile.write(response.encode())
    
    def log_message(self, format, *args):
        """Suppress logging"""
        pass


class YahooAuth:
    """Yahoo OAuth 2.0 Authentication with PKCE"""
    
    def __init__(self):
        self.client_id = YAHOO_CLIENT_ID
        self.client_secret = YAHOO_CLIENT_SECRET
        self.redirect_uri = REDIRECT_URI
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = None
        self.code_verifier = None  # For PKCE
        
        # Try to load existing token
        self.load_token()
    
    def _generate_code_verifier(self):
        """Generate a random code verifier for PKCE"""
        # Generate 32 random bytes and encode as base64
        return secrets.token_urlsafe(32)
    
    def _generate_code_challenge(self, verifier):
        """Generate code challenge from verifier using SHA256"""
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    
    def get_auth_url(self):
        """Generate OAuth authorization URL with PKCE"""
        # Generate PKCE codes
        self.code_verifier = self._generate_code_verifier()
        code_challenge = self._generate_code_challenge(self.code_verifier)
        
        # Save code_verifier to session so it survives across serverless invocations
        try:
            from flask import session
            session['pkce_code_verifier'] = self.code_verifier
            session.modified = True
        except RuntimeError:
            pass  # No Flask request context (CLI mode)
        
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'fspt-r',  # Fantasy Sports Read
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
        }
        return f"{AUTH_URL}?{urlencode(params)}"
    
    def start_local_server(self, port=8080):
        """Start local server to receive OAuth callback"""
        server = HTTPServer(('localhost', port), OAuthCallbackHandler)
        
        # Create SSL context for HTTPS
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        
        # Generate self-signed certificate
        cert_file = 'localhost.pem'
        key_file = 'localhost-key.pem'
        
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
            self._generate_self_signed_cert(cert_file, key_file)
        
        context.load_cert_chain(cert_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        
        return server
    
    def _generate_self_signed_cert(self, cert_file, key_file):
        """Generate self-signed certificate for localhost"""
        try:
            from OpenSSL import crypto
            
            # Create key pair
            key = crypto.PKey()
            key.generate_key(crypto.TYPE_RSA, 2048)
            
            # Create certificate
            cert = crypto.X509()
            cert.get_subject().CN = 'localhost'
            cert.set_serial_number(1000)
            cert.gmtime_adj_notBefore(0)
            cert.gmtime_adj_notAfter(365 * 24 * 60 * 60)  # 1 year
            cert.set_issuer(cert.get_subject())
            cert.set_pubkey(key)
            cert.sign(key, 'sha256')
            
            # Save certificate and key
            with open(cert_file, 'wb') as f:
                f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
            with open(key_file, 'wb') as f:
                f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))
                
        except ImportError:
            # If pyOpenSSL not available, use openssl command
            import subprocess
            subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                '-keyout', key_file, '-out', cert_file,
                '-days', '365', '-nodes',
                '-subj', '/CN=localhost'
            ], capture_output=True)
    
    def authenticate_interactive(self):
        """Interactive authentication flow"""
        print("\n" + "="*50)
        print("🔐 Yahoo Fantasy Authentication")
        print("="*50)
        
        auth_url = self.get_auth_url()
        print(f"\nפותח את הדפדפן להתחברות...")
        print(f"אם הדפדפן לא נפתח, לחץ על הקישור הבא:\n{auth_url}\n")
        
        # Start local server in background
        OAuthCallbackHandler.auth_code = None
        
        try:
            server = self.start_local_server()
            server_thread = threading.Thread(target=server.handle_request)
            server_thread.daemon = True
            server_thread.start()
            
            # Open browser
            webbrowser.open(auth_url)
            
            # Wait for callback
            print("ממתין להתחברות...")
            server_thread.join(timeout=120)  # 2 minute timeout
            
            if OAuthCallbackHandler.auth_code:
                self.exchange_code_for_token(OAuthCallbackHandler.auth_code)
                print("✅ ההתחברות הצליחה!")
                return True
            else:
                print("❌ פג תוקף ההמתנה להתחברות")
                return False
                
        except Exception as e:
            print(f"\n⚠️ לא ניתן להפעיל שרת מקומי: {e}")
            print("\nנא להתחבר ידנית:")
            print(f"1. לך ל: {auth_url}")
            print("2. אחרי ההתחברות, העתק את הקוד מה-URL")
            
            code = input("\nהכנס את הקוד: ").strip()
            if code:
                self.exchange_code_for_token(code)
                print("✅ ההתחברות הצליחה!")
                return True
            return False
    
    def exchange_code_for_token(self, code):
        """Exchange authorization code for access token with PKCE"""
        # Restore code_verifier from session if it was lost (serverless)
        if not self.code_verifier:
            try:
                from flask import session
                self.code_verifier = session.get('pkce_code_verifier')
            except RuntimeError:
                pass
        
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.redirect_uri,
            'client_id': self.client_id,
        }
        
        # Add PKCE code_verifier
        if self.code_verifier:
            data['code_verifier'] = self.code_verifier
        
        if self.client_secret:
            data['client_secret'] = self.client_secret
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        
        response = requests.post(TOKEN_URL, data=data, headers=headers)
        
        if response.status_code == 200:
            token_data = response.json()
            self.access_token = token_data['access_token']
            self.refresh_token = token_data.get('refresh_token')
            self.token_expiry = time.time() + token_data.get('expires_in', 3600)
            self.save_token()
            self.code_verifier = None  # Clear after use
            # Also clear from session
            try:
                from flask import session
                session.pop('pkce_code_verifier', None)
            except RuntimeError:
                pass
        else:
            raise Exception(f"Failed to get token: {response.text}")
    
    def refresh_access_token(self):
        """Refresh the access token"""
        if not self.refresh_token:
            return False
        
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
        }
        
        if self.client_secret:
            data['client_secret'] = self.client_secret
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        
        response = requests.post(TOKEN_URL, data=data, headers=headers)
        
        if response.status_code == 200:
            token_data = response.json()
            self.access_token = token_data['access_token']
            self.refresh_token = token_data.get('refresh_token', self.refresh_token)
            self.token_expiry = time.time() + token_data.get('expires_in', 3600)
            self.save_token()
            return True
        else:
            return False
    
    def get_valid_token(self):
        """Get a valid access token, refreshing if necessary"""
        # Ensure we always have the latest token from the current session/file
        self.load_token()
        
        if self.access_token and self.token_expiry:
            # Check if token is about to expire (within 5 minutes)
            if time.time() < self.token_expiry - 300:
                return self.access_token
            
            # Try to refresh
            if self.refresh_access_token():
                return self.access_token
        
        # Need to re-authenticate
        if self.authenticate_interactive():
            return self.access_token
        
        return None
    
    def save_token(self):
        """Save token - uses Flask session on Vercel, file on local."""
        token_data = {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'token_expiry': self.token_expiry
        }
        # Always try Flask session first (works in both environments)
        try:
            from flask import session
            session['yahoo_token'] = token_data
            session.modified = True
        except RuntimeError:
            pass  # No request context (e.g. CLI mode)
        
        # Also save to file if not on Vercel (for local persistence across restarts)
        if not IS_VERCEL:
            try:
                with open(TOKEN_FILE, 'w') as f:
                    json.dump(token_data, f)
            except Exception:
                pass
    
    def load_token(self):
        """Load token - prefers Flask session, falls back to file on local."""
        # Try Flask session first
        try:
            from flask import session
            token_data = session.get('yahoo_token')
            if token_data:
                self.access_token = token_data.get('access_token')
                self.refresh_token = token_data.get('refresh_token')
                self.token_expiry = token_data.get('token_expiry')
                return
        except RuntimeError:
            pass  # No request context
        
        # Fall back to file (local dev only)
        if not IS_VERCEL and os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, 'r') as f:
                    token_data = json.load(f)
                    self.access_token = token_data.get('access_token')
                    self.refresh_token = token_data.get('refresh_token')
                    self.token_expiry = token_data.get('token_expiry')
            except Exception:
                pass
    
    def is_authenticated(self):
        """Check if user is authenticated"""
        # Always reload from session to pick up the current request's token
        self.load_token()
        return self.access_token is not None


# Singleton instance
auth = YahooAuth()
