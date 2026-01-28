"""
NBA Schedule - Get games per week for each team
"""
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import json
import os

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
    
    # Week 16: Feb 2 - Feb 8, 2026 (partial data)
    '2026-02-02': ['CHA', 'NOP', 'HOU', 'IND', 'MEM', 'MIN'],
    '2026-02-03': ['ATL', 'MIA', 'BKN', 'LAL', 'BOS', 'DAL', 'CHI', 'MIL', 'DEN', 'DET', 'GSW', 'PHI', 'IND', 'UTA', 'NYK', 'WAS', 'OKC', 'ORL', 'PHO', 'POR'],
}

# Games per team per week from hashtagbasketball.com
# Week 15: Jan 26 - Feb 1, 2026
WEEKLY_GAMES = {
    '2026-01-26': {  # Week starting Jan 26
        'ATL': 4, 'BOS': 4, 'BKN': 4, 'CHA': 4, 'CHI': 5, 'CLE': 4, 'DAL': 3, 'DEN': 4,
        'DET': 4, 'GSW': 3, 'HOU': 4, 'IND': 3, 'LAC': 3, 'LAL': 4, 'MEM': 4, 'MIA': 4,
        'MIL': 3, 'MIN': 4, 'NOP': 3, 'NYK': 4, 'OKC': 3, 'ORL': 4, 'PHI': 4, 'PHO': 4,
        'POR': 4, 'SAC': 4, 'SAS': 3, 'TOR': 3, 'UTA': 4, 'WAS': 4,
    }
}


class NBASchedule:
    """Fetch and manage NBA schedule data"""
    
    def __init__(self):
        self.schedule_cache = {}
        self.load_cache()
    
    def get_week_dates(self, week_start: datetime = None) -> tuple:
        """Get start and end dates for a fantasy week (Monday to Sunday)"""
        if week_start is None:
            # Find the most recent Monday
            today = datetime.now()
            days_since_monday = today.weekday()
            week_start = today - timedelta(days=days_since_monday)
        
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
    
    # Get current week start
    today = datetime.now()
    days_since_monday = today.weekday()
    week_start = today - timedelta(days=days_since_monday)
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
    
    First checks hardcoded schedule from hashtagbasketball.com,
    then falls back to NBA API if date not found.
    """
    target_date = date.strftime('%Y-%m-%d')
    
    # First, check hardcoded schedule (most reliable for current season)
    if target_date in HARDCODED_SCHEDULE:
        teams = HARDCODED_SCHEDULE[target_date]
        print(f"[DEBUG] Using hardcoded schedule for {target_date}: {len(teams)} teams")
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
        today = datetime.now()
        # Find week end (Sunday)
        days_until_sunday = 6 - today.weekday()
        week_end = today + timedelta(days=days_until_sunday)
        
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
    """Get the start (Monday) and end (Sunday) dates for the current fantasy week"""
    today = datetime.now()
    # Monday is day 0
    days_since_monday = today.weekday()
    week_start = today - timedelta(days=days_since_monday)
    week_end = week_start + timedelta(days=6)
    
    return week_start.replace(hour=0, minute=0, second=0, microsecond=0), \
           week_end.replace(hour=23, minute=59, second=59, microsecond=0)


def get_todays_games() -> Dict[str, Dict]:
    """
    Get all games happening today with details.
    Returns dict: team_abbr -> {opponent, time_israel, is_home, game_time_utc}
    """
    try:
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            return {}
        
        data = response.json()
        game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
        
        today = datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        
        games_today = {}
        
        for game_date in game_dates:
            date_str = game_date.get('gameDate', '')[:10]
            
            if date_str == today_str:
                for game in game_date.get('games', []):
                    home_team = game.get('homeTeam', {}).get('teamTricode', '')
                    away_team = game.get('awayTeam', {}).get('teamTricode', '')
                    game_time_utc = game.get('gameDateTimeUTC', '')
                    
                    # Convert UTC time to Israel time (UTC+2 or UTC+3)
                    israel_time = None
                    if game_time_utc:
                        try:
                            # Parse UTC time
                            utc_dt = datetime.strptime(game_time_utc, '%Y-%m-%dT%H:%M:%SZ')
                            # Israel is UTC+2 (winter) or UTC+3 (summer/DST)
                            # January is winter, so UTC+2
                            israel_offset = 2
                            if 3 <= today.month <= 10:  # Approximate DST period
                                israel_offset = 3
                            israel_dt = utc_dt + timedelta(hours=israel_offset)
                            israel_time = israel_dt.strftime('%H:%M')
                        except:
                            israel_time = None
                    
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
                break
        
        return games_today
        
    except Exception as e:
        print(f"Error getting today's games: {e}")
        return {}


def get_team_game_today(team_abbr: str) -> Optional[Dict]:
    """
    Get game info for a specific team today.
    Returns None if no game, or dict with: opponent, time_israel, is_home
    """
    team_abbr = schedule._normalize_team_abbr(team_abbr)
    games = get_todays_games()
    return games.get(team_abbr)


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
    """
    team_abbr = schedule._normalize_team_abbr(team_abbr)
    
    # Get week dates
    if week_start is None:
        today = datetime.now()
        days_since_monday = today.weekday()
        week_start = today - timedelta(days=days_since_monday)
    
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6)
    
    # Hebrew day names (Monday to Sunday)
    hebrew_days = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
    
    # Try to fetch detailed schedule from NBA API
    games_by_date = {}
    
    try:
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
            
            for game_date in game_dates:
                date_str = game_date.get('gameDate', '')[:10]
                
                try:
                    game_dt = datetime.strptime(date_str, '%Y-%m-%d')
                except:
                    continue
                
                if week_start <= game_dt <= week_end:
                    for game in game_date.get('games', []):
                        home_team = game.get('homeTeam', {}).get('teamTricode', '')
                        away_team = game.get('awayTeam', {}).get('teamTricode', '')
                        game_time_utc = game.get('gameDateTimeUTC', '')
                        
                        # Check if this game involves our team
                        if team_abbr not in [home_team, away_team]:
                            # Try alternate abbreviations
                            alt_abbrs = {'GSW': 'GS', 'NOP': 'NO', 'NYK': 'NY', 'PHO': 'PHX', 'SAS': 'SA'}
                            found = False
                            for main, alt in alt_abbrs.items():
                                if team_abbr == main and (alt in [home_team, away_team]):
                                    found = True
                                    break
                                if team_abbr == alt and (main in [home_team, away_team]):
                                    found = True
                                    break
                            if not found:
                                continue
                        
                        # Convert UTC time to Israel time
                        israel_time = None
                        if game_time_utc:
                            try:
                                utc_dt = datetime.strptime(game_time_utc, '%Y-%m-%dT%H:%M:%SZ')
                                # Israel is UTC+2 (winter) or UTC+3 (summer/DST)
                                today = datetime.now()
                                israel_offset = 2
                                if 3 <= today.month <= 10:  # Approximate DST period
                                    israel_offset = 3
                                israel_dt = utc_dt + timedelta(hours=israel_offset)
                                israel_time = israel_dt.strftime('%H:%M')
                            except:
                                israel_time = None
                        
                        is_home = (team_abbr == home_team or 
                                   (team_abbr in alt_abbrs.values() and alt_abbrs.get(home_team) == team_abbr) or
                                   (team_abbr in alt_abbrs.keys() and home_team == alt_abbrs.get(team_abbr)))
                        opponent = away_team if is_home else home_team
                        
                        games_by_date[date_str] = {
                            'opponent': opponent,
                            'time_israel': israel_time,
                            'is_home': is_home
                        }
    except Exception as e:
        print(f"Error fetching weekly schedule: {e}")
    
    # Build weekly schedule
    weekly_schedule = []
    current_day = week_start
    
    while current_day <= week_end:
        date_str = current_day.strftime('%Y-%m-%d')
        day_index = current_day.weekday()  # Monday=0, Sunday=6
        
        game_info = games_by_date.get(date_str)
        
        # Also check hardcoded schedule as fallback
        if not game_info and date_str in HARDCODED_SCHEDULE:
            teams_playing = HARDCODED_SCHEDULE[date_str]
            if team_abbr in teams_playing:
                game_info = {
                    'opponent': '?',
                    'time_israel': None,
                    'is_home': None
                }
        
        weekly_schedule.append({
            'date': date_str,
            'day_name': hebrew_days[day_index],
            'day_short': hebrew_days[day_index][:2] + "'",  # ב', ג', etc.
            'has_game': game_info is not None,
            'opponent': game_info.get('opponent') if game_info else None,
            'time_israel': game_info.get('time_israel') if game_info else None,
            'is_home': game_info.get('is_home') if game_info else None,
            'is_today': current_day.date() == datetime.now().date(),
            'is_past': current_day.date() < datetime.now().date()
        })
        
        current_day += timedelta(days=1)
    
    return weekly_schedule
