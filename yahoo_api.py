"""
Yahoo Fantasy API Wrapper
"""
import requests
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any
from yahoo_auth import auth
from config import YAHOO_FANTASY_API_URL, CATEGORIES

# XML Namespace
NS = {'yh': 'http://fantasysports.yahooapis.com/fantasy/v2/base.rng'}


class YahooFantasyAPI:
    """Yahoo Fantasy Sports API Client"""
    
    def __init__(self):
        self.base_url = YAHOO_FANTASY_API_URL
        self.auth = auth
    
    def _make_request(self, endpoint: str, params: Dict = None) -> Optional[ET.Element]:
        """Make authenticated request to Yahoo Fantasy API"""
        token = self.auth.get_valid_token()
        if not token:
            raise Exception("Not authenticated. Please authenticate first.")
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/xml'
        }
        
        url = f"{self.base_url}/{endpoint}"
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            return ET.fromstring(response.content)
        elif response.status_code == 401:
            # Token expired, try to refresh
            if self.auth.refresh_access_token():
                return self._make_request(endpoint, params)
            raise Exception("Authentication failed. Please re-authenticate.")
        else:
            raise Exception(f"API request failed: {response.status_code} - {response.text}")
    
    def get_user_leagues(self, game_key: str = 'nba') -> List[Dict]:
        """Get all leagues for the current user"""
        root = self._make_request(f"users;use_login=1/games;game_keys={game_key}/leagues")
        
        leagues = []
        for league in root.findall('.//yh:league', NS):
            league_data = {
                'league_key': self._get_text(league, 'yh:league_key'),
                'league_id': self._get_text(league, 'yh:league_id'),
                'name': self._get_text(league, 'yh:name'),
                'num_teams': self._get_text(league, 'yh:num_teams'),
                'current_week': self._get_text(league, 'yh:current_week'),
                'start_week': self._get_text(league, 'yh:start_week'),
                'end_week': self._get_text(league, 'yh:end_week'),
            }
            leagues.append(league_data)
        
        return leagues
    
    def get_league_settings(self, league_key: str) -> Dict:
        """Get league settings including scoring categories"""
        root = self._make_request(f"league/{league_key}/settings")
        
        settings = {
            'league_key': league_key,
            'stat_categories': []
        }
        
        for stat in root.findall('.//yh:stat', NS):
            stat_data = {
                'stat_id': self._get_text(stat, 'yh:stat_id'),
                'name': self._get_text(stat, 'yh:name'),
                'display_name': self._get_text(stat, 'yh:display_name'),
                'enabled': self._get_text(stat, 'yh:enabled') == '1',
                'is_only_display_stat': self._get_text(stat, 'yh:is_only_display_stat') == '1'
            }
            if stat_data['enabled'] and not stat_data['is_only_display_stat']:
                settings['stat_categories'].append(stat_data)
        
        return settings
    
    def get_my_team(self, league_key: str) -> Dict:
        """Get the current user's team in a league"""
        root = self._make_request(f"users;use_login=1/games/leagues;league_keys={league_key}/teams")
        
        team = root.find('.//yh:team', NS)
        if team is None:
            return None
        
        return {
            'team_key': self._get_text(team, 'yh:team_key'),
            'team_id': self._get_text(team, 'yh:team_id'),
            'name': self._get_text(team, 'yh:name'),
            'manager': self._get_text(team, './/yh:manager/yh:nickname'),
        }
    
    def get_team_roster(self, team_key: str, week: int = None) -> List[Dict]:
        """Get roster for a team with player stats"""
        # First get the roster
        endpoint = f"team/{team_key}/roster/players"
        
        root = self._make_request(endpoint)
        
        players = []
        player_keys = []
        
        for player in root.findall('.//yh:player', NS):
            player_key = self._get_text(player, 'yh:player_key')
            player_keys.append(player_key)
            
            # Try multiple paths for player name
            name = None
            for name_path in ['.//yh:full', 'yh:name/yh:full', './/yh:name/yh:full']:
                name = self._get_text(player, name_path)
                if name:
                    break
            
            # Get the roster slot position (IL, IL+, BN, etc.)
            roster_position = self._get_text(player, './/yh:selected_position/yh:position') or ''
            
            player_data = {
                'player_key': player_key,
                'player_id': self._get_text(player, 'yh:player_id'),
                'name': name or 'Unknown Player',
                'team': self._get_text(player, 'yh:editorial_team_abbr') or '',
                'position': self._get_text(player, 'yh:display_position') or '',
                'roster_position': roster_position,  # The slot in fantasy roster (IL, IL+, BN, etc.)
                'status': self._get_text(player, 'yh:status') or '',
                'injury_note': self._get_text(player, 'yh:injury_note') or '',
                'stats': {},
                'is_on_il': roster_position in ['IL', 'IL+'],  # Flag for easy checking
            }
            
            players.append(player_data)
        
        # Now get stats for all players
        if player_keys:
            player_stats = self.get_player_stats_averages(player_keys)
            for player in players:
                if player['player_key'] in player_stats:
                    player['stats'] = player_stats[player['player_key']]
        
        return players
    
    def get_matchup(self, team_key: str, week: int = None) -> Dict:
        """Get current matchup for a team"""
        endpoint = f"team/{team_key}/matchups"
        if week:
            endpoint += f";weeks={week}"
        
        root = self._make_request(endpoint)
        
        matchup = root.find('.//yh:matchup', NS)
        if matchup is None:
            return None
        
        teams_data = []
        for team in matchup.findall('.//yh:team', NS):
            team_data = {
                'team_key': self._get_text(team, 'yh:team_key'),
                'team_id': self._get_text(team, 'yh:team_id'),
                'name': self._get_text(team, 'yh:name'),
                'stats': {}
            }
            
            for stat in team.findall('.//yh:stat', NS):
                stat_id = self._get_text(stat, 'yh:stat_id')
                value = self._get_text(stat, 'yh:value')
                team_data['stats'][stat_id] = self._parse_stat_value(value)
            
            teams_data.append(team_data)
        
        # Determine which is my team and which is opponent
        result = {
            'week': self._get_text(matchup, 'yh:week'),
            'my_team': None,
            'opponent': None
        }
        
        for team_data in teams_data:
            if team_data['team_key'] == team_key:
                result['my_team'] = team_data
            else:
                result['opponent'] = team_data
        
        return result
    
    def get_player_stats_averages(self, player_keys: List[str]) -> Dict[str, Dict]:
        """Get season stats for multiple players"""
        if not player_keys:
            return {}
        
        print(f"[DEBUG] Getting stats for {len(player_keys)} players...")
        
        # Yahoo API allows max 25 players per request
        results = {}
        for i in range(0, len(player_keys), 25):
            batch = player_keys[i:i+25]
            keys_str = ','.join(batch)
            
            try:
                # Try to get season stats
                print(f"[DEBUG] Trying: players;player_keys=.../stats;type=season")
                root = self._make_request(f"players;player_keys={keys_str}/stats;type=season")
                
                players_found = root.findall('.//yh:player', NS)
                print(f"[DEBUG] Found {len(players_found)} players in response")
                
                for player in players_found:
                    player_key = self._get_text(player, 'yh:player_key')
                    stats = {}
                    
                    stat_elements = player.findall('.//yh:stat', NS)
                    print(f"[DEBUG] Player {player_key}: found {len(stat_elements)} stat elements")
                    
                    for stat in stat_elements:
                        stat_id = self._get_text(stat, 'yh:stat_id')
                        value = self._get_text(stat, 'yh:value')
                        stats[stat_id] = self._parse_stat_value(value)
                    
                    if stats:
                        results[player_key] = stats
                        print(f"[DEBUG] Player {player_key} stats: {list(stats.keys())[:5]}...")
                        
            except Exception as e:
                print(f"[DEBUG] Error with type=season: {e}")
                # Try without type parameter
                try:
                    print(f"[DEBUG] Trying: players;player_keys=.../stats (no type)")
                    root = self._make_request(f"players;player_keys={keys_str}/stats")
                    
                    for player in root.findall('.//yh:player', NS):
                        player_key = self._get_text(player, 'yh:player_key')
                        stats = {}
                        
                        for stat in player.findall('.//yh:stat', NS):
                            stat_id = self._get_text(stat, 'yh:stat_id')
                            value = self._get_text(stat, 'yh:value')
                            stats[stat_id] = self._parse_stat_value(value)
                        
                        if stats:
                            results[player_key] = stats
                except Exception as e2:
                    print(f"[DEBUG] Error without type: {e2}")
        
        print(f"[DEBUG] Total players with stats: {len(results)}")
        return results
    
    def get_player_stats_last30(self, player_keys: List[str]) -> Dict[str, Dict]:
        """Get last 30 days stats for multiple players (per-game averages).
        Falls back to season averages if last 30 not available.
        """
        if not player_keys:
            return {}
        
        print(f"[DEBUG] Getting last 30 days stats for {len(player_keys)} players...")
        
        results = {}
        for i in range(0, len(player_keys), 25):
            batch = player_keys[i:i+25]
            keys_str = ','.join(batch)
            
            # Try different stat types for last 30 days
            stat_types_to_try = ['lastmonth', 'average']
            success = False
            
            for stat_type in stat_types_to_try:
                try:
                    print(f"[DEBUG] Trying: players;player_keys=.../stats;type={stat_type}")
                    root = self._make_request(f"players;player_keys={keys_str}/stats;type={stat_type}")
                    
                    players_found = root.findall('.//yh:player', NS)
                    print(f"[DEBUG] Found {len(players_found)} players in response for type={stat_type}")
                    
                    for player in players_found:
                        player_key = self._get_text(player, 'yh:player_key')
                        stats = {}
                        
                        stat_elements = player.findall('.//yh:stat', NS)
                        for stat in stat_elements:
                            stat_id = self._get_text(stat, 'yh:stat_id')
                            value = self._get_text(stat, 'yh:value')
                            stats[stat_id] = self._parse_stat_value(value)
                        
                        if stats:
                            # Mark as per-game average (already averaged)
                            stats['_is_average'] = True
                            results[player_key] = stats
                    
                    if results:
                        success = True
                        break
                        
                except Exception as e:
                    print(f"[DEBUG] Error with type={stat_type}: {e}")
                    continue
            
            # Fallback to season stats if needed
            if not success:
                print(f"[DEBUG] Falling back to season stats")
                season_stats = self.get_player_stats_averages(batch)
                for pk, stats in season_stats.items():
                    if pk not in results:
                        stats['_is_average'] = False  # Total stats, need to divide by GP
                        results[pk] = stats
        
        print(f"[DEBUG] Total players with last30 stats: {len(results)}")
        return results
    
    def get_opponent_roster(self, league_key: str, opponent_team_key: str, week: int = None) -> List[Dict]:
        """Get opponent's roster"""
        return self.get_team_roster(opponent_team_key, week)
    
    def get_scoreboard(self, league_key: str, week: int = None) -> List[Dict]:
        """Get league scoreboard for a week"""
        endpoint = f"league/{league_key}/scoreboard"
        if week:
            endpoint += f";week={week}"
        
        root = self._make_request(endpoint)
        
        matchups = []
        for matchup in root.findall('.//yh:matchup', NS):
            matchup_data = {
                'week': self._get_text(matchup, 'yh:week'),
                'teams': []
            }
            
            for team in matchup.findall('.//yh:team', NS):
                team_data = {
                    'team_key': self._get_text(team, 'yh:team_key'),
                    'name': self._get_text(team, 'yh:name'),
                    'stats': {},
                    'win_probability': self._get_text(team, 'yh:win_probability')
                }
                
                for stat in team.findall('.//yh:stat', NS):
                    stat_id = self._get_text(stat, 'yh:stat_id')
                    value = self._get_text(stat, 'yh:value')
                    team_data['stats'][stat_id] = self._parse_stat_value(value)
                
                matchup_data['teams'].append(team_data)
            
            matchups.append(matchup_data)
        
        return matchups
    
    def _get_text(self, element: ET.Element, path: str) -> Optional[str]:
        """Helper to get text from XML element"""
        found = element.find(path, NS)
        return found.text if found is not None else None
    
    def _parse_stat_value(self, value: str) -> float:
        """Parse stat value - handles fractions like '227/467' and regular numbers"""
        if not value or value == '-':
            return 0.0
        
        # Handle fractions (e.g., "227/467" for FGM/FGA)
        if '/' in value:
            try:
                parts = value.split('/')
                numerator = float(parts[0])
                denominator = float(parts[1])
                if denominator > 0:
                    return (numerator / denominator) * 100  # Return as percentage
                return 0.0
            except:
                return 0.0
        
        # Handle regular numbers
        try:
            return float(value)
        except:
            return 0.0


# Singleton instance
api = YahooFantasyAPI()


# Stat ID to Name mapping (Yahoo Fantasy Basketball)
STAT_ID_MAP = {
    '0': 'GP',      # Games Played
    '1': 'GS',      # Games Started  
    '2': 'MIN',     # Minutes
    '3': 'FGA',     # Field Goals Attempted
    '4': 'FGM',     # Field Goals Made
    '5': 'FG%',     # Field Goal Percentage
    '6': 'FTA',     # Free Throws Attempted
    '7': 'FTM',     # Free Throws Made
    '8': 'FT%',     # Free Throw Percentage
    '9': '3PTA',    # 3-Pointers Attempted
    '10': '3PTM',   # 3-Pointers Made
    '11': '3PT%',   # 3-Point Percentage
    '12': 'PTS',    # Points
    '13': 'OREB',   # Offensive Rebounds
    '14': 'DREB',   # Defensive Rebounds
    '15': 'REB',    # Total Rebounds
    '16': 'AST',    # Assists
    '17': 'STL',    # Steals
    '18': 'BLK',    # Blocks
    '19': 'TO',     # Turnovers
    '20': 'A/T',    # Assist/Turnover Ratio
    '21': 'PF',     # Personal Fouls
    '22': 'TECH',   # Technical Fouls
    '23': 'EJCT',   # Ejections
    '24': 'FF',     # Flagrant Fouls
    '25': 'MPG',    # Minutes Per Game
    '26': 'DD',     # Double-Doubles
    '27': 'TD',     # Triple-Doubles
}

# Reverse mapping
STAT_NAME_TO_ID = {v: k for k, v in STAT_ID_MAP.items()}
