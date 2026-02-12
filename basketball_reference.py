"""
Basketball Reference Data Fetcher
Fetches seasonal averages for NBA players from Basketball Reference
"""
import requests
from typing import Dict, Optional
import re
import json
import os
from datetime import datetime

# Cache settings
CACHE_FILE = 'bbref_stats_cache.json'
CACHE_DURATION_HOURS = 24  # Refresh cache every 24 hours (stats don't change that often)

# In-memory cache
_player_stats_cache: Dict[str, Dict] = {}
_cache_timestamp: Optional[datetime] = None


def _load_cache_from_disk() -> bool:
    """Load cache from disk if available and fresh."""
    global _player_stats_cache, _cache_timestamp
    
    if not os.path.exists(CACHE_FILE):
        return False
    
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Check timestamp
        timestamp_str = data.get('_timestamp')
        if timestamp_str:
            cache_time = datetime.fromisoformat(timestamp_str)
            age_hours = (datetime.now() - cache_time).total_seconds() / 3600
            
            if age_hours < CACHE_DURATION_HOURS:
                # Cache is fresh, load it
                _player_stats_cache = data.get('players', {})
                _cache_timestamp = cache_time
                print(f"[BBRef] Loaded {len(_player_stats_cache)} players from disk cache ({age_hours:.1f}h old)")
                return True
            else:
                print(f"[BBRef] Disk cache expired ({age_hours:.1f}h old)")
                return False
        
        return False
        
    except Exception as e:
        print(f"[BBRef] Error loading disk cache: {e}")
        return False


def _save_cache_to_disk():
    """Save current cache to disk."""
    global _player_stats_cache, _cache_timestamp
    
    if not _player_stats_cache:
        return
    
    try:
        data = {
            '_timestamp': _cache_timestamp.isoformat() if _cache_timestamp else datetime.now().isoformat(),
            'players': _player_stats_cache
        }
        
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        
        print(f"[BBRef] Saved {len(_player_stats_cache)} players to disk cache")
        
    except Exception as e:
        print(f"[BBRef] Error saving disk cache: {e}")


def _normalize_name(name: str) -> str:
    """Normalize player name for matching."""
    if not name:
        return ""
    # Remove accents, lowercase, remove Jr./Sr./III etc.
    name = name.lower().strip()
    name = re.sub(r'\s+(jr\.?|sr\.?|iii|ii|iv)$', '', name)
    # Remove special characters
    name = re.sub(r'[^\w\s]', '', name)
    # Normalize whitespace
    name = ' '.join(name.split())
    return name


def get_player_season_averages(player_name: str, team_abbr: str = None) -> Optional[Dict]:
    """
    Get season per-game averages for a player from Basketball Reference.
    
    Args:
        player_name: Player's full name (e.g., "LeBron James")
        team_abbr: Optional team abbreviation to help with matching
    
    Returns:
        Dictionary with per-game averages, or None if not found
        Keys: 'PTS', 'REB', 'AST', 'STL', 'BLK', 'TO', '3PTM', 'FG%', 'FT%', 'GP'
    """
    global _player_stats_cache, _cache_timestamp
    
    # Check if cache needs refresh
    now = datetime.now()
    if _cache_timestamp and (now - _cache_timestamp).total_seconds() < CACHE_DURATION_HOURS * 3600:
        normalized = _normalize_name(player_name)
        if normalized in _player_stats_cache:
            return _player_stats_cache[normalized]
    
    # Try to fetch from cache or return default
    normalized = _normalize_name(player_name)
    if normalized in _player_stats_cache:
        return _player_stats_cache[normalized]
    
    # For now, return None - we'll fetch all stats in bulk
    return None


def fetch_all_nba_season_averages() -> Dict[str, Dict]:
    """
    Fetch season averages for all NBA players from Basketball Reference.
    Returns a dictionary keyed by normalized player name.
    """
    global _player_stats_cache, _cache_timestamp
    
    # Check in-memory cache freshness
    now = datetime.now()
    if _cache_timestamp and (now - _cache_timestamp).total_seconds() < CACHE_DURATION_HOURS * 3600:
        if _player_stats_cache:
            print(f"[BBRef] Using memory cache ({len(_player_stats_cache)} players)")
            return _player_stats_cache
    
    # Try loading from disk cache
    if _load_cache_from_disk():
        return _player_stats_cache
    
    print("[BBRef] Fetching NBA season averages from Basketball Reference...")
    
    try:
        # Basketball Reference per-game stats page for current season
        # Season 2025-26 uses year 2026 in URL
        current_year = datetime.now().year
        # If we're past October, use next year's code
        if datetime.now().month >= 10:
            season_year = current_year + 1
        else:
            season_year = current_year
        
        url = f"https://www.basketball-reference.com/leagues/NBA_{season_year}_per_game.html"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0'
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            print(f"[BBRef] Failed to fetch data: HTTP {response.status_code}")
            if response.status_code == 403:
                print(f"[BBRef] Access forbidden - Basketball Reference may be blocking requests")
                print(f"[BBRef] Will use cached data or fall back to Yahoo stats")
            # Try to use existing cache even if expired
            if _player_stats_cache:
                print(f"[BBRef] Using existing cache ({len(_player_stats_cache)} players)")
                return _player_stats_cache
            return {}
        
        # Parse the HTML to extract stats
        html = response.text
        stats = _parse_bbref_stats(html)
        
        if stats:
            _player_stats_cache = stats
            _cache_timestamp = now
            print(f"[BBRef] Successfully fetched {len(stats)} player averages")
            # Save to disk for persistence
            _save_cache_to_disk()
        
        return stats
        
    except Exception as e:
        print(f"[BBRef] Error fetching data: {e}")
        # Try disk cache as last resort
        if not _player_stats_cache:
            _load_cache_from_disk()
        return _player_stats_cache or {}


def _parse_bbref_stats(html: str) -> Dict[str, Dict]:
    """Parse Basketball Reference HTML to extract player stats."""
    stats = {}
    
    try:
        # Find the per_game_stats table
        # Look for data rows in the table
        
        # Pattern to match player rows
        # Each row has: Rk, Player, Pos, Age, Tm, G, GS, MP, FG, FGA, FG%, 3P, 3PA, 3P%, 2P, 2PA, 2P%, eFG%, FT, FTA, FT%, ORB, DRB, TRB, AST, STL, BLK, TOV, PF, PTS
        
        # Find table body
        table_match = re.search(r'<table[^>]*id="per_game_stats"[^>]*>(.*?)</table>', html, re.DOTALL)
        if not table_match:
            # Try alternate table ID
            table_match = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
        
        if not table_match:
            print("[BBRef] Could not find stats table")
            return stats
        
        table_content = table_match.group(1)
        
        # Find all data rows
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_content, re.DOTALL)
        
        for row in rows:
            # Skip header rows
            if 'data-stat="player"' not in row:
                continue
            
            # Extract player name
            name_match = re.search(r'data-stat="player"[^>]*><a[^>]*>([^<]+)</a>', row)
            if not name_match:
                name_match = re.search(r'data-stat="player"[^>]*>([^<]+)<', row)
            
            if not name_match:
                continue
            
            player_name = name_match.group(1).strip()
            normalized = _normalize_name(player_name)
            
            # Extract stats using data-stat attributes
            player_stats = {}
            
            # Games played
            gp_match = re.search(r'data-stat="g"[^>]*>(\d+)', row)
            player_stats['GP'] = int(gp_match.group(1)) if gp_match else 0
            
            # Points per game
            pts_match = re.search(r'data-stat="pts_per_g"[^>]*>([\d.]+)', row)
            player_stats['PTS'] = float(pts_match.group(1)) if pts_match else 0.0
            
            # Rebounds per game
            reb_match = re.search(r'data-stat="trb_per_g"[^>]*>([\d.]+)', row)
            player_stats['REB'] = float(reb_match.group(1)) if reb_match else 0.0
            
            # Assists per game
            ast_match = re.search(r'data-stat="ast_per_g"[^>]*>([\d.]+)', row)
            player_stats['AST'] = float(ast_match.group(1)) if ast_match else 0.0
            
            # Steals per game
            stl_match = re.search(r'data-stat="stl_per_g"[^>]*>([\d.]+)', row)
            player_stats['STL'] = float(stl_match.group(1)) if stl_match else 0.0
            
            # Blocks per game
            blk_match = re.search(r'data-stat="blk_per_g"[^>]*>([\d.]+)', row)
            player_stats['BLK'] = float(blk_match.group(1)) if blk_match else 0.0
            
            # Turnovers per game
            tov_match = re.search(r'data-stat="tov_per_g"[^>]*>([\d.]+)', row)
            player_stats['TO'] = float(tov_match.group(1)) if tov_match else 0.0
            
            # 3-pointers made per game
            threes_match = re.search(r'data-stat="fg3_per_g"[^>]*>([\d.]+)', row)
            player_stats['3PTM'] = float(threes_match.group(1)) if threes_match else 0.0
            
            # FG% - Basketball Reference stores as decimal (0.507)
            fgpct_match = re.search(r'data-stat="fg_pct"[^>]*>([\d.]+)', row)
            if fgpct_match:
                fg_val = float(fgpct_match.group(1))
                # Convert to percentage if stored as decimal
                player_stats['FG%'] = fg_val * 100 if fg_val < 1 else fg_val
            else:
                player_stats['FG%'] = 0.0
            
            # FT% - Basketball Reference stores as decimal (0.745)
            ftpct_match = re.search(r'data-stat="ft_pct"[^>]*>([\d.]+)', row)
            if ftpct_match:
                ft_val = float(ftpct_match.group(1))
                # Convert to percentage if stored as decimal
                player_stats['FT%'] = ft_val * 100 if ft_val < 1 else ft_val
            else:
                player_stats['FT%'] = 0.0
            
            # Team abbreviation
            team_match = re.search(r'data-stat="team_id"[^>]*>([A-Z]{3})', row)
            player_stats['TEAM'] = team_match.group(1) if team_match else ''
            
            # Mark as per-game average
            player_stats['_is_average'] = True
            player_stats['_source'] = 'basketball_reference'
            
            if player_stats['GP'] > 0:  # Only include players who have played
                stats[normalized] = player_stats
        
        return stats
        
    except Exception as e:
        print(f"[BBRef] Error parsing HTML: {e}")
        return stats


def get_player_stats_by_name(player_name: str) -> Optional[Dict]:
    """
    Get player stats by name. Fetches all stats if cache is empty.
    
    Args:
        player_name: Player's full name
    
    Returns:
        Dictionary with per-game averages or None if not found
    """
    # Ensure we have data - try disk cache first, then fetch
    if not _player_stats_cache:
        if not _load_cache_from_disk():
            fetch_all_nba_season_averages()
    
    normalized = _normalize_name(player_name)
    
    # Try exact match first
    if normalized in _player_stats_cache:
        return _player_stats_cache[normalized]
    
    # Try partial matching
    for cached_name, stats in _player_stats_cache.items():
        # Check if all parts of the search name are in the cached name
        search_parts = normalized.split()
        cached_parts = cached_name.split()
        
        if len(search_parts) >= 2 and len(cached_parts) >= 2:
            # Match first and last name
            if search_parts[0] == cached_parts[0] and search_parts[-1] == cached_parts[-1]:
                return stats
            # Or last name + first initial
            if search_parts[-1] == cached_parts[-1] and search_parts[0][0] == cached_parts[0][0]:
                return stats
    
    return None


def convert_to_yahoo_stat_ids(bbref_stats: Dict) -> Dict:
    """
    Convert Basketball Reference stats to Yahoo stat ID format.
    
    Args:
        bbref_stats: Stats dictionary from Basketball Reference
    
    Returns:
        Stats dictionary with Yahoo stat IDs
    """
    if not bbref_stats:
        return {}
    
    # Get FG% and FT% - Basketball Reference returns them as decimals (0.507) 
    # but our parsing multiplies by 100, so we need to handle both cases
    fg_pct = bbref_stats.get('FG%', 0)
    ft_pct = bbref_stats.get('FT%', 0)
    
    # If values are > 1, they're already percentages (e.g., 50.7)
    # Convert to decimal for Yahoo format
    if fg_pct > 1:
        fg_pct = fg_pct / 100
    if ft_pct > 1:
        ft_pct = ft_pct / 100
    
    # Yahoo stat ID mapping (from yahoo_api.py)
    yahoo_stats = {
        '0': bbref_stats.get('GP', 1),       # Games Played (default 1 to avoid div by 0)
        '5': fg_pct,                          # FG% (as decimal, e.g., 0.507)
        '8': ft_pct,                          # FT% (as decimal, e.g., 0.745)
        '10': bbref_stats.get('3PTM', 0),    # 3-Pointers Made per game
        '12': bbref_stats.get('PTS', 0),     # Points per game
        '15': bbref_stats.get('REB', 0),     # Total Rebounds per game
        '16': bbref_stats.get('AST', 0),     # Assists per game
        '17': bbref_stats.get('STL', 0),     # Steals per game
        '18': bbref_stats.get('BLK', 0),     # Blocks per game
        '19': bbref_stats.get('TO', 0),      # Turnovers per game
        '_is_average': True,                  # These are per-game averages
        '_source': 'basketball_reference'
    }
    
    return yahoo_stats


# Pre-fetch stats on module load (optional, can be removed if too slow)
# fetch_all_nba_season_averages()
