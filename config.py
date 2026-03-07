"""
Configuration for Yahoo Fantasy Basketball Predictor
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Debug mode - set to True only when debugging (massive performance impact!)
DEBUG_MODE = False

# Vercel serverless detection
IS_VERCEL = os.getenv('VERCEL') == '1'

# Yahoo OAuth Credentials
YAHOO_CLIENT_ID = os.getenv('YAHOO_CLIENT_ID', 'dj0yJmk9aVU1SmE5WE56NW5NJmQ9WVdrOWN6ZHlTRTl6TWxrbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PTRi')
YAHOO_CLIENT_SECRET = os.getenv('YAHOO_CLIENT_SECRET', '')  # Empty for Public Client

# Yahoo API URLs
YAHOO_AUTH_URL = 'https://api.login.yahoo.com/oauth2/request_auth'
YAHOO_TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'
YAHOO_FANTASY_API_URL = 'https://fantasysports.yahooapis.com/fantasy/v2'

# Redirect URI (must match what's in Yahoo Developer Console)
# On Vercel: set REDIRECT_URI env var to override, or update the default here
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://fantasy-predictor-eta.vercel.app/auth/callback')

# Fantasy Basketball Categories (9-CAT)
CATEGORIES = [
    'FG%',   # Field Goal Percentage
    'FT%',   # Free Throw Percentage
    '3PTM',  # 3-Pointers Made
    'PTS',   # Points
    'REB',   # Rebounds
    'AST',   # Assists
    'STL',   # Steals
    'BLK',   # Blocks
    'TO'     # Turnovers (lower is better)
]

# Categories where lower is better
NEGATIVE_CATEGORIES = ['TO']

# NBA Season
NBA_SEASON = '2025-26'
GAME_CODE = 'nba'

# Admin users (by Yahoo email) — have access to /admin panel
ADMIN_EMAILS = ['yairzanger@gmail.com', 'yoelzanger@gmail.com']
ADMIN_NICKNAMES = ['Yair abdul gerald', 'Yoel']

# ─── Stripe (set these in Vercel Environment Variables) ───────────────────────
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRICE_ID = os.getenv('STRIPE_PRICE_ID', '')  # Your Stripe Price ID

