"""
NBA Schedule - Get games per week for each team
"""
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import json
import os
from bs4 import BeautifulSoup
import re


def get_pacific_time() -> datetime:
    """
    Get current time in Pacific Time (PST/PDT) to match Yahoo Fantasy's timezone.
    Yahoo Fantasy uses Pacific Time for determining game dates.
    
    PST = UTC-8 (Winter: roughly November-March)
    PDT = UTC-7 (Summer: roughly March-November)
    """
    now = datetime.now()
    # Determine if we're in DST (PDT) or standard time (PST)
    # Approximate: DST is roughly March-November
    is_dst = 3 <= now.month <= 10
    pst_offset = -7 if is_dst else -8
    return now + timedelta(hours=pst_offset)


def get_pacific_date() -> datetime:
    """Get current date in Pacific Time (midnight)"""
    return get_pacific_time().replace(hour=0, minute=0, second=0, microsecond=0)


# Cache file for disk persistence
SCHEDULE_CACHE_FILE = 'nba_schedule_disk_cache.json'
WEEKLY_CACHE_TTL_HOURS = 6  # Cache for 6 hours (schedule doesn't change often)

# In-memory cache for today's games (refreshes once per day)
_todays_games_cache: Dict[str, Dict] = {}
_todays_games_date: Optional[str] = None

# In-memory cache for weekly schedule (refreshes once per hour)
_weekly_schedule_cache: Dict[str, List[Dict]] = {}
_weekly_schedule_timestamp: Optional[datetime] = None

# Flag to track if disk cache was loaded
_schedule_cache_loaded = False

# Cache for hashtagbasketball scraped schedule (refreshes once per day)
_hashtag_schedule_cache: Dict[str, List[str]] = {}
_hashtag_schedule_date: Optional[str] = None
HASHTAG_CACHE_FILE = 'hashtag_schedule_cache.json'


def _load_schedule_cache_from_disk():
    """Load schedule cache from disk."""
    global _todays_games_cache, _todays_games_date
    global _weekly_schedule_cache, _weekly_schedule_timestamp
    global _schedule_cache_loaded
    
    if _schedule_cache_loaded:
        return
    
    if not os.path.exists(SCHEDULE_CACHE_FILE):
        _schedule_cache_loaded = True
        return
    
    try:
        with open(SCHEDULE_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        
        # Load today's games (only if still today)
        if data.get('todays_games_date') == today_str:
            _todays_games_cache = data.get('todays_games', {})
            _todays_games_date = data.get('todays_games_date')
            print(f"[NBA Schedule] Loaded today's games from disk ({len(_todays_games_cache)} teams)")
        
        # Load weekly schedule (check if still valid)
        if data.get('weekly_schedule_timestamp'):
            cached_time = datetime.fromisoformat(data['weekly_schedule_timestamp'])
            age_hours = (now - cached_time).total_seconds() / 3600
            
            if age_hours < WEEKLY_CACHE_TTL_HOURS:
                _weekly_schedule_cache = data.get('weekly_schedule', {})
                _weekly_schedule_timestamp = cached_time
                print(f"[NBA Schedule] Loaded weekly schedule from disk ({len(_weekly_schedule_cache)} dates, {age_hours:.1f}h old)")
        
        _schedule_cache_loaded = True
        
    except Exception as e:
        print(f"[NBA Schedule] Error loading cache from disk: {e}")
        _schedule_cache_loaded = True


def _save_schedule_cache_to_disk():
    """Save schedule cache to disk."""
    try:
        data = {
            'todays_games': _todays_games_cache,
            'todays_games_date': _todays_games_date,
            'weekly_schedule': _weekly_schedule_cache,
            'weekly_schedule_timestamp': _weekly_schedule_timestamp.isoformat() if _weekly_schedule_timestamp else None
        }
        
        with open(SCHEDULE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        
    except Exception as e:
        print(f"[NBA Schedule] Error saving cache to disk: {e}")


def fetch_schedule_from_hashtagbasketball() -> Dict[str, List[str]]:
    """
    Fetch NBA schedule from hashtagbasketball.com's advanced schedule grid.
    Returns dict: {date_str: [list of team abbreviations playing that day]}
    
    Uses caching - only fetches once per day.
    """
    global _hashtag_schedule_cache, _hashtag_schedule_date
    
    # Get today's date as YYYY-MM-DD string
    today_date = datetime.now().date()
    today = today_date.strftime('%Y-%m-%d')
    
    # Check if we have cached data for today
    if _hashtag_schedule_date == today and _hashtag_schedule_cache:
        print(f"[HashtagBB] Using cached schedule ({len(_hashtag_schedule_cache)} dates)")
        return _hashtag_schedule_cache
    
    # Try to load from disk cache
    if os.path.exists(HASHTAG_CACHE_FILE):
        try:
            with open(HASHTAG_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            if cache_data.get('date') == today:
                _hashtag_schedule_cache = cache_data.get('schedule', {})
                _hashtag_schedule_date = today
                print(f"[HashtagBB] Loaded schedule from disk cache ({len(_hashtag_schedule_cache)} dates)")
                return _hashtag_schedule_cache
        except Exception as e:
            print(f"[HashtagBB] Error loading cache: {e}")
    
    print("[HashtagBB] Fetching schedule from hashtagbasketball.com...")
    
    try:
        url = "https://hashtagbasketball.com/advanced-nba-schedule-grid"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"[HashtagBB] Failed to fetch: HTTP {response.status_code}")
            return _hashtag_schedule_cache or {}
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Find the schedule table by ID (ContentPlaceHolder1_w16_GridView1)
        table = soup.find('table', id=lambda x: x and 'GridView' in x)
        
        if not table:
            # Fallback: find by class
            table = soup.find('table', class_='table--statistics')
        
        if not table:
            print("[HashtagBB] Could not find schedule table in HTML")
            return _hashtag_schedule_cache or {}
        
        # Parse the table
        schedule_data = {}
        
        # Get header row to extract dates and day names
        thead = table.find('thead')
        if not thead:
            thead = table.find('tr')
        
        # Extract column headers (dates with day names)
        date_columns = []  # Will store: [(day_name, date_str), ...]
        if thead:
            header_row = thead.find_all('th') or thead.find_all('td')
            
            # Get current week's Monday (start of NBA fantasy week) - Pacific Time
            now_pacific = get_pacific_time()
            days_since_monday = now_pacific.weekday()  # Monday = 0
            week_start = now_pacific - timedelta(days=days_since_monday)
            
            # Skip first 2 columns (Team, Games)
            for i, cell in enumerate(header_row[2:]):
                day_name = cell.get_text(strip=True).lower()
                
                # Map day name to date
                day_map = {
                    'monday': 0, 'mon': 0,
                    'tuesday': 1, 'tue': 1, 'tues': 1,
                    'wednesday': 2, 'wed': 2,
                    'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
                    'friday': 4, 'fri': 4,
                    'saturday': 5, 'sat': 5,
                    'sunday': 6, 'sun': 6
                }
                
                day_offset = None
                for key, offset in day_map.items():
                    if key in day_name:
                        day_offset = offset
                        break
                
                if day_offset is not None:
                    date = week_start + timedelta(days=day_offset)
                    date_str = date.strftime('%Y-%m-%d')
                    date_columns.append(date_str)
                else:
                    date_columns.append(None)
        
        # Parse data rows
        tbody = table.find('tbody') or table
        rows = tbody.find_all('tr')
        
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 3:  # Need at least Team + Games + 1 day
                continue
            
            # First cell is team name/abbreviation
            team_cell = cells[0]
            team_text = team_cell.get_text(strip=True)
            
            # Extract 3-letter abbreviation (usually at start or in data attribute)
            team_abbr = None
            if team_cell.get('data-team'):
                team_abbr = team_cell.get('data-team').upper()[:3]
            else:
                # Try to extract from text (e.g., "ATL Hawks" -> "ATL")
                parts = team_text.split()
                team_name_lower = team_text.lower()
                
                # Special handling for teams with non-standard abbreviations
                if 'lakers' in team_name_lower or 'los angeles lakers' in team_name_lower:
                    team_abbr = 'LAL'
                elif 'clippers' in team_name_lower or 'los angeles clippers' in team_name_lower:
                    team_abbr = 'LAC'
                elif 'knicks' in team_name_lower or 'new york knicks' in team_name_lower:
                    team_abbr = 'NYK'
                elif 'spurs' in team_name_lower or 'san antonio spurs' in team_name_lower:
                    team_abbr = 'SAS'
                elif 'warriors' in team_name_lower or 'golden state warriors' in team_name_lower:
                    team_abbr = 'GSW'
                elif 'pelicans' in team_name_lower or 'new orleans pelicans' in team_name_lower:
                    team_abbr = 'NOP'
                elif 'suns' in team_name_lower or 'phoenix suns' in team_name_lower:
                    team_abbr = 'PHO'
                elif parts and len(parts[0]) == 3:
                    team_abbr = parts[0].upper()
                elif parts:
                    # Try to match full name to abbreviation
                    for abbr, full_name in NBA_TEAMS.items():
                        if full_name.lower() in team_name_lower or abbr.lower() in team_name_lower:
                            team_abbr = abbr
                            break
            
            if not team_abbr or len(team_abbr) != 3:
                continue
            
            # Skip "Games" column (index 1), start from index 2
            for i, cell in enumerate(cells[2:]):
                if i >= len(date_columns):
                    break
                
                date_str = date_columns[i]
                if not date_str:
                    continue
                
                # Check if team has game on this date
                cell_text = cell.get_text(strip=True)
                
                # Check for game indicators:
                # - "@XXX" or "vs XXX" (opponent notation)
                # - Non-empty cell that's not "-"
                has_game = False
                if cell_text and cell_text != '-':
                    # Check for opponent indicators
                    if '@' in cell_text or 'vs' in cell_text.lower():
                        has_game = True
                    # Or check cell background color/class (often colored when there's a game)
                    elif cell.get('class'):
                        cell_classes = ' '.join(cell.get('class', []))
                        has_game = 'game' in cell_classes or 'playing' in cell_classes
                    # Or just non-empty
                    elif len(cell_text) > 0:
                        has_game = True
                
                if has_game:
                    if date_str not in schedule_data:
                        schedule_data[date_str] = []
                    if team_abbr not in schedule_data[date_str]:
                        schedule_data[date_str].append(team_abbr)
        
        if schedule_data:
            print(f"[HashtagBB] Successfully scraped schedule: {len(schedule_data)} dates, {sum(len(teams) for teams in schedule_data.values())} team-games")
            
            # Ensure all data is JSON-serializable (convert any datetime to strings)
            clean_schedule_data = {}
            for date_key, teams in schedule_data.items():
                # Ensure date key is string
                if isinstance(date_key, datetime):
                    date_key = date_key.strftime('%Y-%m-%d')
                clean_schedule_data[str(date_key)] = [str(t) for t in teams]
            
            # Cache the results
            _hashtag_schedule_cache = clean_schedule_data
            _hashtag_schedule_date = today
            
            # Save to disk
            try:
                cache_obj = {
                    'date': str(today),
                    'schedule': clean_schedule_data
                }
                with open(HASHTAG_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cache_obj, f, ensure_ascii=False, indent=2)
                print(f"[HashtagBB] Saved cache to {HASHTAG_CACHE_FILE}")
            except Exception as e:
                print(f"[HashtagBB] Error saving cache: {e}")
                import traceback
                traceback.print_exc()
            
            return schedule_data
        else:
            print("[HashtagBB] No schedule data found in table")
            return _hashtag_schedule_cache or {}
        
    except Exception as e:
        print(f"[HashtagBB] Error scraping schedule: {e}")
        import traceback
        traceback.print_exc()
        return _hashtag_schedule_cache or {}


# NBA Team abbreviations mapping
NBA_TEAMS = {
    'ATL': 'Atlanta Hawks',
    'BOS': 'Boston Celtics',
    'BKN': 'Brooklyn Nets',
    'CHA': 'Charlotte Hornets',
    'CHI': 'Chicago Bulls',
    'CLE': 'Cleveland Cavaliers',
    'DAL': 'Dallas Mavericks',
    'DEN': 'Denver Nuggets',
    'DET': 'Detroit Pistons',
    'GSW': 'Golden State Warriors',
    'GS': 'Golden State Warriors',
    'HOU': 'Houston Rockets',
    'IND': 'Indiana Pacers',
    'LAC': 'LA Clippers',
    'LAL': 'Los Angeles Lakers',
    'MEM': 'Memphis Grizzlies',
    'MIA': 'Miami Heat',
    'MIL': 'Milwaukee Bucks',
    'MIN': 'Minnesota Timberwolves',
    'NOP': 'New Orleans Pelicans',
    'NO': 'New Orleans Pelicans',
    'NYK': 'New York Knicks',
    'NY': 'New York Knicks',
    'OKC': 'Oklahoma City Thunder',
    'ORL': 'Orlando Magic',
    'PHI': 'Philadelphia 76ers',
    'PHO': 'Phoenix Suns',
    'PHX': 'Phoenix Suns',
    'POR': 'Portland Trail Blazers',
    'SAC': 'Sacramento Kings',
    'SAS': 'San Antonio Spurs',
    'SA': 'San Antonio Spurs',
    'TOR': 'Toronto Raptors',
    'UTA': 'Utah Jazz',
    'WAS': 'Washington Wizards',
}

# Reverse mapping
TEAM_NAME_TO_ABBR = {}
for abbr, name in NBA_TEAMS.items():
    TEAM_NAME_TO_ABBR[name.lower()] = abbr
    # Also add partial names
    TEAM_NAME_TO_ABBR[name.split()[-1].lower()] = abbr

CACHE_FILE = 'nba_schedule_cache.json'

# Hardcoded schedule from hashtagbasketball.com for current weeks
# Source: https://hashtagbasketball.com/advanced-nba-schedule-grid
# Week 15: Jan 26 - Feb 1, 2026
HARDCODED_SCHEDULE = {
    # Monday Jan 26, 2026
    '2026-01-26': ['ATL', 'IND', 'BOS', 'POR', 'CHI', 'LAL', 'CLE', 'ORL', 'GSW', 'MIN', 'HOU', 'MEM', 'PHI', 'CHA'],
    # Tuesday Jan 27, 2026
    '2026-01-27': ['BKN', 'PHO', 'DEN', 'DET', 'LAC', 'UTA', 'MIL', 'PHI', 'NOP', 'OKC', 'NYK', 'SAC', 'POR', 'WAS'],
    # Wednesday Jan 28, 2026
    '2026-01-28': ['ATL', 'BOS', 'CHA', 'MEM', 'CHI', 'IND', 'CLE', 'LAL', 'DAL', 'MIN', 'GSW', 'UTA', 'HOU', 'SAS', 'MIA', 'ORL', 'NYK', 'TOR'],
    # Thursday Jan 29, 2026
    '2026-01-29': ['ATL', 'HOU', 'BKN', 'DEN', 'CHA', 'DAL', 'DET', 'PHO', 'MIA', 'CHI', 'MIL', 'WAS', 'MIN', 'OKC', 'PHI', 'SAC'],
    # Friday Jan 30, 2026
    '2026-01-30': ['BKN', 'UTA', 'BOS', 'SAC', 'CLE', 'PHO', 'DEN', 'LAC', 'DET', 'GSW', 'LAL', 'WAS', 'MEM', 'NOP', 'NYK', 'POR', 'ORL', 'TOR'],
    # Saturday Jan 31, 2026
    '2026-01-31': ['ATL', 'IND', 'CHA', 'SAS', 'CHI', 'MIA', 'DAL', 'HOU', 'MEM', 'MIN', 'NOP', 'PHI'],
    # Sunday Feb 1, 2026
    '2026-02-01': ['BKN', 'DET', 'BOS', 'MIL', 'CHI', 'MIA', 'CLE', 'POR', 'DEN', 'OKC', 'LAC', 'PHO', 'LAL', 'NYK', 'ORL', 'SAS', 'TOR', 'UTA', 'WAS', 'SAC'],
    
    # Week 16: Feb 2 - Feb 8, 2026 (CORRECTED from Hashtag Basketball table)
    # Monday 02/02 (4 games = 8 teams)
    '2026-02-02': ['CHA', 'HOU', 'IND', 'MEM', 'MIN', 'NOP'],
    # Tuesday 03/02 (10 games = 20 teams)
    '2026-02-03': ['ATL', 'BKN', 'BOS', 'CHI', 'DAL', 'DEN', 'DET', 'GSW', 'IND', 'LAL', 'MIA', 'MIL', 'NYK', 'OKC', 'ORL', 'PHI', 'PHO', 'POR', 'UTA', 'WAS'],
    # Wednesday 04/02 (7 games = 14 teams)
    '2026-02-04': ['BOS', 'CLE', 'DET', 'HOU', 'LAC', 'LAL', 'MEM', 'MIL', 'MIN', 'NOP', 'NYK', 'OKC', 'PHI', 'SAC', 'SAS', 'TOR'],
    # Thursday 05/02 (8 games = 16 teams) 
    '2026-02-05': ['ATL', 'BKN', 'BOS', 'CHI', 'DAL', 'DEN', 'DET', 'GSW', 'IND', 'LAL', 'MIL', 'OKC', 'ORL', 'PHI', 'PHO', 'POR', 'TOR', 'UTA', 'WAS'],
    # Friday 06/02 (6 games = 12 teams)
    '2026-02-06': ['BOS', 'CLE', 'HOU', 'LAC', 'MEM', 'MIA', 'MIN', 'NOP', 'NYK', 'POR', 'SAC', 'SAS'],
    # Saturday 07/02 (10 games = 20 teams)
    '2026-02-07': ['ATL', 'BKN', 'BOS', 'CHI', 'DAL', 'DEN', 'DET', 'GSW', 'HOU', 'IND', 'LAL', 'MIL', 'OKC', 'ORL', 'PHI', 'PHO', 'POR', 'TOR', 'UTA', 'WAS'],
    # Sunday 08/02 (4 games = 8 teams) - Note: some teams appear in Monday W17 column
    '2026-02-08': ['BOS', 'CHI', 'CLE', 'DEN', 'IND', 'MIA', 'MIL', 'NYK', 'TOR', 'WAS'],
    
    # Week 17: Feb 9 - Feb 15, 2026
    '2026-02-09': ['BKN', 'PHI', 'BOS', 'IND', 'CHI', 'ORL', 'DAL', 'LAL', 'DET', 'MIL', 'GSW', 'TOR', 'PHO', 'POR', 'UTA', 'WAS'],
    '2026-02-10': ['ATL', 'CLE', 'CHA', 'HOU', 'LAC', 'MEM', 'MIN', 'NOP', 'NYK', 'OKC', 'SAC', 'SAS'],
    '2026-02-11': ['BKN', 'MIA', 'BOS', 'DEN', 'CHI', 'IND', 'DAL', 'PHI', 'DET', 'ORL', 'GSW', 'LAL', 'MIL', 'TOR', 'PHO', 'UTA', 'POR', 'WAS'],
    '2026-02-12': ['ATL', 'CLE', 'CHA', 'MEM', 'HOU', 'NOP', 'LAC', 'SAS', 'MIN', 'OKC', 'NYK', 'SAC'],
    '2026-02-13': ['BKN', 'IND', 'BOS', 'MIA', 'CHI', 'PHI', 'DAL', 'MIL', 'DEN', 'GSW', 'DET', 'TOR', 'LAL', 'UTA', 'ORL', 'WAS', 'PHO', 'POR'],
    '2026-02-14': ['ATL', 'OKC', 'CHA', 'CLE', 'HOU', 'SAS', 'LAC', 'SAC', 'MEM', 'NOP', 'MIN', 'NYK'],
    '2026-02-15': ['BKN', 'MIA', 'BOS', 'DET', 'CHI', 'PHO', 'DAL', 'GSW', 'DEN', 'POR', 'IND', 'MIL', 'LAL', 'ORL', 'PHI', 'TOR', 'UTA', 'WAS'],
}

# Games per team per week from hashtagbasketball.com
WEEKLY_GAMES = {
    '2026-01-26': {  # Week 15: Jan 26 - Feb 1
        'ATL': 4, 'BOS': 4, 'BKN': 4, 'CHA': 4, 'CHI': 5, 'CLE': 4, 'DAL': 3, 'DEN': 4,
        'DET': 4, 'GSW': 3, 'HOU': 4, 'IND': 3, 'LAC': 3, 'LAL': 4, 'MEM': 4, 'MIA': 4,
        'MIL': 3, 'MIN': 4, 'NOP': 3, 'NYK': 4, 'OKC': 3, 'ORL': 4, 'PHI': 4, 'PHO': 4,
        'POR': 4, 'SAC': 4, 'SAS': 3, 'TOR': 3, 'UTA': 4, 'WAS': 4,
    },
    '2026-02-02': {  # Week 16: Feb 2 - Feb 8 (CORRECTED from Hashtag Basketball)
        'ATL': 3, 'BOS': 4, 'BKN': 3, 'CHA': 3, 'CHI': 3, 'CLE': 2, 'DAL': 3, 'DEN': 3,
        'DET': 3, 'GSW': 3, 'HOU': 4, 'IND': 4, 'LAC': 4, 'LAL': 3, 'MEM': 4, 'MIA': 3,
        'MIL': 3, 'MIN': 4, 'NOP': 3, 'NYK': 4, 'OKC': 3, 'ORL': 3, 'PHI': 4, 'PHO': 3,
        'POR': 3, 'SAC': 3, 'SAS': 3, 'TOR': 3, 'UTA': 3, 'WAS': 4,
    },
    '2026-02-09': {  # Week 17: Feb 9 - Feb 15
        'ATL': 4, 'BOS': 4, 'BKN': 4, 'CHA': 4, 'CHI': 4, 'CLE': 4, 'DAL': 4, 'DEN': 4,
        'DET': 4, 'GSW': 4, 'HOU': 4, 'IND': 4, 'LAC': 4, 'LAL': 4, 'MEM': 4, 'MIA': 4,
        'MIL': 4, 'MIN': 4, 'NOP': 4, 'NYK': 4, 'OKC': 4, 'ORL': 4, 'PHI': 4, 'PHO': 4,
        'POR': 4, 'SAC': 4, 'SAS': 4, 'TOR': 4, 'UTA': 4, 'WAS': 4,
    },
}


class NBASchedule:
    """Fetch and manage NBA schedule data"""
    
    def __init__(self):
        self.schedule_cache = {}
        self.load_cache()
    
    def get_week_dates(self, week_start: datetime = None) -> tuple:
        """Get start and end dates for a fantasy week (Monday to Sunday) - Pacific Time"""
        if week_start is None:
            # Find the most recent Monday (Pacific Time)
            today_pacific = get_pacific_time()
            days_since_monday = today_pacific.weekday()  # Monday = 0, so this gives days since Monday
            week_start = today_pacific - timedelta(days=days_since_monday)
        
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        
        return week_start, week_end
    
    def get_games_this_week(self, team_abbr: str, week_start: datetime = None) -> List[Dict]:
        """Get all games for a team in the current/specified week"""
        start, end = self.get_week_dates(week_start)
        
        # Normalize team abbreviation
        team_abbr = self._normalize_team_abbr(team_abbr)
        
        # Get schedule from NBA API
        games = self._fetch_team_schedule(team_abbr, start, end)
        
        return games
    
    def get_games_count_this_week(self, team_abbr: str, week_start: datetime = None) -> int:
        """Get number of games for a team this week"""
        games = self.get_games_this_week(team_abbr, week_start)
        return len(games)
    
    def get_all_teams_games_count(self, week_start: datetime = None) -> Dict[str, int]:
        """Get games count for all NBA teams this week"""
        start, end = self.get_week_dates(week_start)
        
        # Try to get from cache
        cache_key = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"
        if cache_key in self.schedule_cache:
            return self.schedule_cache[cache_key]
        
        # Fetch full schedule
        games_count = self._fetch_weekly_schedule(start, end)
        
        # Cache results
        self.schedule_cache[cache_key] = games_count
        self.save_cache()
        
        return games_count
    
    def _normalize_team_abbr(self, abbr: str) -> str:
        """Normalize team abbreviation"""
        abbr = abbr.upper().strip()
        
        # Handle common variations
        variations = {
            'GS': 'GSW',
            'NO': 'NOP',
            'NY': 'NYK',
            'PHX': 'PHO',
            'SA': 'SAS',
        }
        
        return variations.get(abbr, abbr)
    
    def _fetch_team_schedule(self, team_abbr: str, start: datetime, end: datetime) -> List[Dict]:
        """Fetch schedule for a specific team from NBA API"""
        try:
            # Use NBA Stats API
            url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
            
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                return self._get_fallback_games(team_abbr, start, end)
            
            data = response.json()
            games = []
            
            # Parse the schedule
            game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
            
            for game_date in game_dates:
                date_str = game_date.get('gameDate', '')
                try:
                    game_dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
                except:
                    continue
                
                if start <= game_dt <= end:
                    for game in game_date.get('games', []):
                        home_team = game.get('homeTeam', {}).get('teamTricode', '')
                        away_team = game.get('awayTeam', {}).get('teamTricode', '')
                        
                        if team_abbr in [home_team, away_team]:
                            games.append({
                                'date': date_str[:10],
                                'home_team': home_team,
                                'away_team': away_team,
                                'is_home': team_abbr == home_team
                            })
            
            return games
            
        except Exception as e:
            print(f"Error fetching NBA schedule: {e}")
            return self._get_fallback_games(team_abbr, start, end)
    
    def _fetch_weekly_schedule(self, start: datetime, end: datetime) -> Dict[str, int]:
        """Fetch games count for all teams in a week"""
        games_count = defaultdict(int)
        
        try:
            url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
            
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                return self._get_fallback_weekly_games(start, end)
            
            data = response.json()
            game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
            
            for game_date in game_dates:
                date_str = game_date.get('gameDate', '')
                try:
                    game_dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
                except:
                    continue
                
                if start <= game_dt <= end:
                    for game in game_date.get('games', []):
                        home_team = game.get('homeTeam', {}).get('teamTricode', '')
                        away_team = game.get('awayTeam', {}).get('teamTricode', '')
                        
                        if home_team:
                            games_count[home_team] += 1
                        if away_team:
                            games_count[away_team] += 1
            
            return dict(games_count)
            
        except Exception as e:
            print(f"Error fetching weekly schedule: {e}")
            return self._get_fallback_weekly_games(start, end)
    
    def _get_fallback_games(self, team_abbr: str, start: datetime, end: datetime) -> List[Dict]:
        """Fallback: estimate 3-4 games per week"""
        # Most NBA teams play 3-4 games per week
        days = (end - start).days + 1
        estimated_games = min(4, max(3, days // 2))
        
        return [{'date': 'estimated', 'estimated': True}] * estimated_games
    
    def _get_fallback_weekly_games(self, start: datetime, end: datetime) -> Dict[str, int]:
        """Fallback: estimate games for all teams"""
        # Default to 3-4 games per team per week
        games_count = {}
        for abbr in set(NBA_TEAMS.keys()):
            if len(abbr) == 3:  # Only use standard 3-letter codes
                games_count[abbr] = 4  # Conservative estimate
        return games_count
    
    def save_cache(self):
        """Save schedule cache to file"""
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.schedule_cache, f)
        except:
            pass
    
    def load_cache(self):
        """Load schedule cache from file"""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    self.schedule_cache = json.load(f)
            except:
                self.schedule_cache = {}


# Singleton instance
schedule = NBASchedule()


def get_team_games_this_week(team_abbr: str) -> int:
    """Convenience function to get games count for a team.
    
    First checks hardcoded weekly games from hashtagbasketball.com,
    then falls back to API or default.
    """
    # Normalize team abbreviation
    team_abbr = schedule._normalize_team_abbr(team_abbr)
    
    # Get current week start (Pacific Time - Monday)
    today_pacific = get_pacific_time()
    days_since_monday = today_pacific.weekday()  # Monday = 0
    week_start = today_pacific - timedelta(days=days_since_monday)
    week_start_str = week_start.strftime('%Y-%m-%d')
    
    # Check hardcoded weekly games first
    if week_start_str in WEEKLY_GAMES:
        weekly_data = WEEKLY_GAMES[week_start_str]
        if team_abbr in weekly_data:
            return weekly_data[team_abbr]
        # Try alternate abbreviations
        alt_abbrs = {'GSW': 'GS', 'NOP': 'NO', 'NYK': 'NY', 'PHO': 'PHX', 'SAS': 'SA'}
        for main, alt in alt_abbrs.items():
            if team_abbr == main and alt in weekly_data:
                return weekly_data[alt]
            if team_abbr == alt and main in weekly_data:
                return weekly_data[main]
    
    # Fallback to API
    try:
        games = schedule.get_games_count_this_week(team_abbr)
        if games == 0:
            return 3
        return games
    except:
        return 3  # Default to 3 games per week


def get_all_games_this_week() -> Dict[str, int]:
    """Convenience function to get all teams' games count"""
    return schedule.get_all_teams_games_count()


def get_teams_playing_on_date(date: datetime) -> List[str]:
    """Get list of team abbreviations playing on a specific date.
    
    First checks scraped schedule from hashtagbasketball.com,
    then falls back to NBA API if date not found.
    """
    target_date = date.strftime('%Y-%m-%d')
    
    # First, check scraped schedule (most reliable for current season)
    hashtag_schedule = fetch_schedule_from_hashtagbasketball()
    if target_date in hashtag_schedule:
        teams = hashtag_schedule[target_date]
        print(f"[DEBUG] Using scraped schedule for {target_date}: {len(teams)} teams")
        return teams
    
    # Fallback to NBA API
    try:
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            print(f"[DEBUG] NBA API failed for {target_date}, no hardcoded data available")
            return []
        
        data = response.json()
        game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
        
        teams_playing = []
        
        for game_date in game_dates:
            date_str = game_date.get('gameDate', '')[:10]
            
            if date_str == target_date:
                for game in game_date.get('games', []):
                    home_team = game.get('homeTeam', {}).get('teamTricode', '')
                    away_team = game.get('awayTeam', {}).get('teamTricode', '')
                    
                    if home_team and home_team not in teams_playing:
                        teams_playing.append(home_team)
                    if away_team and away_team not in teams_playing:
                        teams_playing.append(away_team)
                break
        
        if teams_playing:
            print(f"[DEBUG] Using NBA API for {target_date}: {len(teams_playing)} teams")
        else:
            print(f"[DEBUG] No games found for {target_date}")
        
        return teams_playing
        
    except Exception as e:
        print(f"Error getting teams playing on {date}: {e}")
        return []


def get_team_games_remaining_this_week(team_abbr: str) -> int:
    """Get number of games remaining this week for a team (from today onwards)"""
    try:
        # Get the current week's end date (Monday) in Pacific Time
        _, week_end = get_week_dates_range()
        today = get_pacific_time()
        
        # Normalize team abbreviation
        team_abbr = schedule._normalize_team_abbr(team_abbr)
        
        games = schedule._fetch_team_schedule(team_abbr, today, week_end)
        
        # Filter games from today onwards
        remaining = 0
        today_str = today.strftime('%Y-%m-%d')
        
        for game in games:
            game_date = game.get('date', '')
            if game_date >= today_str or game.get('estimated'):
                remaining += 1
        
        return remaining if remaining > 0 else 1  # At least 1 remaining
        
    except:
        return 2  # Default estimate


def get_week_dates_range() -> Tuple[datetime, datetime]:
    """Get the start (Monday) and end (Sunday) dates for the current fantasy week (Pacific Time)"""
    # Use Pacific Time for week boundaries (Monday to Sunday)
    today_pacific = get_pacific_time()
    # Week starts on Monday
    days_since_monday = today_pacific.weekday()  # Monday = 0
    week_start = today_pacific - timedelta(days=days_since_monday)
    week_end = week_start + timedelta(days=6)
    
    return week_start.replace(hour=0, minute=0, second=0, microsecond=0), \
           week_end.replace(hour=23, minute=59, second=59, microsecond=0)


def get_todays_games() -> Dict[str, Dict]:
    """
    Get all games happening today with details.
    Returns dict: team_abbr -> {opponent, time_israel, is_home, game_time_utc}
    
    Uses caching - only fetches from API once per day.
    """
    global _todays_games_cache, _todays_games_date
    
    # Load from disk if not loaded yet
    _load_schedule_cache_from_disk()
    
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    
    # Check if we have cached data for today
    if _todays_games_date == today_str and _todays_games_cache:
        print(f"[NBA Schedule] Using cached today's games ({len(_todays_games_cache)} teams)")
        return _todays_games_cache
    
    print(f"[NBA Schedule] Fetching today's games from NBA API...")
    
    try:
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            return _todays_games_cache or {}
        
        data = response.json()
        game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
        
        today = datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        
        games_today = {}
        
        for game_date in game_dates:
            date_str = game_date.get('gameDate', '')[:10]
            
            # Check both API date and the day before (in case game is late evening US = next day Israel)
            check_dates = [today_str]
            yesterday = today - timedelta(days=1)
            check_dates.append(yesterday.strftime('%Y-%m-%d'))
            
            if date_str in check_dates:
                for game in game_date.get('games', []):
                    home_team = game.get('homeTeam', {}).get('teamTricode', '')
                    away_team = game.get('awayTeam', {}).get('teamTricode', '')
                    game_time_utc = game.get('gameDateTimeUTC', '')
                    
                    # Convert UTC time to Israel time AND check if it's actually today
                    israel_time = None
                    israel_date_matches_today = False
                    if game_time_utc:
                        try:
                            # Parse UTC time
                            utc_dt = datetime.strptime(game_time_utc, '%Y-%m-%dT%H:%M:%SZ')
                            # Israel is UTC+2 (winter) or UTC+3 (summer/DST)
                            israel_offset = 2
                            if 3 <= today.month <= 10:  # Approximate DST period
                                israel_offset = 3
                            israel_dt = utc_dt + timedelta(hours=israel_offset)
                            israel_time = israel_dt.strftime('%H:%M')
                            # Check if this game is actually TODAY in Israel time
                            israel_date_matches_today = (israel_dt.date() == today.date())
                        except:
                            israel_time = None
                            israel_date_matches_today = False
                    
                    # Only add if game is actually today in Israel time
                    if israel_date_matches_today:
                        # Add home team's game
                        if home_team:
                            games_today[home_team] = {
                                'opponent': away_team,
                                'time_israel': israel_time,
                                'is_home': True,
                                'game_time_utc': game_time_utc
                            }
                        
                        # Add away team's game
                        if away_team:
                            games_today[away_team] = {
                                'opponent': home_team,
                                'time_israel': israel_time,
                                'is_home': False,
                                'game_time_utc': game_time_utc
                            }
        
        # Cache the results
        _todays_games_cache = games_today
        _todays_games_date = today_str
        print(f"[NBA Schedule] Cached {len(games_today)} teams for today")
        
        # Save to disk
        _save_schedule_cache_to_disk()
        
        return games_today
        
    except Exception as e:
        print(f"Error getting today's games: {e}")
        return _todays_games_cache or {}


def get_team_game_today(team_abbr: str) -> Optional[Dict]:
    """
    Get game info for a specific team today.
    Returns None if no game, or dict with: opponent, time_israel, is_home
    """
    team_abbr = schedule._normalize_team_abbr(team_abbr)
    games = get_todays_games()
    return games.get(team_abbr)


def _fetch_and_cache_full_schedule() -> Dict[str, Dict]:
    """
    Fetch the full NBA schedule and cache it.
    Returns dict: date_str -> {team_abbr -> game_info}
    """
    global _weekly_schedule_cache, _weekly_schedule_timestamp
    
    # Load from disk if not loaded yet
    _load_schedule_cache_from_disk()
    
    now = datetime.now()
    
    # Check if cache is still valid
    if _weekly_schedule_timestamp:
        age_hours = (now - _weekly_schedule_timestamp).total_seconds() / 3600
        if age_hours < WEEKLY_CACHE_TTL_HOURS and _weekly_schedule_cache:
            print(f"[NBA Schedule] Using cached schedule ({len(_weekly_schedule_cache)} dates, {age_hours:.1f}h old)")
            return _weekly_schedule_cache
    
    print("[NBA Schedule] Fetching full schedule from NBA API...")
    
    try:
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            return _weekly_schedule_cache or {}
        
        data = response.json()
        game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
        
        schedule_data = {}
        
        for game_date in game_dates:
            raw_date = game_date.get('gameDate', '')
            
            # Parse date - NBA API returns various formats like "MM/DD/YYYY" or "YYYY-MM-DD"
            date_str = None
            try:
                # Try MM/DD/YYYY format first (NBA API format)
                if '/' in raw_date:
                    parsed = datetime.strptime(raw_date.split(' ')[0], '%m/%d/%Y')
                    date_str = parsed.strftime('%Y-%m-%d')
                # Try YYYY-MM-DD format
                elif '-' in raw_date:
                    date_str = raw_date[:10]
                else:
                    continue
            except:
                continue
            
            if not date_str:
                continue
                
            if date_str not in schedule_data:
                schedule_data[date_str] = {}
            
            for game in game_date.get('games', []):
                home_team = game.get('homeTeam', {}).get('teamTricode', '')
                away_team = game.get('awayTeam', {}).get('teamTricode', '')
                game_time_utc = game.get('gameDateTimeUTC', '')
                
                # Convert UTC time to both Pacific Time (for date) and Israel Time (for display)
                israel_time = None
                pacific_date_str = date_str  # Default to API date
                
                if game_time_utc:
                    try:
                        utc_dt = datetime.strptime(game_time_utc, '%Y-%m-%dT%H:%M:%SZ')
                        
                        # Convert to Pacific Time to determine the game date (Yahoo's logic)
                        # PST = UTC-8 (Winter), PDT = UTC-7 (Summer)
                        is_dst = 3 <= now.month <= 10
                        pacific_offset = -7 if is_dst else -8
                        pacific_dt = utc_dt + timedelta(hours=pacific_offset)
                        pacific_date_str = pacific_dt.strftime('%Y-%m-%d')
                        
                        # Convert to Israel Time for display
                        israel_offset = 3 if is_dst else 2
                        israel_dt = utc_dt + timedelta(hours=israel_offset)
                        israel_time = israel_dt.strftime('%H:%M')
                    except:
                        pass
                
                # Ensure the date key exists (using Pacific date as key)
                if pacific_date_str not in schedule_data:
                    schedule_data[pacific_date_str] = {}
                
                # Add home team's game (keyed by Pacific date, showing Israel time)
                if home_team:
                    schedule_data[pacific_date_str][home_team] = {
                        'opponent': away_team,
                        'time_israel': israel_time,
                        'is_home': True
                    }
                
                # Add away team's game (keyed by Pacific date, showing Israel time)
                if away_team:
                    schedule_data[pacific_date_str][away_team] = {
                        'opponent': home_team,
                        'time_israel': israel_time,
                        'is_home': False
                    }
        
        _weekly_schedule_cache = schedule_data
        _weekly_schedule_timestamp = now
        print(f"[NBA Schedule] Cached schedule for {len(schedule_data)} dates")
        
        # Save to disk
        _save_schedule_cache_to_disk()
        
        return schedule_data
        
    except Exception as e:
        print(f"[NBA Schedule] Error fetching schedule: {e}")
        return _weekly_schedule_cache or {}


def get_team_weekly_schedule(team_abbr: str, week_start: datetime = None) -> List[Dict]:
    """
    Get detailed weekly schedule for a team.
    Returns list of games for each day of the week with:
    - date: str (YYYY-MM-DD)
    - day_name: str (Hebrew day name)
    - has_game: bool
    - opponent: str (team abbr) or None
    - time_israel: str (HH:MM) or None
    - is_home: bool or None
    
    Uses cached full schedule to avoid repeated API calls.
    """
    team_abbr = schedule._normalize_team_abbr(team_abbr)
    
    # Get week dates (Pacific Time - Monday to Sunday)
    if week_start is None:
        today_pacific = get_pacific_time()
        days_since_monday = today_pacific.weekday()  # Monday = 0
        week_start = today_pacific - timedelta(days=days_since_monday)
    
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6)
    
    # Hebrew day names (Monday to Sunday)
    hebrew_days = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
    
    # Get cached full schedule (single API call for all teams)
    full_schedule = _fetch_and_cache_full_schedule()
    
    # Extract games for this team from the cached schedule
    games_by_date = {}
    current_day = week_start
    while current_day <= week_end:
        date_str = current_day.strftime('%Y-%m-%d')
        date_games = full_schedule.get(date_str, {})
        
        # Try team abbreviation and alternatives
        game_info = date_games.get(team_abbr)
        if not game_info:
            alt_abbrs = {'GSW': 'GS', 'NOP': 'NO', 'NYK': 'NY', 'PHO': 'PHX', 'SAS': 'SA'}
            for main, alt in alt_abbrs.items():
                if team_abbr == main:
                    game_info = date_games.get(alt)
                    break
                elif team_abbr == alt:
                    game_info = date_games.get(main)
                    break
        
        if game_info:
            games_by_date[date_str] = game_info
        
        current_day += timedelta(days=1)
    
    # Fallback to scraped schedule from hashtagbasketball for any dates not found in API
    hashtag_schedule = fetch_schedule_from_hashtagbasketball()
    
    current_day = week_start
    while current_day <= week_end:
        date_str = current_day.strftime('%Y-%m-%d')
        # If we don't have this date from API, try scraped schedule
        if date_str not in games_by_date:
            if date_str in hashtag_schedule:
                teams_playing = hashtag_schedule[date_str]
                if team_abbr in teams_playing:
                    # Try to find opponent by finding which teams are missing from API
                    opponent = None
                    date_games = full_schedule.get(date_str, {})
                    
                    # #region agent log
                    import json; open(r'c:\Users\USER\Fantasy-Predictor\.cursor\debug.log', 'a', encoding='utf-8').write(json.dumps({'location':'nba_schedule.py:796','message':'Fallback logic - checking for opponent','data':{'date':date_str,'team':team_abbr,'teams_in_hardcoded':len(teams_playing),'teams_in_api':len(date_games),'teams_playing_sample':teams_playing[:6]},'timestamp':datetime.now().timestamp()*1000,'sessionId':'debug-session','runId':'run1','hypothesisId':'A,B'})+'\n')
                    # #endregion
                    
                    # Find teams in HARDCODED but missing from API (like our team)
                    missing_teams = [t for t in teams_playing if t not in date_games]
                    
                    # #region agent log
                    import json; open(r'c:\Users\USER\Fantasy-Predictor\.cursor\debug.log', 'a', encoding='utf-8').write(json.dumps({'location':'nba_schedule.py:799','message':'Missing teams found','data':{'date':date_str,'team':team_abbr,'missing_teams':missing_teams,'missing_count':len(missing_teams)},'timestamp':datetime.now().timestamp()*1000,'sessionId':'debug-session','runId':'run1','hypothesisId':'B'})+'\n')
                    # #endregion
                    
                    # If exactly 2 teams are missing, they play each other!
                    if len(missing_teams) == 2:
                        opponent = missing_teams[0] if missing_teams[1] == team_abbr else missing_teams[1]
                        print(f"[NBA Schedule] Inferred opponent for {team_abbr} on {date_str}: {opponent} (both missing from API)")
                        # #region agent log
                        import json; open(r'c:\Users\USER\Fantasy-Predictor\.cursor\debug.log', 'a', encoding='utf-8').write(json.dumps({'location':'nba_schedule.py:804','message':'Found opponent pair','data':{'date':date_str,'team':team_abbr,'opponent':opponent},'timestamp':datetime.now().timestamp()*1000,'sessionId':'debug-session','runId':'run1','hypothesisId':'B'})+'\n')
                        # #endregion
                    
                    # Only add game if we have a valid opponent
                    if opponent:
                        games_by_date[date_str] = {
                            'opponent': opponent,
                            'time_israel': None,
                            'is_home': None
                        }
                    
                    # #region agent log
                    import json; open(r'c:\Users\USER\Fantasy-Predictor\.cursor\debug.log', 'a', encoding='utf-8').write(json.dumps({'location':'nba_schedule.py:810','message':'Added game to games_by_date','data':{'date':date_str,'team':team_abbr,'opponent':opponent if opponent else '?'},'timestamp':datetime.now().timestamp()*1000,'sessionId':'debug-session','runId':'run1','hypothesisId':'B'})+'\n')
                    # #endregion
        current_day += timedelta(days=1)
        
    # Build weekly schedule
    weekly_schedule = []
    current_day = week_start
    
    while current_day <= week_end:
        date_str = current_day.strftime('%Y-%m-%d')
        day_index = (current_day - week_start).days  # Days since week start (Monday=0)
        
        game_info = games_by_date.get(date_str)
        
        # Use Pacific Time to match Yahoo Fantasy's timezone
        pacific_date = get_pacific_date().date()
        
        weekly_schedule.append({
            'date': date_str,
            'day_name': hebrew_days[day_index],
            'day_short': hebrew_days[day_index][:2] + "'",  # ב', ג', etc.
            'has_game': game_info is not None,
            'opponent': game_info.get('opponent') if game_info else None,
            'time_israel': game_info.get('time_israel') if game_info else None,
            'is_home': game_info.get('is_home') if game_info else None,
            'is_today': current_day.date() == pacific_date,
            'is_past': current_day.date() < pacific_date
        })
        
        current_day += timedelta(days=1)
    
    return weekly_schedule
