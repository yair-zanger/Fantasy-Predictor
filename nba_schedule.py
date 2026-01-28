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
    """Convenience function to get games count for a team"""
    try:
        games = schedule.get_games_count_this_week(team_abbr)
        # If we get 0, use default of 3-4 games (typical NBA week)
        if games == 0:
            return 3
        return games
    except:
        return 3  # Default to 3 games per week


def get_all_games_this_week() -> Dict[str, int]:
    """Convenience function to get all teams' games count"""
    return schedule.get_all_teams_games_count()


def get_teams_playing_on_date(date: datetime) -> List[str]:
    """Get list of team abbreviations playing on a specific date"""
    try:
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            # Fallback: return empty list (all teams could be playing)
            return []
        
        data = response.json()
        game_dates = data.get('leagueSchedule', {}).get('gameDates', [])
        
        target_date = date.strftime('%Y-%m-%d')
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
