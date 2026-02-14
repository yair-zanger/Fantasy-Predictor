"""
Fantasy Basketball Matchup Predictor
Predicts weekly matchup results based on player stats, games played, and injuries
"""
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics

from yahoo_api import api, STAT_ID_MAP, STAT_NAME_TO_ID

# ==================== PREDICTION CACHE ====================
# Cache for predictions (in-memory)
_prediction_cache: Dict[str, Dict] = {}
PREDICTION_CACHE_TTL = 3600  # 1 hour - predictions don't change much within same week

def _get_prediction_cached(key: str) -> Optional[Any]:
    """Get cached prediction if not expired."""
    if key in _prediction_cache:
        cached = _prediction_cache[key]
        if datetime.now() < cached['expires']:
            return cached['data']
        else:
            del _prediction_cache[key]
    return None

def _set_prediction_cached(key: str, data: Any):
    """Store prediction in cache with TTL."""
    _prediction_cache[key] = {
        'data': data,
        'expires': datetime.now() + timedelta(seconds=PREDICTION_CACHE_TTL)
    }
from nba_schedule import (
    schedule, get_team_games_this_week, get_teams_playing_on_date,
    get_team_games_remaining_this_week, get_week_dates_range,
    get_todays_games, get_team_game_today, get_team_weekly_schedule,
    get_pacific_time, get_pacific_date, get_full_nba_schedule
)
from config import CATEGORIES, NEGATIVE_CATEGORIES
from basketball_reference import (
    fetch_all_nba_season_averages, 
    get_player_stats_by_name,
    convert_to_yahoo_stat_ids
)


# ==================== EXCEPTIONS ====================

class PlayoffWeekError(Exception):
    """Exception raised when trying to predict a playoff week without a known opponent"""
    def __init__(self, week: int, message: str = None):
        self.week = week
        self.message = message or f"שבוע {week} הוא שבוע פלייאוף - היריב עדיין לא נקבע"
        super().__init__(self.message)


# ==================== CONFIGURATION ====================

# Active fantasy roster positions (count for projections)
ACTIVE_POSITIONS = ['PG', 'SG', 'G', 'SF', 'PF', 'F', 'C', 'UTIL']

# Bench position - counts only if <= 10 players have games
BENCH_POSITION = 'BN'

# Inactive positions (never count)
INACTIVE_POSITIONS = ['IL', 'IL+']

# Maximum daily starters (if more players available, use only starting positions)
MAX_DAILY_STARTERS = 10

# Injury status adjustments (probability multiplier)
# Based on historical NBA injury tag play rates and fantasy community consensus
# These percentages represent the likelihood a player will actually play
INJURY_COUNT = {
    'Probable': 0.85,   # 85% - likely to play but some risk (most do play)
    'P': 0.85,
    'Questionable': 0.45,  # 45% - uncertain, less than 50/50 (conservative estimate)
    'Q': 0.45,
    'GTD': 0.45,        # Game-Time Decision - 45% (won't know until game time)
    'DTD': 0.65,        # Day-to-Day - 65% (recovering, decent chance)
    '': 1.0,            # Healthy
    'Healthy': 1.0,
}

# Injury status that should NOT be counted (0%)
INJURY_SKIP = {
    'Doubtful': 0.0,
    'D': 0.0,
    'Out': 0.0,
    'O': 0.0,
    'INJ': 0.0,
    'SUSP': 0.0,
    'IL': 0.0,
    'IL+': 0.0,
}


@dataclass
class PlayerProjection:
    """Projected stats for a player for the week"""
    player_key: str
    name: str
    team: str
    position: str
    roster_position: str  # The slot in fantasy roster (IL, IL+, BN, PG, etc.)
    status: str
    injury_note: str
    games_this_week: int
    avg_stats: Dict[str, float]
    projected_stats: Dict[str, float]
    injury_adjustment: float  # 1.0 = healthy, 0.0 = out
    is_on_il: bool  # True if player is in IL or IL+ slot
    game_today: Optional[Dict] = None  # Today's game info: {opponent, time_israel, is_home}
    weekly_schedule: Optional[List[Dict]] = None  # Full week schedule with daily games


@dataclass
class TeamProjection:
    """Projected stats for a team for the week"""
    team_key: str
    team_name: str
    players: List[PlayerProjection]
    total_projected: Dict[str, float]
    remaining_games: int = 0  # Games remaining (calculated accurately from active roster)


@dataclass
class MatchupPrediction:
    """Complete matchup prediction"""
    week: int
    my_team: TeamProjection
    opponent: TeamProjection
    category_winners: Dict[str, str]  # category -> 'my_team' or 'opponent'
    predicted_score: Tuple[int, int]  # (my_wins, opponent_wins)
    confidence: Dict[str, float]  # confidence level per category
    is_past_week: bool = False  # True if this is a completed week (actual results)
    # Initial projections from start of week (for comparison)
    initial_my_projected: Optional[Dict[str, float]] = None
    initial_opponent_projected: Optional[Dict[str, float]] = None
    # Current actual stats (accumulated so far this week)
    actual_my_stats: Optional[Dict[str, float]] = None
    actual_opponent_stats: Optional[Dict[str, float]] = None


def get_injury_factor(status: str) -> float:
    """Get injury factor for a player status.
    Returns probability multiplier (0.0 to 1.0):
    - 1.0 = healthy (100%)
    - 0.9 = probable (90%)
    - 0.5 = questionable/GTD (50%)
    - 0.0 = out/doubtful (0%)
    """
    status = status.strip() if status else ''
    
    # Check if should be skipped (0%)
    if status in INJURY_SKIP:
        return 0.0
    
    # Check if has probability multiplier
    if status in INJURY_COUNT:
        return INJURY_COUNT[status]
    
    # Default: count unknown statuses as healthy
    return 1.0


class FantasyPredictor:
    """Predicts fantasy basketball matchup outcomes"""
    
    # Legacy injury adjustments (kept for compatibility)
    # NOTE: These match the updated INJURY_COUNT values
    INJURY_ADJUSTMENTS = {
        'INJ': 0.0,      # Injured - out
        'O': 0.0,        # Out
        'Out': 0.0,
        'SUSP': 0.0,     # Suspended
        'IL': 0.0,       # Injured List
        'IL+': 0.0,      # Extended IL
        'D': 0.0,        # Doubtful
        'Doubtful': 0.0,
        'DTD': 0.65,     # Day-to-Day - 65%
        'GTD': 0.45,     # Game-Time Decision - 45%
        'Q': 0.45,       # Questionable - 45%
        'Questionable': 0.45,
        'P': 0.85,       # Probable - 85%
        'Probable': 0.85,
        '': 1.0,         # Healthy
        'Healthy': 1.0,
    }
    
    # Standard 9-CAT stat IDs
    STAT_CATEGORIES = {
        'FG%': '5',
        'FT%': '8',
        '3PTM': '10',
        'PTS': '12',
        'REB': '15',
        'AST': '16',
        'STL': '17',
        'BLK': '18',
        'TO': '19',
    }
    
    # Counting stats (sum over games) vs rate stats (average)
    COUNTING_STATS = ['3PTM', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'TO']
    RATE_STATS = ['FG%', 'FT%']
    
    def __init__(self):
        self.api = api
        self.schedule = schedule
    
    def predict_matchup(self, league_key: str, week: int = None, current_week: int = None,
                       yahoo_remaining_my_team: Optional[int] = None) -> MatchupPrediction:
        """Generate full matchup prediction based on 30-day averages.
        
        For PAST weeks (week < current_week): Shows actual results from Yahoo
        For CURRENT/FUTURE weeks: Generates predictions
        
        Algorithm for predictions:
        - For past days (week_start to yesterday): 30-day avg × games played
        - For remaining days (today to week_end): 30-day avg × games remaining
        - Total = past + remaining (no reliance on Yahoo's matchup stats)
        """
        
        # Get my team
        my_team_info = self.api.get_my_team(league_key)
        if not my_team_info:
            raise Exception("Could not find your team in this league")
        
        # Get current matchup to find opponent
        matchup = self.api.get_matchup(my_team_info['team_key'], week)
        if not matchup or not matchup.get('opponent'):
            # This is likely a playoff week where the opponent hasn't been determined yet
            raise PlayoffWeekError(week or 0)
        
        week_num = int(matchup.get('week', 0))
        
        # Get week dates from Yahoo (handles double weeks automatically!)
        week_start_str = matchup.get('week_start')
        week_end_str = matchup.get('week_end')
        
        print(f"[DEBUG] Yahoo week dates: start={week_start_str}, end={week_end_str}")
        
        # Determine if this is a past week (completed)
        is_past_week = current_week is not None and week_num < current_week
        
        # Get rosters (needed for both past and current weeks)
        my_roster = self.api.get_team_roster(my_team_info['team_key'], week)
        opponent_roster = self.api.get_team_roster(matchup['opponent']['team_key'], week)
        
        # Get seasonal averages from Basketball Reference (more reliable than Yahoo)
        print("[DEBUG] Fetching seasonal averages from Basketball Reference...")
        bbref_stats = fetch_all_nba_season_averages()
        print(f"[DEBUG] Got {len(bbref_stats)} player averages from Basketball Reference")
        
        # Collect all player keys for batch fetching from Yahoo
        all_player_keys = []
        for player in my_roster + opponent_roster:
            all_player_keys.append(player['player_key'])
        
        # Get season stats from Yahoo for all players (fallback if BBRef fails)
        print("[DEBUG] Fetching season stats from Yahoo API as fallback...")
        yahoo_season_stats = self.api.get_player_stats_averages(all_player_keys)
        print(f"[DEBUG] Got season stats for {len(yahoo_season_stats)} players from Yahoo")
        
        # Build player averages dictionary using Basketball Reference data
        player_averages = {}
        for player in my_roster + opponent_roster:
            player_key = player['player_key']
            player_name = player.get('name', '')
            
            # Try to get stats from Basketball Reference first
            bbref_player_stats = get_player_stats_by_name(player_name)
            
            if bbref_player_stats:
                # Convert to Yahoo stat ID format
                player_averages[player_key] = convert_to_yahoo_stat_ids(bbref_player_stats)
                try:
                    print(f"[DEBUG] {player_name}: Using BBRef seasonal avg (PTS: {bbref_player_stats.get('PTS', 0):.1f}/game)")
                except UnicodeEncodeError:
                    print(f"[DEBUG] Player {player_key}: Using BBRef seasonal avg (PTS: {bbref_player_stats.get('PTS', 0):.1f}/game)")
            elif player_key in yahoo_season_stats:
                # Fallback to Yahoo season stats
                player_averages[player_key] = yahoo_season_stats[player_key]
                pts = yahoo_season_stats[player_key].get('12', yahoo_season_stats[player_key].get(12, 0))
                try:
                    print(f"[DEBUG] {player_name}: Using Yahoo season stats (PTS stat_id=12: {pts})")
                except UnicodeEncodeError:
                    print(f"[DEBUG] Player {player_key}: Using Yahoo season stats (PTS stat_id=12: {pts})")
            elif player.get('stats'):
                # Last fallback: roster stats (might be empty for future weeks)
                player_averages[player_key] = player['stats']
                try:
                    print(f"[DEBUG] {player_name}: Using Yahoo roster stats (last resort)")
                except UnicodeEncodeError:
                    print(f"[DEBUG] Player {player_key}: Using Yahoo roster stats (last resort)")
            else:
                try:
                    print(f"[DEBUG] {player_name}: WARNING - No stats available!")
                except UnicodeEncodeError:
                    print(f"[DEBUG] Player {player_key}: WARNING - No stats available!")
        
        print(f"[DEBUG] Got averages for {len(player_averages)} players")
        
        # Use week dates from Yahoo if available, otherwise calculate
        week_end_for_projection = None
        if week_start_str:
            try:
                week_start_for_projection = datetime.strptime(week_start_str, '%Y-%m-%d')
                if week_end_str:
                    week_end_for_projection = datetime.strptime(week_end_str, '%Y-%m-%d')
                print(f"[DEBUG] Using Yahoo week_start: {week_start_for_projection}, week_end: {week_end_for_projection}")
            except:
                # Fallback to calculation
                week_end_for_projection = None
                if current_week:
                    current_week_start, _ = get_week_dates_range()
                    week_offset = week_num - current_week
                    week_start_for_projection = current_week_start + timedelta(weeks=week_offset)
                else:
                    week_start_for_projection, _ = get_week_dates_range()
        else:
            # No week dates from Yahoo, calculate based on offset
            week_end_for_projection = None
            if current_week:
                current_week_start, _ = get_week_dates_range()
                week_offset = week_num - current_week
                week_start_for_projection = current_week_start + timedelta(weeks=week_offset)
            else:
                week_start_for_projection, _ = get_week_dates_range()
        
        # Calculate initial projections (full week, as if from start of week; supports double weeks)
        initial_my_projected = self._calculate_initial_projection(my_roster, player_averages, week_start_for_projection, week_end_for_projection)
        initial_opponent_projected = self._calculate_initial_projection(opponent_roster, player_averages, week_start_for_projection, week_end_for_projection)
        
        # For past weeks, return actual results with initial projections for comparison
        if is_past_week:
            return self._get_past_week_results(
                matchup, my_team_info, week_num,
                initial_my_projected, initial_opponent_projected
            )
        
        # Get actual stats from Yahoo matchup (accumulated stats for the week so far)
        my_actual_stats = matchup.get('my_team', {}).get('stats', {})
        opponent_actual_stats = matchup.get('opponent', {}).get('stats', {})
        
        print(f"[DEBUG] My team actual stats from Yahoo: {my_actual_stats}")
        print(f"[DEBUG] Opponent actual stats from Yahoo: {opponent_actual_stats}")
        
        # Acquisition dates for both teams (from transactions) so we don't count players before they were added
        acquisition_dates_my = {}
        acquisition_dates_opponent = {}
        il_placements_my = {}
        il_removals_my = {}
        il_placements_opponent = {}
        il_removals_opponent = {}
        
        if week_start_str and week_end_str:
            try:
                ws_dt = datetime.strptime(week_start_str, '%Y-%m-%d')
                we_dt = datetime.strptime(week_end_str, '%Y-%m-%d')
                acquisition_dates_my = self.api.get_acquisition_dates_for_team(
                    league_key, my_team_info['team_key'], ws_dt, we_dt
                )
                acquisition_dates_opponent = self.api.get_acquisition_dates_for_team(
                    league_key, matchup['opponent']['team_key'], ws_dt, we_dt
                )
                # Get IL placement/removal history for both teams
                il_placements_my, il_removals_my = self.api.get_il_history_for_team(
                    league_key, my_team_info['team_key'], ws_dt, we_dt
                )
                il_placements_opponent, il_removals_opponent = self.api.get_il_history_for_team(
                    league_key, matchup['opponent']['team_key'], ws_dt, we_dt
                )
            except (ValueError, TypeError):
                pass
        
        # Convert actual stats to category names for display
        my_actual_converted = self._convert_stats_to_categories(my_actual_stats)
        opponent_actual_converted = self._convert_stats_to_categories(opponent_actual_stats)
        
        # Project each team: actual stats (past) + projected stats (remaining)
        my_projection = self._project_team_with_actuals(
            my_team_info['team_key'],
            my_team_info['name'],
            my_roster,
            player_averages,
            my_actual_stats,  # Pass actual stats for past days
            week_num,  # Pass week number for correct date calculation
            current_week,  # Pass current week for offset calculation
            week_start_str,  # Pass Yahoo week_start for double weeks
            week_end_str,  # Pass Yahoo week_end for double weeks
            acquisition_dates=acquisition_dates_my,
            yahoo_remaining_override=yahoo_remaining_my_team,
            il_placements=il_placements_my,
            il_removals=il_removals_my,
        )
        
        opponent_projection = self._project_team_with_actuals(
            matchup['opponent']['team_key'],
            matchup['opponent']['name'],
            opponent_roster,
            player_averages,
            opponent_actual_stats,  # Pass actual stats for past days
            week_num,  # Pass week number for correct date calculation
            current_week,  # Pass current week for offset calculation
            week_start_str,  # Pass Yahoo week_start for double weeks
            week_end_str,  # Pass Yahoo week_end for double weeks
            acquisition_dates=acquisition_dates_opponent,
            il_placements=il_placements_opponent,
            il_removals=il_removals_opponent,
        )
        
        # Compare projections
        category_winners, predicted_score, confidence = self._compare_projections(
            my_projection, opponent_projection
        )
        
        return MatchupPrediction(
            week=week_num,
            my_team=my_projection,
            opponent=opponent_projection,
            category_winners=category_winners,
            predicted_score=predicted_score,
            confidence=confidence,
            initial_my_projected=initial_my_projected,
            initial_opponent_projected=initial_opponent_projected,
            actual_my_stats=my_actual_converted,
            actual_opponent_stats=opponent_actual_converted
        )
    
    def predict_all_matchups(self, league_key: str, week: int = None, current_week: int = None) -> List[Dict]:
        """Generate predictions for all matchups in the league.
        
        Returns a list of matchup predictions with team names, scores, and winners.
        """
        # Check cache first
        cache_key = f"all_matchups:{league_key}:{week}"
        cached = _get_prediction_cached(cache_key)
        if cached is not None:
            print(f"[DEBUG] Using cached predictions for {league_key} week {week}")
            return cached
        
        # Get all matchups from scoreboard
        matchups = self.api.get_league_scoreboard(league_key, week)
        
        if not matchups:
            # Check if this is a playoff week without matchups set yet
            leagues = self.api.get_user_leagues()
            league_info = next((l for l in leagues if l['league_key'] == league_key), None)
            if league_info:
                playoff_start_week = int(league_info.get('playoff_start_week', 0)) if league_info.get('playoff_start_week') else None
                if playoff_start_week and week and week >= playoff_start_week:
                    raise PlayoffWeekError(week, f"שבוע {week} הוא שבוע פלייאוף - הזיווגים עדיין לא נקבעו")
            return []
        
        week_num = int(matchups[0].get('week', 0)) if matchups else 0
        
        # Use week_num as current_week if not provided
        if current_week is None:
            current_week = week_num
        
        # Fetch all rosters and player averages at once for efficiency
        all_team_keys = []
        for matchup in matchups:
            for team in matchup.get('teams', []):
                all_team_keys.append(team['team_key'])
        
        # Get rosters for all teams IN PARALLEL (much faster!)
        print(f"[DEBUG] Fetching rosters for {len(all_team_keys)} teams in parallel...")
        all_rosters = {}
        
        def fetch_roster(team_key):
            return team_key, self.api.get_team_roster(team_key, week)
        
        # Use ThreadPoolExecutor for parallel fetching
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_roster, tk): tk for tk in all_team_keys}
            for future in as_completed(futures):
                try:
                    team_key, roster = future.result()
                    all_rosters[team_key] = roster
                except Exception as e:
                    print(f"[DEBUG] Error fetching roster: {e}")
        
        print(f"[DEBUG] Fetched {len(all_rosters)} rosters")
        
        # Get all player keys
        all_player_keys = []
        for roster in all_rosters.values():
            for player in roster:
                all_player_keys.append(player['player_key'])
        
        # Get seasonal averages from Basketball Reference
        print("[DEBUG] Fetching seasonal averages from Basketball Reference...")
        bbref_stats = fetch_all_nba_season_averages()
        print(f"[DEBUG] Got {len(bbref_stats)} player averages from Basketball Reference")
        
        # Get season stats from Yahoo for all players (fallback if BBRef fails)
        print("[DEBUG] Fetching season stats from Yahoo API as fallback...")
        yahoo_season_stats = self.api.get_player_stats_averages(all_player_keys)
        print(f"[DEBUG] Got season stats for {len(yahoo_season_stats)} players from Yahoo")
        
        # Build player averages dictionary
        player_averages = {}
        for roster in all_rosters.values():
            for player in roster:
                player_key = player['player_key']
                player_name = player.get('name', '')
                
                bbref_player_stats = get_player_stats_by_name(player_name)
                
                if bbref_player_stats:
                    player_averages[player_key] = convert_to_yahoo_stat_ids(bbref_player_stats)
                elif player_key in yahoo_season_stats:
                    # Fallback to Yahoo season stats
                    player_averages[player_key] = yahoo_season_stats[player_key]
                elif player.get('stats'):
                    # Last fallback: roster stats
                    player_averages[player_key] = player['stats']
        
        # Predict each matchup
        predictions = []
        for matchup in matchups:
            teams = matchup.get('teams', [])
            if len(teams) != 2:
                continue
            
            team1 = teams[0]
            team2 = teams[1]
            
            # Get week dates from matchup (handles double weeks)
            yahoo_week_start = matchup.get('week_start')
            yahoo_week_end = matchup.get('week_end')
            
            # Acquisition dates per team (from transactions) - so we don't count players before they were added
            acquisition_dates_team1 = {}
            acquisition_dates_team2 = {}
            il_placements_team1 = {}
            il_removals_team1 = {}
            il_placements_team2 = {}
            il_removals_team2 = {}
            
            if yahoo_week_start and yahoo_week_end:
                try:
                    ws_dt = datetime.strptime(yahoo_week_start, '%Y-%m-%d')
                    we_dt = datetime.strptime(yahoo_week_end, '%Y-%m-%d')
                    acquisition_dates_team1 = self.api.get_acquisition_dates_for_team(
                        league_key, team1['team_key'], ws_dt, we_dt
                    )
                    acquisition_dates_team2 = self.api.get_acquisition_dates_for_team(
                        league_key, team2['team_key'], ws_dt, we_dt
                    )
                    # Get IL placement/removal history for both teams
                    il_placements_team1, il_removals_team1 = self.api.get_il_history_for_team(
                        league_key, team1['team_key'], ws_dt, we_dt
                    )
                    il_placements_team2, il_removals_team2 = self.api.get_il_history_for_team(
                        league_key, team2['team_key'], ws_dt, we_dt
                    )
                except (ValueError, TypeError):
                    pass
            
            # Project each team
            team1_projection = self._project_team_with_actuals(
                team1['team_key'],
                team1['name'],
                all_rosters.get(team1['team_key'], []),
                player_averages,
                team1.get('stats', {}),
                week_num,  # Pass week number for correct date calculation
                current_week,  # Pass current week for offset calculation
                yahoo_week_start,  # Pass Yahoo week_start for double weeks
                yahoo_week_end,  # Pass Yahoo week_end for double weeks
                acquisition_dates=acquisition_dates_team1,
                il_placements=il_placements_team1,
                il_removals=il_removals_team1,
            )
            
            team2_projection = self._project_team_with_actuals(
                team2['team_key'],
                team2['name'],
                all_rosters.get(team2['team_key'], []),
                player_averages,
                team2.get('stats', {}),
                week_num,  # Pass week number for correct date calculation
                current_week,  # Pass current week for offset calculation
                yahoo_week_start,  # Pass Yahoo week_start for double weeks
                yahoo_week_end,  # Pass Yahoo week_end for double weeks
                acquisition_dates=acquisition_dates_team2,
                il_placements=il_placements_team2,
                il_removals=il_removals_team2,
            )
            
            # Compare projections
            category_winners, predicted_score, confidence = self._compare_projections(
                team1_projection, team2_projection
            )
            
            # Determine overall winner
            team1_wins = predicted_score[0]
            team2_wins = predicted_score[1]
            
            if team1_wins > team2_wins:
                winner = team1['name']
                winner_key = team1['team_key']
            elif team2_wins > team1_wins:
                winner = team2['name']
                winner_key = team2['team_key']
            else:
                winner = "Tie"
                winner_key = None
            
            predictions.append({
                'week': week_num,
                'team1': {
                    'key': team1['team_key'],
                    'name': team1['name'],
                    'manager': team1.get('manager', ''),
                    'projected': team1_projection.total_projected,
                    'wins': team1_wins,
                    'remaining_games': team1_projection.remaining_games
                },
                'team2': {
                    'key': team2['team_key'],
                    'name': team2['name'],
                    'manager': team2.get('manager', ''),
                    'projected': team2_projection.total_projected,
                    'wins': team2_wins,
                    'remaining_games': team2_projection.remaining_games
                },
                'category_winners': category_winners,
                'predicted_score': f"{team1_wins}-{team2_wins}",
                'winner': winner,
                'winner_key': winner_key
            })
        
        # Cache the result
        _set_prediction_cached(cache_key, predictions)
        return predictions
    
    def _get_past_week_results(self, matchup: Dict, my_team_info: Dict, week_num: int,
                                initial_my_projected: Dict[str, float] = None,
                                initial_opponent_projected: Dict[str, float] = None) -> MatchupPrediction:
        """Get actual results for a completed past week from Yahoo matchup data."""
        
        # Get actual stats from Yahoo matchup
        my_stats = matchup.get('my_team', {}).get('stats', {})
        opponent_stats = matchup.get('opponent', {}).get('stats', {})
        
        print(f"[DEBUG] Past week {week_num} - My actual stats: {my_stats}")
        print(f"[DEBUG] Past week {week_num} - Opponent actual stats: {opponent_stats}")
        
        # Convert stat IDs to category names
        my_totals = {}
        opponent_totals = {}
        
        for cat_name, stat_id in self.STAT_CATEGORIES.items():
            my_val = my_stats.get(stat_id) or my_stats.get(int(stat_id)) or my_stats.get(str(stat_id)) or 0
            opp_val = opponent_stats.get(stat_id) or opponent_stats.get(int(stat_id)) or opponent_stats.get(str(stat_id)) or 0
            
            try:
                my_val = float(my_val)
                opp_val = float(opp_val)
                
                # Convert decimal percentages to regular percentages (0.485 -> 48.5)
                if cat_name in ['FG%', 'FT%']:
                    if 0 < my_val < 1:
                        my_val = my_val * 100
                    if 0 < opp_val < 1:
                        opp_val = opp_val * 100
                
                my_totals[cat_name] = my_val
                opponent_totals[cat_name] = opp_val
            except:
                my_totals[cat_name] = 0
                opponent_totals[cat_name] = 0
        
        # Create team projections with actual stats (empty player list for past weeks)
        my_projection = TeamProjection(
            team_key=my_team_info['team_key'],
            team_name=my_team_info['name'],
            players=[],  # No player details for past weeks
            total_projected=my_totals
        )
        
        opponent_projection = TeamProjection(
            team_key=matchup['opponent']['team_key'],
            team_name=matchup['opponent']['name'],
            players=[],  # No player details for past weeks
            total_projected=opponent_totals
        )
        
        # Compare actual results
        category_winners, final_score, confidence = self._compare_projections(
            my_projection, opponent_projection
        )
        
        return MatchupPrediction(
            week=week_num,
            my_team=my_projection,
            opponent=opponent_projection,
            category_winners=category_winners,
            predicted_score=final_score,
            confidence=confidence,
            is_past_week=True,  # Mark as past week
            initial_my_projected=initial_my_projected,
            initial_opponent_projected=initial_opponent_projected,
            actual_my_stats=my_totals,  # For past weeks, actual = final
            actual_opponent_stats=opponent_totals
        )
    
    def _calculate_initial_projection(self, roster: List[Dict], player_averages: Dict, week_start: datetime = None, week_end: datetime = None) -> Dict[str, float]:
        """Calculate pure initial projection for the entire week (as if from start of week).
        
        This gives us what the projection would be if no games were played yet,
        useful for comparing prediction vs actual results.
        """
        totals = {cat: 0.0 for cat in self.STAT_CATEGORIES.keys()}
        total_fga = 0.0
        total_fgm = 0.0
        total_fta = 0.0
        total_ftm = 0.0
        
        for player in roster:
            # Get roster position
            roster_position = player.get('roster_position', '') or player.get('selected_position', '')
            is_on_il = roster_position in INACTIVE_POSITIONS
            
            # Skip IL players
            if is_on_il:
                continue
            
            # Get injury factor
            status = player.get('status', '')
            injury_factor = get_injury_factor(status)
            if injury_factor == 0:
                continue
            
            # Get games this week for player's team (from weekly schedule) for the REQUESTED week (supports double weeks)
            team_abbr = player.get('team', '')
            weekly_sched = get_team_weekly_schedule(team_abbr, week_start, week_end) if team_abbr else []
            games = sum(1 for day in weekly_sched if day.get('has_game')) if weekly_sched else 3
            
            # Get player averages
            avg_stats = player_averages.get(player['player_key'], player.get('stats', {}))
            
            # Get games played for per-game calculation
            games_played = avg_stats.get('0') or avg_stats.get(0) or 1
            try:
                games_played = float(games_played) if games_played > 0 else 1
            except:
                games_played = 1
            
            is_average = avg_stats.get('_is_average', False)
            
            # Add counting stats
            for cat in self.COUNTING_STATS:
                stat_id = self.STAT_CATEGORIES[cat]
                raw_value = avg_stats.get(stat_id) or avg_stats.get(int(stat_id)) or avg_stats.get(str(stat_id)) or 0
                try:
                    raw_value = float(raw_value)
                except:
                    raw_value = 0
                
                if is_average:
                    per_game = raw_value
                else:
                    per_game = raw_value / games_played
                
                totals[cat] += per_game * games * injury_factor
            
            # Track FG/FT data for percentage calculation
            pts = avg_stats.get('12') or avg_stats.get(12) or 0
            try:
                pts = float(pts) / (1 if is_average else games_played)
            except:
                pts = 0
            
            fg_pct = avg_stats.get('5') or avg_stats.get(5) or 0.45
            ft_pct = avg_stats.get('8') or avg_stats.get(8) or 0.75
            try:
                fg_pct = float(fg_pct)
                if fg_pct > 1:
                    fg_pct = fg_pct / 100
            except:
                fg_pct = 0.45
            try:
                ft_pct = float(ft_pct)
                if ft_pct > 1:
                    ft_pct = ft_pct / 100
            except:
                ft_pct = 0.75
            
            # Estimate attempts based on points per game
            est_fga = pts / 2.1 if pts > 0 else 8
            est_fta = pts / 6 if pts > 0 else 3
            
            total_fga += est_fga * games * injury_factor
            total_fgm += est_fga * fg_pct * games * injury_factor
            total_fta += est_fta * games * injury_factor
            total_ftm += est_fta * ft_pct * games * injury_factor
        
        # Calculate team percentage stats
        totals['FG%'] = (total_fgm / total_fga * 100) if total_fga > 0 else 0
        totals['FT%'] = (total_ftm / total_fta * 100) if total_fta > 0 else 0
        
        return totals
    
    def _project_team_with_actuals(self, team_key: str, team_name: str, 
                                    roster: List[Dict], player_averages: Dict,
                                    actual_stats: Dict = None,
                                    week_num: int = None,
                                    current_week: int = None,
                                    yahoo_week_start: str = None,
                                    yahoo_week_end: str = None,
                                    acquisition_dates: Dict = None,
                                    yahoo_remaining_override: Optional[int] = None,
                                    il_placements: Optional[Dict[str, datetime]] = None,
                                    il_removals: Optional[Dict[str, datetime]] = None) -> TeamProjection:
        """Project team stats combining actual results with projections.
        
        Algorithm:
        1. Split the week into past days (already played) and future days (remaining)
        2. For past days: USE ACTUAL STATS from Yahoo matchup (real results!)
        3. For future days: project using per-game averages × games remaining
        4. Total = actual (past) + projected (remaining)
        
        Rules:
        - Only count players in ACTIVE positions + BENCH (if <= 10 players have games)
        - Apply injury rules: Probable/Questionable = count, Doubtful/Out = skip
        - If <= 10 eligible players on a day, include bench
        - If > 10 eligible players, exclude bench (Yahoo "Start Active Players" logic)
        """
        
        # Yahoo Fantasy uses Pacific Time (PST/PDT) for determining game dates
        # Use Pacific Time to match Yahoo's logic for past vs future games
        today = datetime.now()
        today_pst = get_pacific_time()
        today_date_pst = get_pacific_date()
        
        is_dst = 3 <= today.month <= 10  # Approximate DST period
        
        print(f"[DEBUG] Server time (Israel): {today}")
        print(f"[DEBUG] Pacific time: {today_pst} ({'PDT' if is_dst else 'PST'})")
        print(f"[DEBUG] Current Pacific date: {today_date_pst.date()}")
        
        # Use Yahoo week dates if available (handles double weeks!)
        if yahoo_week_start and yahoo_week_end:
            try:
                week_start = datetime.strptime(yahoo_week_start, '%Y-%m-%d')
                week_end = datetime.strptime(yahoo_week_end, '%Y-%m-%d')
                week_end = week_end.replace(hour=23, minute=59, second=59)
                print(f"[DEBUG] Using Yahoo week dates: {week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}")
                print(f"[DEBUG] Week duration: {(week_end - week_start).days + 1} days")
            except:
                yahoo_week_start = None
                yahoo_week_end = None
        
        # Fallback: Calculate week_start and week_end for the REQUESTED week
        if not yahoo_week_start or not yahoo_week_end:
            if week_num and current_week:
                # Get current week dates first
                current_week_start, current_week_end = get_week_dates_range()
                
                # Calculate offset in weeks
                week_offset = week_num - current_week
                
                # Calculate the requested week's dates (7 days)
                week_start = current_week_start + timedelta(weeks=week_offset)
                week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
                
                print(f"[DEBUG] Calculated week {week_num}, current week {current_week}, offset: {week_offset} weeks")
                print(f"[DEBUG] Week dates: {week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}")
            else:
                # Fallback: use current week
                week_start, week_end = get_week_dates_range()
                print(f"[DEBUG] Using current week dates (fallback)")
        
        # Get today's games for all teams (for display)
        todays_games = get_todays_games()
        
        # Split the week into past days and remaining days (using Pacific Time)
        past_days = []  # Days that have already passed in Pacific Time
        remaining_days = []  # Days remaining in Pacific Time (today onwards)
        
        current_day = week_start
        while current_day <= week_end:
            # Compare dates using Pacific Time
            if current_day < today_date_pst:
                past_days.append(current_day)
            else:
                remaining_days.append(current_day)
            current_day += timedelta(days=1)
        
        print(f"[DEBUG] Past days: {len(past_days)}, Remaining days: {len(remaining_days)}")
        print(f"[DEBUG] Past dates: {[d.strftime('%Y-%m-%d') for d in past_days]}")
        print(f"[DEBUG] Remaining dates: {[d.strftime('%Y-%m-%d') for d in remaining_days]}")
        
        # Build player info with positions and injury status
        acquisition_dates = acquisition_dates or {}
        player_info = []
        for player in roster:
            team_abbr = player.get('team', '')
            # Get weekly schedule (single source of truth for games count) for the REQUESTED week (supports double weeks)
            weekly_sched = get_team_weekly_schedule(team_abbr, week_start, week_end) if team_abbr else []
            total_games = sum(1 for day in weekly_sched if day.get('has_game')) if weekly_sched else 3
            
            roster_position = player.get('roster_position', '') or player.get('selected_position', '')
            is_on_il = roster_position in INACTIVE_POSITIONS
            
            status = player.get('status', '')
            injury_factor = get_injury_factor(status)
            
            # If in IL/IL+ slot, always skip
            if is_on_il:
                injury_factor = 0.0
            
            avg_stats = player_averages.get(player['player_key'], player.get('stats', {}))
            
            # Get today's game info for this player's team
            normalized_team = schedule._normalize_team_abbr(team_abbr) if team_abbr else ''
            game_today = todays_games.get(normalized_team) or todays_games.get(team_abbr)
            
            # Acquisition date: don't count this player on days before he was added (from transactions)
            acq = acquisition_dates.get(player['player_key'])
            acq_date = acq.date() if hasattr(acq, 'date') else (acq if isinstance(acq, date) else None)
            
            player_info.append({
                'player_key': player['player_key'],
                'name': player.get('name', 'Unknown'),
                'team_abbr': team_abbr,
                'normalized_team': normalized_team,
                'position': player.get('position', ''),
                'roster_position': roster_position,
                'status': status,
                'injury_note': player.get('injury_note', ''),
                'injury_factor': injury_factor,
                'is_on_il': is_on_il,
                'avg_stats': avg_stats,
                'total_games': total_games,
                'games_counted': 0,  # Will be counted day by day
                'game_today': game_today,
                'acquisition_date': acq_date,  # None = was on roster all week
            })
        
        # Helper function to calculate stats for a list of days (day-by-day approach)
        def calculate_daily_stats(
            days_list: List[datetime], 
            include_il_players: bool = False,
            il_placements: Optional[Dict[str, datetime]] = None,
            il_removals: Optional[Dict[str, datetime]] = None
        ) -> Tuple[Dict[str, float], Dict[str, float], int]:
            """Calculate stats for a list of days using NBA Official API schedule data.
            Returns: (stats, fg_data, games_counted)
            
            Args:
                days_list: List of days to calculate
                include_il_players: if True, count IL/IL+ players too (for past days)
                il_placements: {player_key: placement_date} when player was moved to IL
                il_removals: {player_key: removal_date} when player was removed from IL
            """
            stats = {cat: 0.0 for cat in self.COUNTING_STATS}
            fg_data = {'fgm': 0.0, 'fga': 0.0, 'ftm': 0.0, 'fta': 0.0}
            games_counted = 0  # Track games using Yahoo's 10/day limit
            
            il_placements = il_placements or {}
            il_removals = il_removals or {}
            
            # Fetch full NBA schedule once (same source as visual schedule display)
            full_schedule = get_full_nba_schedule()
            print(f"[DEBUG] Using NBA Official API schedule with {len(full_schedule)} dates")
            
            for day in days_list:
                # Get teams playing on this day from NBA Official API
                day_str = day.strftime('%Y-%m-%d')
                
                # Check if we have data for this day
                if day_str not in full_schedule:
                    print(f"[DEBUG] No schedule data for {day_str}, skipping day (no estimation)")
                    continue
                
                # Get teams from the day's schedule
                teams_playing = list(full_schedule[day_str].keys()) if full_schedule[day_str] else []
                
                # If we have data but no games (e.g., All-Star break), count 0 games (skip day)
                if not teams_playing:
                    print(f"[DEBUG] Confirmed no games on {day_str} (e.g., All-Star break)")
                    continue
                
                # Filter to eligible players for this day (including bench; optionally IL for past days)
                eligible_players = []
                bench_players = []
                
                for p in player_info:
                    player_key = p['player_key']
                    day_date = day.date()
                    
                    # Check if player was on IL on this specific day
                    was_on_il_this_day = False
                    if player_key in il_placements:
                        placement_date = il_placements[player_key].date() if hasattr(il_placements[player_key], 'date') else il_placements[player_key]
                        # If placed on IL and not yet removed, or removal is after this day
                        if day_date >= placement_date:
                            # Check if removed from IL before this day
                            if player_key in il_removals:
                                removal_date = il_removals[player_key].date() if hasattr(il_removals[player_key], 'date') else il_removals[player_key]
                                if day_date < removal_date:
                                    was_on_il_this_day = True
                            else:
                                # Still on IL (no removal date)
                                was_on_il_this_day = True
                    
                    # Skip if player was on IL on this day (can't count games while on IL)
                    if was_on_il_this_day:
                        continue
                    
                    # Skip if injured (Out/Doubtful - didn't play), unless on IL and we're including IL for past days
                    if p['injury_factor'] == 0.0 and not (include_il_players and p['roster_position'] in INACTIVE_POSITIONS):
                        continue
                    
                    # Skip if on IL/IL+ slot, unless we're counting past days (include_il_players)
                    if not include_il_players and p['roster_position'] in INACTIVE_POSITIONS:
                        continue
                    
                    # Skip if player was added mid-week and this day is before his acquisition date
                    if p.get('acquisition_date') and day_date < p['acquisition_date']:
                        continue
                    
                    # Check if team plays today (we have schedule data for this day)
                    team_plays = (p['team_abbr'] in teams_playing or 
                                  p['normalized_team'] in teams_playing)
                    
                    if not team_plays:
                        continue
                    
                    # Separate bench players from starters
                    if p['roster_position'] == BENCH_POSITION:
                        bench_players.append(p)
                    else:
                        eligible_players.append(p)
                
                # Determine which players to count based on Yahoo's 10/day limit
                # New SAP Logic: Assume user will start ANY player with a game,
                # but Yahoo limits to max 10 players per day
                # Combine all players (starters + bench) and take first 10
                all_players_with_games = eligible_players + bench_players
                players_to_count = all_players_with_games[:MAX_DAILY_STARTERS]
                
                # Detailed logging
                day_str = day.strftime('%Y-%m-%d')
                print(f"[DEBUG] {day_str}: {len(eligible_players)} starters + {len(bench_players)} bench = {len(all_players_with_games)} total")
                # Count games for this day (Yahoo logic: max 10 per day)
                games_counted += len(players_to_count)
                
                # Log each player being counted
                for p in players_to_count:
                    pos = p['roster_position']
                    name = p['name']
                    team = p['team_abbr']
                    try:
                        print(f"[DEBUG]   -> {name} ({team}) [{pos}]")
                    except UnicodeEncodeError:
                        print(f"[DEBUG]   -> Player {p['player_key']} ({team}) [{pos}]")
                
                # Add stats for each player (1 game worth)
                for p in players_to_count:
                    avg_stats = p['avg_stats']
                    p['games_counted'] += 1
                    
                    # Get games played for per-game calculation
                    games_played = avg_stats.get('0') or avg_stats.get(0) or 1
                    try:
                        games_played = float(games_played) if games_played else 1
                        if games_played <= 0:
                            games_played = 1
                    except:
                        games_played = 1
                    
                    is_average = avg_stats.get('_is_average', False)
                    
                    # Add counting stats (1 game worth)
                    for cat in self.COUNTING_STATS:
                        stat_id = self.STAT_CATEGORIES[cat]
                        raw_value = avg_stats.get(stat_id) or avg_stats.get(int(stat_id)) or avg_stats.get(str(stat_id)) or 0
                        try:
                            raw_value = float(raw_value)
                        except:
                            raw_value = 0
                        
                        if is_average:
                            per_game = raw_value
                        else:
                            per_game = raw_value / games_played
                        
                        stats[cat] += per_game * p['injury_factor']
                    
                    # Track FG/FT data - use actual stats from Yahoo if available
                    # Stat IDs: '3'=FGA, '4'=FGM, '6'=FTA, '7'=FTM
                    fga_raw = avg_stats.get('3') or avg_stats.get(3)
                    fgm_raw = avg_stats.get('4') or avg_stats.get(4)
                    fta_raw = avg_stats.get('6') or avg_stats.get(6)
                    ftm_raw = avg_stats.get('7') or avg_stats.get(7)
                    
                    # Convert to per-game if needed
                    if fga_raw is not None and fgm_raw is not None:
                        try:
                            fga = float(fga_raw) / (1 if is_average else games_played)
                            fgm = float(fgm_raw) / (1 if is_average else games_played)
                        except:
                            fga, fgm = None, None
                    else:
                        fga, fgm = None, None
                    
                    if fta_raw is not None and ftm_raw is not None:
                        try:
                            fta = float(fta_raw) / (1 if is_average else games_played)
                            ftm = float(ftm_raw) / (1 if is_average else games_played)
                        except:
                            fta, ftm = None, None
                    else:
                        fta, ftm = None, None
                    
                    # Fallback to estimation if actual stats not available
                    if fga is None or fgm is None:
                        pts = avg_stats.get('12') or avg_stats.get(12) or 0
                        try:
                            pts = float(pts) / (1 if is_average else games_played)
                        except:
                            pts = 0
                        fg_pct = avg_stats.get('5') or avg_stats.get(5) or 0.45
                        try:
                            fg_pct = float(fg_pct)
                            if fg_pct > 1:
                                fg_pct = fg_pct / 100
                        except:
                            fg_pct = 0.45
                        fga = pts / 2.1 if pts > 0 else 8
                        fgm = fga * fg_pct
                    
                    if fta is None or ftm is None:
                        pts = avg_stats.get('12') or avg_stats.get(12) or 0
                        try:
                            pts = float(pts) / (1 if is_average else games_played)
                        except:
                            pts = 0
                        ft_pct = avg_stats.get('8') or avg_stats.get(8) or 0.75
                        try:
                            ft_pct = float(ft_pct)
                            if ft_pct > 1:
                                ft_pct = ft_pct / 100
                        except:
                            ft_pct = 0.75
                        fta = pts / 6 if pts > 0 else 3
                        ftm = fta * ft_pct
                    
                    fg_data['fgm'] += fgm * p['injury_factor']
                    fg_data['fga'] += fga * p['injury_factor']
                    fg_data['ftm'] += ftm * p['injury_factor']
                    fg_data['fta'] += fta * p['injury_factor']

            return stats, fg_data, games_counted
        
        # Calculate stats for past days (already played)
        past_stats, past_fg_data, past_games_counted_calc = calculate_daily_stats(
            past_days, 
            include_il_players=True,
            il_placements=il_placements,
            il_removals=il_removals
        )
        print(f"[DEBUG] Past days stats: {past_stats}")
        print(f"[DEBUG] Past days games counted (calculated from current roster): {past_games_counted_calc}")

        # Calculate stats for remaining days (projections)
        print(f"[DEBUG] ========== REMAINING GAMES CALCULATION ==========")
        print(f"[DEBUG] Remaining days: {len(remaining_days)} days")
        if remaining_days:
            print(f"[DEBUG] From: {remaining_days[0].strftime('%Y-%m-%d')} to {remaining_days[-1].strftime('%Y-%m-%d')}")
        remaining_stats, remaining_fg_data, remaining_games_counted = calculate_daily_stats(
            remaining_days, 
            include_il_players=False,
            il_placements=il_placements,
            il_removals=il_removals
        )
        print(f"[DEBUG] Remaining days stats (projected): {remaining_stats}")
        print(f"[DEBUG] Remaining days games (projected, Yahoo logic): {remaining_games_counted}")
        print(f"[DEBUG] ==================================================")

        # Calculate actual games played based on weekly schedule for past days
        # Count games from weekly_schedule for each player in past days
        # IMPORTANT: Include IL players for past days (they may have played before being moved to IL)
        schedule_games_played = 0
        for p in player_info:
            # Get weekly schedule for this player's team for the REQUESTED week (supports double weeks)
            team_abbr = p['team_abbr']
            weekly_sched = get_team_weekly_schedule(team_abbr, week_start, week_end) if team_abbr else []
            
            # Count games in past days (include IL players - they may have played before IL)
            for day in past_days:
                day_str = day.strftime('%Y-%m-%d')
                # Find this day in the weekly schedule
                for sched_day in weekly_sched:
                    if sched_day.get('date') == day_str and sched_day.get('has_game'):
                        schedule_games_played += 1
                        break
        
        print(f"[DEBUG] Games played from schedule (past {len(past_days)} days, includes IL): {schedule_games_played}")
        print(f"[DEBUG] Games counted with Yahoo logic (calculated): {past_games_counted_calc}")
        
        # Try to get actual games played from Yahoo (stat_id 0 = Games Played)
        yahoo_gp = None
        if actual_stats:
            raw_gp = actual_stats.get('0') or actual_stats.get(0)
            if raw_gp is None:
                for k, v in actual_stats.items():
                    if str(k).strip() == '0':
                        raw_gp = v
                        break
            if raw_gp is not None:
                try:
                    yahoo_gp = int(float(raw_gp))
                    print(f"[DEBUG] Yahoo GP from matchup stats (actual, includes roster changes): {yahoo_gp} vs Schedule count: {schedule_games_played}")
                except (TypeError, ValueError):
                    yahoo_gp = None
        
        # If Games Played not in matchup stats, fetch from team stats endpoint
        if yahoo_gp is None and week_num is not None:
            try:
                print(f"[DEBUG] Fetching team stats for {team_key} week {week_num} to get GP...")
                team_stats = self.api.get_team_stats(team_key, week_num)
                raw_gp = team_stats.get('0') or team_stats.get(0)
                if raw_gp is not None:
                    yahoo_gp = int(float(raw_gp))
                    print(f"[DEBUG] Yahoo GP from team stats (actual, includes roster changes): {yahoo_gp} vs Schedule count: {schedule_games_played}")
            except Exception as e:
                print(f"[DEBUG] Failed to fetch team stats for GP: {e}")
        
        print(f"[DEBUG] Remaining days games (projected, Yahoo logic): {remaining_games_counted}")
        
        # Only apply overrides/scaling if explicitly provided by user (for manual correction)
        scale_remaining = 1.0
        if yahoo_remaining_override is not None and remaining_games_counted > 0:
            scale_remaining = yahoo_remaining_override / remaining_games_counted
            print(f"[DEBUG] Using yahoo_remaining override: {yahoo_remaining_override}, scale={scale_remaining:.3f}")
        
        if scale_remaining != 1.0:
            for cat in self.COUNTING_STATS:
                remaining_stats[cat] = remaining_stats.get(cat, 0) * scale_remaining
            for k in remaining_fg_data:
                remaining_fg_data[k] = remaining_fg_data.get(k, 0) * scale_remaining
        
        # Use ACTUAL stats from Yahoo for past days if available
        # Only project remaining days
        if actual_stats and len(past_days) > 0:
            print(f"[DEBUG] Using ACTUAL stats from Yahoo for past {len(past_days)} days")
            
            # Map Yahoo stat IDs to category names
            stat_id_to_cat = {v: k for k, v in self.STAT_CATEGORIES.items()}
            
            # Extract actual counting stats from Yahoo
            actual_counting = {}
            for stat_id, value in actual_stats.items():
                cat_name = stat_id_to_cat.get(str(stat_id))
                if cat_name and cat_name in self.COUNTING_STATS:
                    actual_counting[cat_name] = float(value) if value else 0
            
            print(f"[DEBUG] Actual stats from Yahoo: {actual_counting}")
            
            # Combine: actual (past) + projected (remaining)
            daily_projections = {}
            for cat in self.COUNTING_STATS:
                actual_val = actual_counting.get(cat, 0)
                projected_val = remaining_stats.get(cat, 0)
                daily_projections[cat] = actual_val + projected_val
                print(f"[DEBUG] {cat}: actual={actual_val:.1f} + projected={projected_val:.1f} = {daily_projections[cat]:.1f}")
            
            # For FG%/FT%, use Yahoo actual data for past + projected for remaining
            # Yahoo provides FG% and FT% directly, we need to estimate FGM/FGA from actual
            actual_fg_pct = actual_stats.get('5', 0) or actual_stats.get(5, 0)
            actual_ft_pct = actual_stats.get('8', 0) or actual_stats.get(8, 0)
            actual_pts = actual_counting.get('PTS', 0)
            
            # Estimate past FGA/FTA from actual points and percentages
            if actual_fg_pct and actual_pts > 0:
                est_past_fga = actual_pts / 2.1
                est_past_fgm = est_past_fga * (actual_fg_pct / 100 if actual_fg_pct > 1 else actual_fg_pct)
            else:
                est_past_fga = past_fg_data['fga']
                est_past_fgm = past_fg_data['fgm']
            
            if actual_ft_pct and actual_pts > 0:
                est_past_fta = actual_pts / 6
                est_past_ftm = est_past_fta * (actual_ft_pct / 100 if actual_ft_pct > 1 else actual_ft_pct)
            else:
                est_past_fta = past_fg_data['fta']
                est_past_ftm = past_fg_data['ftm']
            
            daily_fg_data = {
                'fgm': est_past_fgm + remaining_fg_data['fgm'],
                'fga': est_past_fga + remaining_fg_data['fga'],
                'ftm': est_past_ftm + remaining_fg_data['ftm'],
                'fta': est_past_fta + remaining_fg_data['fta']
            }
        else:
            # No actual stats - use projections for everything (beginning of week)
            print(f"[DEBUG] No actual stats available, using projections for all days")
            daily_projections = {cat: past_stats.get(cat, 0) + remaining_stats.get(cat, 0) for cat in self.COUNTING_STATS}
            daily_fg_data = {
                'fgm': past_fg_data['fgm'] + remaining_fg_data['fgm'],
                'fga': past_fg_data['fga'] + remaining_fg_data['fga'],
                'ftm': past_fg_data['ftm'] + remaining_fg_data['ftm'],
                'fta': past_fg_data['fta'] + remaining_fg_data['fta']
            }
        
        # Build player projections for display
        player_projections = []
        for p in player_info:
            # Calculate this player's projected stats based on games counted
            proj_stats = self._project_player_stats(
                p['avg_stats'], 
                p['games_counted'], 
                p['injury_factor']
            )
            
            # Get weekly schedule for this player's team for the REQUESTED week (supports double weeks)
            weekly_sched = get_team_weekly_schedule(p['team_abbr'], week_start, week_end) if p['team_abbr'] else []
            
            # Count games from weekly schedule (correct for both normal and double weeks)
            games_count = sum(1 for day in weekly_sched if day.get('has_game')) if weekly_sched else p['total_games']
            
            player_projections.append(PlayerProjection(
                player_key=p['player_key'],
                name=p['name'],
                team=p['team_abbr'],
                position=p['position'],
                roster_position=p['roster_position'],
                status=p['status'],
                injury_note=p['injury_note'],
                games_this_week=games_count,
                avg_stats=self._convert_stat_ids_to_names(p['avg_stats']),
                projected_stats=proj_stats,
                injury_adjustment=p['injury_factor'],
                is_on_il=p['is_on_il'],
                game_today=p.get('game_today'),
                weekly_schedule=weekly_sched
            ))
        
        # Final totals = actual stats (past days from Yahoo) + projected stats (remaining days)
        # This gives the most accurate prediction by using real results for days already played
        final_totals = {}
        for cat in self.STAT_CATEGORIES.keys():
            if cat in self.COUNTING_STATS:
                # Counting stats: sum of past + remaining (all calculated from 30-day averages)
                final_totals[cat] = daily_projections.get(cat, 0)
            else:
                # Rate stats (FG%, FT%): calculate from combined FG/FT data
                if cat == 'FG%' and daily_fg_data['fga'] > 0:
                    final_totals[cat] = (daily_fg_data['fgm'] / daily_fg_data['fga']) * 100
                elif cat == 'FT%' and daily_fg_data['fta'] > 0:
                    final_totals[cat] = (daily_fg_data['ftm'] / daily_fg_data['fta']) * 100
                else:
                    final_totals[cat] = 0
        
        print(f"[DEBUG] Final projected totals: {final_totals}")

        return TeamProjection(
            team_key=team_key,
            team_name=team_name,
            players=player_projections,
            total_projected=final_totals,
            remaining_games=remaining_games_counted
        )
    
    def _project_team(self, team_key: str, team_name: str, 
                      roster: List[Dict], player_averages: Dict) -> TeamProjection:
        """Project a team's stats for the week"""
        
        # Get today's games for all teams
        todays_games = get_todays_games()
        
        player_projections = []
        
        for player in roster:
            # Get games this week for player's team
            team_abbr = player.get('team', '')
            
            # Get weekly schedule first (single source of truth)
            weekly_sched = get_team_weekly_schedule(team_abbr) if team_abbr else []
            games = sum(1 for day in weekly_sched if day.get('has_game')) if weekly_sched else 3
            
            # Get roster position (IL, IL+, BN, etc.)
            roster_position = player.get('roster_position', '')
            is_on_il = player.get('is_on_il', False) or roster_position in ['IL', 'IL+']
            
            # Get injury adjustment
            status = player.get('status', '')
            injury_adj = self.INJURY_ADJUSTMENTS.get(status, 1.0)
            
            # If player is on IL or IL+ slot, don't count their stats at all
            if is_on_il:
                injury_adj = 0.0
            
            # Get player's average stats
            avg_stats = player_averages.get(player['player_key'], player.get('stats', {}))
            
            # Project stats (will be 0 if on IL due to injury_adj = 0)
            projected = self._project_player_stats(avg_stats, games, injury_adj)
            
            # Get today's game info
            normalized_team = schedule._normalize_team_abbr(team_abbr) if team_abbr else ''
            game_today = todays_games.get(normalized_team) or todays_games.get(team_abbr)
            
            player_projections.append(PlayerProjection(
                player_key=player['player_key'],
                name=player.get('name', 'Unknown'),
                team=team_abbr,
                position=player.get('position', ''),
                roster_position=roster_position,
                status=status,
                injury_note=player.get('injury_note', ''),
                games_this_week=games,
                avg_stats=self._convert_stat_ids_to_names(avg_stats),
                projected_stats=projected,
                injury_adjustment=injury_adj,
                is_on_il=is_on_il,
                game_today=game_today,
                weekly_schedule=weekly_sched
            ))
        
        # Aggregate team totals
        total_projected = self._aggregate_team_stats(player_projections)
        
        return TeamProjection(
            team_key=team_key,
            team_name=team_name,
            players=player_projections,
            total_projected=total_projected
        )
    
    def _project_player_stats(self, avg_stats: Dict, games: int, 
                               injury_adj: float) -> Dict[str, float]:
        """Project a player's stats for the week"""
        projected = {}
        

        
        # Get games played for calculating per-game averages (stat_id 0)
        games_played = avg_stats.get('0') or avg_stats.get(0) or 1
        try:
            games_played = float(games_played) if games_played > 0 else 1
        except:
            games_played = 1
        
        for cat_name, stat_id in self.STAT_CATEGORIES.items():
            # Try both string and int keys
            raw_value = avg_stats.get(stat_id) or avg_stats.get(int(stat_id)) or avg_stats.get(str(stat_id)) or 0
            try:
                raw_value = float(raw_value)
            except:
                raw_value = 0
            
            if cat_name in self.COUNTING_STATS:
                # Convert season total to per-game average, then multiply by projected games
                per_game_avg = raw_value / games_played
                projected[cat_name] = per_game_avg * games * injury_adj
            else:
                # Rate stats: keep as-is (already percentages or ratios)
                # Convert decimal to percentage if needed (e.g., 0.482 -> 48.2)
                if raw_value < 1 and raw_value > 0:
                    raw_value = raw_value * 100
                projected[cat_name] = raw_value
        

        
        # Store games for rate stat calculations
        projected['_games'] = games * injury_adj
        
        return projected
    
    def _aggregate_team_stats(self, players: List[PlayerProjection]) -> Dict[str, float]:
        """Aggregate projected stats for all players on a team"""
        totals = {cat: 0.0 for cat in self.STAT_CATEGORIES.keys()}
        

        
        # For rate stats, we need weighted averages
        total_fga = 0  # Field Goals Attempted (for FG%)
        total_fgm = 0  # Field Goals Made
        total_fta = 0  # Free Throws Attempted (for FT%)
        total_ftm = 0  # Free Throws Made
        
        for player in players:
            proj = player.projected_stats
            games = proj.get('_games', 0)
            
            # Sum counting stats
            for cat in self.COUNTING_STATS:
                totals[cat] += proj.get(cat, 0)
            
            # For percentage stats, we need to estimate attempts
            # Using typical ratios: ~15 FGA per game, ~5 FTA per game
            if games > 0:
                fg_pct = proj.get('FG%', 0) / 100 if proj.get('FG%', 0) > 1 else proj.get('FG%', 0)
                ft_pct = proj.get('FT%', 0) / 100 if proj.get('FT%', 0) > 1 else proj.get('FT%', 0)
                
                # Estimate attempts based on points
                est_fga = proj.get('PTS', 0) / 2.1 if proj.get('PTS', 0) > 0 else games * 10
                est_fta = proj.get('PTS', 0) / 6 if proj.get('PTS', 0) > 0 else games * 3
                
                total_fga += est_fga
                total_fgm += est_fga * fg_pct
                total_fta += est_fta
                total_ftm += est_fta * ft_pct
        
        # Calculate team percentage stats
        totals['FG%'] = (total_fgm / total_fga * 100) if total_fga > 0 else 0
        totals['FT%'] = (total_ftm / total_fta * 100) if total_fta > 0 else 0
        

        
        return totals
    
    def _compare_projections(self, my_team: TeamProjection, 
                             opponent: TeamProjection) -> Tuple[Dict, Tuple, Dict]:
        """Compare two team projections and predict winner for each category"""
        
        category_winners = {}
        confidence = {}
        my_wins = 0
        opp_wins = 0
        
        for cat in self.STAT_CATEGORIES.keys():
            my_val = my_team.total_projected.get(cat, 0)
            opp_val = opponent.total_projected.get(cat, 0)
            
            # For turnovers, lower is better
            if cat in NEGATIVE_CATEGORIES:
                my_val, opp_val = -my_val, -opp_val
            
            # Determine winner
            if my_val > opp_val:
                category_winners[cat] = 'my_team'
                my_wins += 1
            elif opp_val > my_val:
                category_winners[cat] = 'opponent'
                opp_wins += 1
            else:
                category_winners[cat] = 'tie'
            
            # Calculate confidence (how close is the matchup)
            total = abs(my_val) + abs(opp_val)
            if total > 0:
                diff = abs(my_val - opp_val)
                confidence[cat] = min(diff / total, 1.0)
            else:
                confidence[cat] = 0.5
        
        return category_winners, (my_wins, opp_wins), confidence
    
    def _convert_stat_ids_to_names(self, stats: Dict) -> Dict[str, float]:
        """Convert stat IDs to human-readable names"""
        converted = {}
        for stat_id, value in stats.items():
            stat_name = STAT_ID_MAP.get(str(stat_id), f'stat_{stat_id}')
            try:
                if isinstance(value, str) and '/' in value:
                    parts = value.split('/')
                    num = float(parts[0])
                    denom = float(parts[1])
                    converted[stat_name] = (num / denom * 100) if denom > 0 else 0
                else:
                    converted[stat_name] = float(value) if value else 0
            except:
                converted[stat_name] = 0
        return converted
    
    def _convert_stats_to_categories(self, stats: Dict) -> Dict[str, float]:
        """Convert Yahoo stat IDs to category names for display"""
        converted = {}
        for cat_name, stat_id in self.STAT_CATEGORIES.items():
            value = stats.get(stat_id) or stats.get(int(stat_id)) or stats.get(str(stat_id)) or 0
            try:
                value = float(value)
                # Convert decimal percentages to regular percentages (0.485 -> 48.5)
                if cat_name in ['FG%', 'FT%']:
                    if 0 < value < 1:
                        value = value * 100
                converted[cat_name] = value
            except:
                converted[cat_name] = 0
        return converted
    
    def format_prediction_report(self, prediction: MatchupPrediction) -> str:
        """Format prediction as a readable report"""
        lines = []
        lines.append("=" * 60)
        lines.append(f"📊 חיזוי מאצ'אפ - שבוע {prediction.week}")
        lines.append("=" * 60)
        lines.append("")
        
        # Teams
        my_score, opp_score = prediction.predicted_score
        lines.append(f"🏀 {prediction.my_team.team_name}")
        lines.append(f"   vs")
        lines.append(f"🏀 {prediction.opponent.team_name}")
        lines.append("")
        
        # Predicted score
        lines.append(f"📈 תוצאה חזויה: {my_score}-{opp_score}")
        if my_score > opp_score:
            lines.append("   ✅ צפי לניצחון!")
        elif opp_score > my_score:
            lines.append("   ⚠️ צפי להפסד")
        else:
            lines.append("   ➡️ צפי לתיקו")
        lines.append("")
        
        # Category breakdown
        lines.append("-" * 60)
        lines.append("פירוט לפי קטגוריה:")
        lines.append("-" * 60)
        lines.append(f"{'קטגוריה':<10} {'אתה':<12} {'יריב':<12} {'מנצח':<10}")
        lines.append("-" * 60)
        
        for cat in self.STAT_CATEGORIES.keys():
            my_val = prediction.my_team.total_projected.get(cat, 0)
            opp_val = prediction.opponent.total_projected.get(cat, 0)
            winner = prediction.category_winners.get(cat, '')
            
            # Format values
            if cat in ['FG%', 'FT%']:
                my_str = f"{my_val:.1f}%"
                opp_str = f"{opp_val:.1f}%"
            else:
                my_str = f"{my_val:.1f}"
                opp_str = f"{opp_val:.1f}"
            
            # Winner indicator
            if winner == 'my_team':
                winner_str = "✅ אתה"
            elif winner == 'opponent':
                winner_str = "❌ יריב"
            else:
                winner_str = "➡️ תיקו"
            
            lines.append(f"{cat:<10} {my_str:<12} {opp_str:<12} {winner_str:<10}")
        
        lines.append("")
        lines.append("-" * 60)
        lines.append("שחקנים פצועים/מפוקפקים:")
        lines.append("-" * 60)
        
        # Injured players
        injured_my = [p for p in prediction.my_team.players if p.injury_adjustment < 1.0]
        injured_opp = [p for p in prediction.opponent.players if p.injury_adjustment < 1.0]
        
        if injured_my:
            lines.append(f"\nשלך:")
            for p in injured_my:
                status_emoji = "🔴" if p.injury_adjustment == 0 else "🟡"
                lines.append(f"  {status_emoji} {p.name} ({p.status}) - {p.injury_note}")
        
        if injured_opp:
            lines.append(f"\nיריב:")
            for p in injured_opp:
                status_emoji = "🔴" if p.injury_adjustment == 0 else "🟡"
                lines.append(f"  {status_emoji} {p.name} ({p.status}) - {p.injury_note}")
        
        if not injured_my and not injured_opp:
            lines.append("  אין שחקנים פצועים! ✅")
        
        lines.append("")
        lines.append("=" * 60)
        
        return "\n".join(lines)


# Singleton instance
predictor = FantasyPredictor()
