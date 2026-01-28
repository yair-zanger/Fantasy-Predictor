"""
Configuration for Yahoo Fantasy Basketball Predictor
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Yahoo OAuth Credentials
YAHOO_CLIENT_ID = os.getenv('YAHOO_CLIENT_ID', 'dj0yJmk9aVU1SmE5WE56NW5NJmQ9WVdrOWN6ZHlTRTl6TWxrbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PTRi')
YAHOO_CLIENT_SECRET = os.getenv('YAHOO_CLIENT_SECRET', '')  # Empty for Public Client

# Yahoo API URLs
YAHOO_AUTH_URL = 'https://api.login.yahoo.com/oauth2/request_auth'
YAHOO_TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'
YAHOO_FANTASY_API_URL = 'https://fantasysports.yahooapis.com/fantasy/v2'

# Redirect URI (must match what's in Yahoo Developer Console)
REDIRECT_URI = 'https://localhost:5000/auth/callback'

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
