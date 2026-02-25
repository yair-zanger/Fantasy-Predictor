"""
Yahoo Fantasy API Wrapper
"""
import requests
import xml.etree.ElementTree as ET
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from functools import wraps
from yahoo_auth import auth
from config import YAHOO_FANTASY_API_URL, CATEGORIES, DEBUG_MODE, IS_VERCEL

def debug_print(*args, **kwargs):
    """Print only if DEBUG_MODE is enabled."""
    if DEBUG_MODE:
        print(*args, **kwargs)

# XML Namespace
NS = {'yh': 'http://fantasysports.yahooapis.com/fantasy/v2/base.rng'}

# Cache file for disk persistence
CACHE_FILE = 'yahoo_api_cache.json'

# Cache settings (in seconds)
CACHE_TTL = {
    'leagues': 86400,    # 24 hours - leagues rarely change
    'settings': 86400,   # 24 hours - settings rarely change
    'team': 7200,        # 2 hours - team info changes occasionally
    'roster': 600,       # 10 minutes - perfectly aligns with background pre-warm
    'matchup': 3600,     # 1 hour - matchup stats update during games
    'scoreboard': 86400, # 24 hours - scoreboard (past weeks don't change)
    'standings': 600,    # 10 minutes - standings update frequently during the week
    'player_stats': 3600,# 1 hour - player stats are relatively stable
    'cat_records': 604800, # 7 days - category records for past weeks NEVER change
    'transactions': 600,   # 10 minutes - transactions can change
}

# In-memory cache: {key: {'data': ..., 'expires': datetime}}
_api_cache: Dict[str, Dict] = {}
_cache_loaded = False


def _load_cache_from_disk():
    """Load cache from disk file."""
    global _api_cache, _cache_loaded
    
    if _cache_loaded:
        return
    
    if not os.path.exists(CACHE_FILE):
        _cache_loaded = True
        return
    
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        now = datetime.now()
        loaded_count = 0
        
        for key, cached in data.items():
            # Parse expiry time
            expires = datetime.fromisoformat(cached['expires'])
            if expires > now:
                _api_cache[key] = {
                    'data': cached['data'],
                    'expires': expires
                }
                loaded_count += 1
        
        debug_print(f"[Yahoo API] Loaded {loaded_count} cached items from disk")
        _cache_loaded = True
        
    except Exception as e:
        debug_print(f"[Yahoo API] Error loading cache from disk: {e}")
        _cache_loaded = True


def _save_cache_to_disk():
    """Save cache to disk file (skipped on Vercel - read-only filesystem)."""
    if IS_VERCEL:
        return  # Vercel filesystem is read-only; in-memory cache is sufficient
    try:
        # Convert datetime to ISO format for JSON
        data = {}
        for key, cached in _api_cache.items():
            data[key] = {
                'data': cached['data'],
                'expires': cached['expires'].isoformat()
            }
        
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        
    except Exception as e:
        debug_print(f"[Yahoo API] Error saving cache to disk: {e}")


def _cache_key(endpoint: str, params: Dict = None) -> str:
    """Generate cache key from endpoint and params."""
    key = endpoint
    if params:
        key += '?' + '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
    return key


def _get_cached(key: str) -> Optional[Any]:
    """Get cached value if not expired."""
    # Ensure disk cache is loaded
    _load_cache_from_disk()
    
    if key in _api_cache:
        cached = _api_cache[key]
        if datetime.now() < cached['expires']:
            return cached['data']
        else:
            del _api_cache[key]
    return None


def _set_cached(key: str, data: Any, ttl_seconds: int):
    """Store value in cache with TTL."""
    _api_cache[key] = {
        'data': data,
        'expires': datetime.now() + timedelta(seconds=ttl_seconds)
    }
    # Save to disk after each update
    _save_cache_to_disk()


def clear_cache_by_pattern(pattern: str):
    """Clear cache entries that match the given pattern (e.g., 'roster:', 'transactions:').
    Useful for forcing a refresh of specific data types.
    """
    keys_to_delete = [key for key in _api_cache.keys() if pattern in key]
    for key in keys_to_delete:
        del _api_cache[key]
    
    # Save to disk after clearing
    _save_cache_to_disk()
    
    debug_print(f"[Yahoo API] Cleared {len(keys_to_delete)} cache entries matching '{pattern}'")
    return len(keys_to_delete)


def load_disk_cache():
    """Load cache from disk into memory (call at startup for faster first request)."""
    _load_cache_from_disk()


def clear_cache():
    """Clear all cached data."""
    global _api_cache
    _api_cache = {}
    # Also clear disk cache (skipped on Vercel)
    if not IS_VERCEL and os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    debug_print("[Yahoo API] Cache cleared")


class YahooFantasyAPI:
    """Yahoo Fantasy Sports API Client"""
    
    def __init__(self):
        self.base_url = YAHOO_FANTASY_API_URL
        self.auth = auth
        self.session = requests.Session()
    
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
        response = self.session.get(url, headers=headers, params=params)
        
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
        cache_key = f"leagues:{game_key}"
        cached = _get_cached(cache_key)
        if cached is not None:
            debug_print(f"[Yahoo API] Using cached leagues ({len(cached)} leagues)")
            return cached
        
        root = self._make_request(f"users;use_login=1/games;game_keys={game_key}/leagues;out=settings")
        
        leagues = []
        for league in root.findall('.//yh:league', NS):
            # Get playoff start week from settings (try multiple paths)
            playoff_start_week = (
                self._get_text(league, './/yh:playoff_start_week') or
                self._get_text(league, 'yh:settings/yh:playoff_start_week') or
                self._get_text(league, 'yh:playoff_start_week')
            )
            
            league_data = {
                'league_key': self._get_text(league, 'yh:league_key'),
                'league_id': self._get_text(league, 'yh:league_id'),
                'name': self._get_text(league, 'yh:name'),
                'num_teams': self._get_text(league, 'yh:num_teams'),
                'current_week': self._get_text(league, 'yh:current_week'),
                'start_week': self._get_text(league, 'yh:start_week'),
                'end_week': self._get_text(league, 'yh:end_week'),
                'playoff_start_week': playoff_start_week,
            }
            leagues.append(league_data)
        
        _set_cached(cache_key, leagues, CACHE_TTL['leagues'])
        return leagues
    
    def get_league_settings(self, league_key: str) -> Dict:
        """Get league settings including scoring categories"""
        cache_key = f"settings:{league_key}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
        
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
        
        _set_cached(cache_key, settings, CACHE_TTL['settings'])
        return settings
    
    def get_my_team(self, league_key: str) -> Dict:
        """Get the current user's team in a league"""
        cache_key = f"my_team:{league_key}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
        
        root = self._make_request(f"users;use_login=1/games/leagues;league_keys={league_key}/teams")
        
        team = root.find('.//yh:team', NS)
        if team is None:
            return None
        
        result = {
            'team_key': self._get_text(team, 'yh:team_key'),
            'team_id': self._get_text(team, 'yh:team_id'),
            'name': self._get_text(team, 'yh:name'),
            'manager': self._get_text(team, './/yh:manager/yh:nickname'),
        }
        
        _set_cached(cache_key, result, CACHE_TTL['team'])
        return result
    
    def get_team_roster(self, team_key: str, week: int = None) -> List[Dict]:
        """Get roster for a team with player stats"""
        cache_key = f"roster:{team_key}:{week or 'current'}"
        cached = _get_cached(cache_key)
        if cached is not None:
            debug_print(f"[Yahoo API] Using cached roster for {team_key}")
            return cached
        
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
        
        _set_cached(cache_key, players, CACHE_TTL['roster'])
        return players
    
    def get_team_stats(self, team_key: str, week: int = None) -> Dict:
        """Get team stats for a specific week (including Games Played)"""
        cache_key = f"team_stats:{team_key}:{week or 'current'}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
        
        # Request team stats for specific week
        endpoint = f"team/{team_key}/stats"
        if week:
            endpoint += f";type=week;week={week}"
        
        root = self._make_request(endpoint)
        
        stats = {}
        for stat in root.findall('.//yh:stat', NS):
            stat_id = self._get_text(stat, 'yh:stat_id')
            value = self._get_text(stat, 'yh:value')
            parsed = self._parse_stat_value(value)
            stats[stat_id] = parsed
            # Also store with integer key for easier access
            if stat_id is not None and stat_id.isdigit():
                stats[int(stat_id)] = parsed
        
        _set_cached(cache_key, stats, CACHE_TTL['matchup'])
        return stats
    
    def get_matchup(self, team_key: str, week: int = None) -> Dict:
        """Get current matchup for a team"""
        cache_key = f"matchup:{team_key}:{week or 'current'}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
        
        # Request matchup WITH team stats (including Games Played stat_id=0)
        endpoint = f"team/{team_key}/matchups"
        if week:
            endpoint += f";weeks={week}"
        # Add ;type=week to get weekly stats (including GP)
        endpoint += ";type=week"
        
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
                parsed = self._parse_stat_value(value)
                team_data['stats'][stat_id] = parsed
                if stat_id is not None and str(stat_id).strip() == '0':
                    team_data['stats'][0] = parsed
            teams_data.append(team_data)
        
        # Determine which is my team and which is opponent
        result = {
            'week': self._get_text(matchup, 'yh:week'),
            'week_start': self._get_text(matchup, 'yh:week_start'),
            'week_end': self._get_text(matchup, 'yh:week_end'),
            'my_team': None,
            'opponent': None
        }
        
        for team_data in teams_data:
            if team_data['team_key'] == team_key:
                result['my_team'] = team_data
            else:
                result['opponent'] = team_data
        
        _set_cached(cache_key, result, CACHE_TTL['matchup'])
        return result
    
    def get_league_scoreboard(self, league_key: str, week: int = None) -> List[Dict]:
        """Get all matchups in the league for a given week"""
        cache_key = f"scoreboard:{league_key}:{week or 'current'}"
        cached = _get_cached(cache_key)
        if cached is not None:
            debug_print(f"[Yahoo API] Using cached scoreboard for {league_key}")
            return cached
        
        # Request scoreboard WITH team stats (including Games Played stat_id=0)
        endpoint = f"league/{league_key}/scoreboard"
        if week:
            endpoint += f";week={week}"
        # Add ;type=week to get weekly stats (including GP)
        endpoint += ";type=week"
        
        root = self._make_request(endpoint)
        
        matchups = []
        for matchup in root.findall('.//yh:matchup', NS):
            matchup_data = {
                'week': self._get_text(matchup, 'yh:week'),
                'week_start': self._get_text(matchup, 'yh:week_start'),
                'week_end': self._get_text(matchup, 'yh:week_end'),
                'teams': []
            }
            
            for team in matchup.findall('.//yh:team', NS):
                team_data = {
                    'team_key': self._get_text(team, 'yh:team_key'),
                    'team_id': self._get_text(team, 'yh:team_id'),
                    'name': self._get_text(team, 'yh:name'),
                    'manager': self._get_text(team, './/yh:manager/yh:nickname'),
                    'stats': {}
                }
                
                # Get current stats for this team (ensure stat 0 = Games Played is also keyed as int)
                for stat in team.findall('.//yh:stat', NS):
                    stat_id = self._get_text(stat, 'yh:stat_id')
                    value = self._get_text(stat, 'yh:value')
                    parsed = self._parse_stat_value(value)
                    team_data['stats'][stat_id] = parsed
                    if stat_id is not None and str(stat_id).strip() == '0':
                        team_data['stats'][0] = parsed
                
                matchup_data['teams'].append(team_data)
            
            matchups.append(matchup_data)
        
        _set_cached(cache_key, matchups, CACHE_TTL['scoreboard'])
        return matchups
    
    def get_league_standings(self, league_key: str) -> List[Dict]:
        """Get league standings with team records"""
        cache_key = f"standings:{league_key}"
        cached = _get_cached(cache_key)
        if cached is not None:
            debug_print(f"[Yahoo API] Using cached standings for {league_key}")
            return cached
        
        endpoint = f"league/{league_key}/standings"
        
        root = self._make_request(endpoint)
        
        standings = []
        for team in root.findall('.//yh:team', NS):
            team_standings = team.find('.//yh:team_standings', NS)
            
            team_data = {
                'team_key': self._get_text(team, 'yh:team_key'),
                'team_id': self._get_text(team, 'yh:team_id'),
                'name': self._get_text(team, 'yh:name'),
                'manager': self._get_text(team, './/yh:manager/yh:nickname'),
                'rank': int(self._get_text(team_standings, 'yh:rank') or 0) if team_standings is not None else 0,
                'wins': int(self._get_text(team_standings, './/yh:wins') or 0) if team_standings is not None else 0,
                'losses': int(self._get_text(team_standings, './/yh:losses') or 0) if team_standings is not None else 0,
                'ties': int(self._get_text(team_standings, './/yh:ties') or 0) if team_standings is not None else 0,
                'points_for': float(self._get_text(team_standings, './/yh:points_for') or 0) if team_standings is not None else 0,
                'points_against': float(self._get_text(team_standings, './/yh:points_against') or 0) if team_standings is not None else 0,
            }
            
            # Calculate winning percentage
            total_games = team_data['wins'] + team_data['losses']
            team_data['win_pct'] = (team_data['wins'] / total_games * 100) if total_games > 0 else 0
            
            standings.append(team_data)
        
        # Sort by rank
        standings.sort(key=lambda x: x['rank'])
        
        _set_cached(cache_key, standings, CACHE_TTL['standings'])
        return standings
    
    def _get_league_transactions_xml(self, league_key: str) -> ET.Element:
        """Fetch and cache transactions XML for a league to avoid redundant API calls"""
        cache_key = f"raw_transactions:{league_key}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return ET.fromstring(cached)
            
        endpoint = f"league/{league_key}/transactions"
        root = self._make_request(endpoint)
        
        # Cache as string for the same TTL as transactions
        xml_str = ET.tostring(root, encoding='unicode')
        _set_cached(cache_key, xml_str, CACHE_TTL.get('transactions', 300))
        return root

    def get_acquisition_dates_for_team(
        self, league_key: str, team_key: str,
        week_start_date: Optional[datetime] = None, week_end_date: Optional[datetime] = None
    ) -> Dict[str, datetime]:
        """Get the first date each player was added to the team during the given week (for roster-change-aware game count).
        
        Fetches league transactions, filters to adds (and trades where player is acquired) to this team,
        and returns {player_key: date} so we don't count a player on days before his acquisition_date.
        """
        cache_key = f"acquisition_dates:{league_key}:{team_key}:{week_start_date.date() if week_start_date else 'none'}:{week_end_date.date() if week_end_date else 'none'}"
        cached = _get_cached(cache_key)
        if cached is not None:
            # Restore datetime from ISO string
            return {k: datetime.fromisoformat(v) for k, v in cached.items()}
        
        try:
            root = self._get_league_transactions_xml(league_key)
        except Exception as e:
            debug_print(f"[Yahoo API] Could not fetch transactions: {e}")
            return {}
        
        # acquisition_dates[player_key] = date (only for players added to team_key during the week)
        acquisition_dates: Dict[str, datetime] = {}
        week_start = (week_start_date.date() if week_start_date else None)
        week_end = (week_end_date.date() if week_end_date else None)
        
        for txn in root.findall('.//yh:transaction', NS):
            txn_type = self._get_text(txn, 'yh:type')
            ts = self._get_text(txn, 'yh:timestamp')
            if not ts:
                continue
            try:
                txn_dt = datetime.utcfromtimestamp(int(ts))
            except (TypeError, ValueError, OSError):
                continue
            txn_date = txn_dt.date()
            if week_start is not None and txn_date < week_start:
                continue
            if week_end is not None and txn_date > week_end:
                continue
            
            # Look at players in this transaction (try multiple XML structures)
            txn_players = (
                txn.findall('.//yh:transaction_players/yh:player', NS) or
                txn.findall('.//yh:players/yh:player', NS) or
                txn.findall('.//yh:player', NS)
            )
            for txn_player in txn_players:
                dest_team = self._get_text(txn_player, 'yh:destination_team_key') or self._get_text(txn_player, './/yh:destination_team_key')
                src_team = self._get_text(txn_player, 'yh:source_team_key') or self._get_text(txn_player, './/yh:source_team_key')
                player_type = self._get_text(txn_player, 'yh:type') or self._get_text(txn_player, './/yh:type')
                player_key = self._get_text(txn_player, 'yh:player_key') or self._get_text(txn_player, './/yh:player_key')
                if not player_key:
                    # Player key might be inside a child
                    p = txn_player.find('yh:player_key', NS) or txn_player.find('.//yh:player_key', NS)
                    if p is not None and p.text:
                        player_key = p.text
                
                if not player_key:
                    continue
                # Add to our team: destination is us, or we're the destination in a trade
                if dest_team != team_key:
                    continue
                # type can be 'add' (waiver/free agent) or part of trade
                if txn_type and txn_type.lower() not in ('add', 'trade'):
                    continue
                if player_type and player_type.lower() not in ('add', 'trade'):
                    continue
                # Use transaction date as acquisition date (earliest add in the week)
                txn_date_dt = datetime.combine(txn_date, datetime.min.time())
                if player_key not in acquisition_dates or txn_date_dt < acquisition_dates[player_key]:
                    acquisition_dates[player_key] = txn_date_dt
        
        # Cache as ISO strings
        _set_cached(cache_key, {k: v.isoformat() for k, v in acquisition_dates.items()}, CACHE_TTL['transactions'])
        if acquisition_dates:
            debug_print(f"[Yahoo API] Acquisition dates for team: {acquisition_dates}")
        return acquisition_dates
    
    def get_il_history_for_team(
        self, league_key: str, team_key: str,
        week_start_date: Optional[datetime] = None, week_end_date: Optional[datetime] = None
    ) -> Tuple[Dict[str, datetime], Dict[str, datetime]]:
        """Get IL placement and removal dates for players during the given week.
        
        Returns:
            tuple: (il_placements, il_removals)
                - il_placements: {player_key: placement_date} when player was moved to IL
                - il_removals: {player_key: removal_date} when player was removed from IL
        
        Note: Yahoo API doesn't always provide IL transactions explicitly. This function
        attempts to extract IL moves from roster changes and transactions.
        """
        cache_key = f"il_history:{league_key}:{team_key}:{week_start_date.date() if week_start_date else 'none'}:{week_end_date.date() if week_end_date else 'none'}"
        cached = _get_cached(cache_key)
        if cached is not None:
            # Restore datetime from ISO string
            placements = {k: datetime.fromisoformat(v) for k, v in cached.get('placements', {}).items()}
            removals = {k: datetime.fromisoformat(v) for k, v in cached.get('removals', {}).items()}
            return placements, removals
        
        il_placements: Dict[str, datetime] = {}
        il_removals: Dict[str, datetime] = {}
        week_start = (week_start_date.date() if week_start_date else None)
        week_end = (week_end_date.date() if week_end_date else None)
        
        # Approach: Get current roster and check who is on IL
        # Then, for past days in the week, assume they were placed on IL at start of week
        # This is a simplified approach since Yahoo API doesn't always expose IL transaction history
        
        try:
            # Get current roster to see who is currently on IL
            roster = self.get_team_roster(team_key)
            current_il_players = set()
            
            for player in roster:
                roster_pos = player.get('selected_position', {}).get('position', '')
                if roster_pos in ['IL', 'IL+']:
                    current_il_players.add(player.get('player_key', ''))
            
            # Try to get transactions to find IL moves
            try:
                root = self._get_league_transactions_xml(league_key)
                
                for txn in root.findall('.//yh:transaction', NS):
                    txn_type = self._get_text(txn, 'yh:type')
                    ts = self._get_text(txn, 'yh:timestamp')
                    if not ts:
                        continue
                    try:
                        txn_dt = datetime.utcfromtimestamp(int(ts))
                    except (TypeError, ValueError, OSError):
                        continue
                    txn_date = txn_dt.date()
                    if week_start is not None and txn_date < week_start:
                        continue
                    if week_end is not None and txn_date > week_end:
                        continue
                    
                    # Look for roster position changes
                    # Yahoo may represent IL moves as part of roster transactions
                    txn_players = (
                        txn.findall('.//yh:transaction_players/yh:player', NS) or
                        txn.findall('.//yh:players/yh:player', NS) or
                        txn.findall('.//yh:player', NS)
                    )
                    
                    for txn_player in txn_players:
                        player_key = self._get_text(txn_player, 'yh:player_key') or self._get_text(txn_player, './/yh:player_key')
                        if not player_key:
                            p = txn_player.find('yh:player_key', NS) or txn_player.find('.//yh:player_key', NS)
                            if p is not None and p.text:
                                player_key = p.text
                        
                        if not player_key:
                            continue
                        
                        # Check for IL designation in transaction
                        # Yahoo sometimes includes destination_type or similar fields
                        dest_type = self._get_text(txn_player, 'yh:destination_type')
                        src_type = self._get_text(txn_player, 'yh:source_type')
                        
                        txn_date_dt = datetime.combine(txn_date, datetime.min.time())
                        
                        # If destination is IL, this is a placement
                        if dest_type and 'IL' in dest_type.upper():
                            if player_key not in il_placements or txn_date_dt < il_placements[player_key]:
                                il_placements[player_key] = txn_date_dt
                        
                        # If source is IL, this is a removal
                        if src_type and 'IL' in src_type.upper():
                            if player_key not in il_removals or txn_date_dt < il_removals[player_key]:
                                il_removals[player_key] = txn_date_dt
            
            except Exception as e:
                debug_print(f"[Yahoo API] Could not parse IL transactions: {e}")
            
            # Fallback: If we found players currently on IL but no placement date,
            # conservatively assume they were placed at the start of the week
            if week_start and current_il_players:
                for player_key in current_il_players:
                    if player_key not in il_placements:
                        # Default to start of week (conservative - don't count any games this week)
                        il_placements[player_key] = datetime.combine(week_start, datetime.min.time())
        
        except Exception as e:
            debug_print(f"[Yahoo API] Could not fetch IL history: {e}")
        
        # Cache as ISO strings
        cache_data = {
            'placements': {k: v.isoformat() for k, v in il_placements.items()},
            'removals': {k: v.isoformat() for k, v in il_removals.items()}
        }
        _set_cached(cache_key, cache_data, CACHE_TTL['transactions'])
        
        if il_placements or il_removals:
            debug_print(f"[Yahoo API] IL history for team: placements={il_placements}, removals={il_removals}")
        
        return il_placements, il_removals
    
    def get_category_records(self, league_key: str, current_week: int) -> Dict[str, Dict]:
        """Calculate category records (wins/losses/ties per category) for all teams.
        
        Goes through all completed weeks and counts how many categories each team won/lost/tied.
        
        Args:
            league_key: The league key
            current_week: The current week number (we count weeks 1 to current_week-1)
            
        Returns:
            Dict mapping team_key to {cat_wins, cat_losses, cat_ties}
        """
        cache_key = f"cat_records:{league_key}:{current_week}"
        cached = _get_cached(cache_key)
        if cached is not None:
            debug_print(f"[Yahoo API] Using cached category records for {league_key}")
            return cached
        
        # Standard 9-CAT categories with stat IDs
        CATEGORIES = {
            '5': 'FG%',   # Higher is better
            '8': 'FT%',   # Higher is better
            '10': '3PTM', # Higher is better
            '12': 'PTS',  # Higher is better
            '15': 'REB',  # Higher is better
            '16': 'AST',  # Higher is better
            '17': 'STL',  # Higher is better
            '18': 'BLK',  # Higher is better
            '19': 'TO',   # Lower is better
        }
        
        # Initialize records for all teams
        team_records = {}
        records_lock = threading.Lock()
        
        completed_weeks = current_week - 1 if current_week > 1 else 0
        weeks_list = list(range(1, completed_weeks + 1))
        
        # Fetch all scoreboards in parallel (instead of 15 sequential API calls)
        def process_week(week: int):
            try:
                return week, self.get_league_scoreboard(league_key, week)
            except Exception:
                return week, None
        
        def update_records(matchups):
            for matchup in matchups:
                teams = matchup.get('teams', [])
                if len(teams) != 2:
                    continue
                team1, team2 = teams[0], teams[1]
                team1_key = team1.get('team_key')
                team2_key = team2.get('team_key')
                if not team1_key or not team2_key:
                    continue
                team1_stats = team1.get('stats', {})
                team2_stats = team2.get('stats', {})
                with records_lock:
                    for team_key in [team1_key, team2_key]:
                        if team_key and team_key not in team_records:
                            team_records[team_key] = {'cat_wins': 0, 'cat_losses': 0, 'cat_ties': 0}
                    for stat_id, cat_name in CATEGORIES.items():
                        val1 = team1_stats.get(stat_id, 0) or 0
                        val2 = team2_stats.get(stat_id, 0) or 0
                        try:
                            val1, val2 = float(val1), float(val2)
                        except (TypeError, ValueError):
                            continue
                        if cat_name == 'TO':
                            if val1 < val2:
                                team_records[team1_key]['cat_wins'] += 1
                                team_records[team2_key]['cat_losses'] += 1
                            elif val1 > val2:
                                team_records[team1_key]['cat_losses'] += 1
                                team_records[team2_key]['cat_wins'] += 1
                            else:
                                team_records[team1_key]['cat_ties'] += 1
                                team_records[team2_key]['cat_ties'] += 1
                        else:
                            if val1 > val2:
                                team_records[team1_key]['cat_wins'] += 1
                                team_records[team2_key]['cat_losses'] += 1
                            elif val1 < val2:
                                team_records[team1_key]['cat_losses'] += 1
                                team_records[team2_key]['cat_wins'] += 1
                            else:
                                team_records[team1_key]['cat_ties'] += 1
                                team_records[team2_key]['cat_ties'] += 1
        
        with ThreadPoolExecutor(max_workers=min(10, len(weeks_list) or 1)) as executor:
            future_to_week = {executor.submit(process_week, w): w for w in weeks_list}
            for future in as_completed(future_to_week):
                try:
                    week, matchups = future.result()
                    if matchups:
                        update_records(matchups)
                except Exception:
                    continue
        
        _set_cached(cache_key, team_records, CACHE_TTL['cat_records'])
        return team_records
    
    def get_player_stats_averages(self, player_keys: List[str]) -> Dict[str, Dict]:
        """Get season stats for multiple players (cached per batch of 25)."""
        if not player_keys:
            return {}
        
        # Yahoo API allows max 25 players per request - cache each batch
        results = {}
        batches_to_fetch = []
        
        for i in range(0, len(player_keys), 25):
            batch = player_keys[i:i+25]
            batch_key = ','.join(sorted(batch))
            cache_key = f"player_stats:{batch_key}"
            cached_batch = _get_cached(cache_key)
            if cached_batch is not None:
                results.update(cached_batch)
            else:
                batches_to_fetch.append((batch, cache_key))
                
        if not batches_to_fetch:
            return results
            
        def fetch_batch(batch_data):
            batch, cache_key = batch_data
            keys_str = ','.join(batch)
            batch_results = {}
            try:
                root = self._make_request(f"players;player_keys={keys_str}/stats;type=season")
                players_found = root.findall('.//yh:player', NS)
                
                for player in players_found:
                    player_key = self._get_text(player, 'yh:player_key')
                    stats = {}
                    
                    for stat in player.findall('.//yh:stat', NS):
                        stat_id = self._get_text(stat, 'yh:stat_id')
                        value = self._get_text(stat, 'yh:value')
                        stats[stat_id] = self._parse_stat_value(value)
                    
                    if stats:
                        stats['_is_average'] = False
                        batch_results[player_key] = stats
                
                if batch_results:
                    _set_cached(cache_key, batch_results, CACHE_TTL['player_stats'])
                return batch_results
            except Exception as e:
                # Try without type parameter
                try:
                    root = self._make_request(f"players;player_keys={keys_str}/stats")
                    for player in root.findall('.//yh:player', NS):
                        player_key = self._get_text(player, 'yh:player_key')
                        stats = {}
                        for stat in player.findall('.//yh:stat', NS):
                            stat_id = self._get_text(stat, 'yh:stat_id')
                            value = self._get_text(stat, 'yh:value')
                            stats[stat_id] = self._parse_stat_value(value)
                        if stats:
                            stats['_is_average'] = False
                            batch_results[player_key] = stats
                    
                    if batch_results:
                        _set_cached(cache_key, batch_results, CACHE_TTL['player_stats'])
                    return batch_results
                except Exception as e2:
                    return {}

        with ThreadPoolExecutor(max_workers=min(10, len(batches_to_fetch))) as executor:
            future_to_batch = {executor.submit(fetch_batch, b): b for b in batches_to_fetch}
            for future in as_completed(future_to_batch):
                batch_res = future.result()
                if batch_res:
                    results.update(batch_res)
                    
        return results
    
    def get_player_stats_last30(self, player_keys: List[str]) -> Dict[str, Dict]:
        """Get last 30 days stats for multiple players (per-game averages).
        Falls back to season averages if last 30 not available.
        """
        if not player_keys:
            return {}
        
        debug_print(f"[DEBUG] Getting last 30 days stats for {len(player_keys)} players...")
        
        results = {}
        batches_to_fetch = []
        
        for i in range(0, len(player_keys), 25):
            batches_to_fetch.append(player_keys[i:i+25])
            
        def fetch_batch(batch):
            keys_str = ','.join(batch)
            batch_results = {}
            
            # Try different stat types for last 30 days
            stat_types_to_try = ['lastmonth', 'average']
            success = False
            
            for stat_type in stat_types_to_try:
                try:
                    debug_print(f"[DEBUG] Trying: players;player_keys=.../stats;type={stat_type}")
                    root = self._make_request(f"players;player_keys={keys_str}/stats;type={stat_type}")
                    players_found = root.findall('.//yh:player', NS)
                    
                    for player in players_found:
                        player_key = self._get_text(player, 'yh:player_key')
                        stats = {}
                        for stat in player.findall('.//yh:stat', NS):
                            stat_id = self._get_text(stat, 'yh:stat_id')
                            value = self._get_text(stat, 'yh:value')
                            stats[stat_id] = self._parse_stat_value(value)
                        
                        if stats:
                            stats['_is_average'] = True
                            batch_results[player_key] = stats
                    
                    if batch_results:
                        success = True
                        break
                        
                except Exception as e:
                    debug_print(f"[DEBUG] Error with type={stat_type}: {e}")
                    continue
            
            # Fallback to season stats if needed
            if not success:
                debug_print(f"[DEBUG] Falling back to season stats")
                season_stats = self.get_player_stats_averages(batch)
                for pk, stats in season_stats.items():
                    if pk not in batch_results:
                        stats['_is_average'] = False  # Total stats, need to divide by GP
                        batch_results[pk] = stats
                        
            return batch_results

        with ThreadPoolExecutor(max_workers=min(10, len(batches_to_fetch))) as executor:
            future_to_batch = {executor.submit(fetch_batch, b): b for b in batches_to_fetch}
            for future in as_completed(future_to_batch):
                batch_res = future.result()
                if batch_res:
                    results.update(batch_res)
        
        debug_print(f"[DEBUG] Total players with last30 stats: {len(results)}")
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
