"""
Fantasy Basketball Matchup Predictor
Predicts weekly matchup results based on player stats, games played, and injuries
"""
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
import statistics

from yahoo_api import api, STAT_ID_MAP, STAT_NAME_TO_ID
from nba_schedule import (
    schedule, get_team_games_this_week, get_teams_playing_on_date,
    get_team_games_remaining_this_week, get_week_dates_range,
    get_todays_games, get_team_game_today
)
from config import CATEGORIES, NEGATIVE_CATEGORIES


# ==================== CONFIGURATION ====================

# Active fantasy roster positions (count for projections)
ACTIVE_POSITIONS = ['PG', 'SG', 'G', 'SF', 'PF', 'F', 'C', 'UTIL']

# Inactive positions (don't count)
INACTIVE_POSITIONS = ['BN', 'IL', 'IL+']

# Maximum daily starters (if more players available, use only starting positions)
MAX_DAILY_STARTERS = 10

# Injury status that SHOULD be counted (100%)
INJURY_COUNT = {
    'Probable': 1.0,
    'P': 1.0,
    'Questionable': 1.0,
    'Q': 1.0,
    'GTD': 1.0,         # Game-Time Decision - count
    'DTD': 1.0,         # Day-to-Day - count
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


@dataclass
class TeamProjection:
    """Projected stats for a team for the week"""
    team_key: str
    team_name: str
    players: List[PlayerProjection]
    total_projected: Dict[str, float]


@dataclass
class MatchupPrediction:
    """Complete matchup prediction"""
    week: int
    my_team: TeamProjection
    opponent: TeamProjection
    category_winners: Dict[str, str]  # category -> 'my_team' or 'opponent'
    predicted_score: Tuple[int, int]  # (my_wins, opponent_wins)
    confidence: Dict[str, float]  # confidence level per category


def get_injury_factor(status: str) -> float:
    """Get injury factor for a player status.
    Returns 1.0 if player should be counted, 0.0 if should be skipped.
    """
    status = status.strip() if status else ''
    
    # Check if should be skipped
    if status in INJURY_SKIP:
        return 0.0
    
    # Check if should be counted
    if status in INJURY_COUNT:
        return 1.0
    
    # Default: count unknown statuses
    return 1.0


class FantasyPredictor:
    """Predicts fantasy basketball matchup outcomes"""
    
    # Legacy injury adjustments (kept for compatibility)
    INJURY_ADJUSTMENTS = {
        'INJ': 0.0,      # Injured - out
        'O': 0.0,        # Out
        'Out': 0.0,
        'SUSP': 0.0,     # Suspended
        'IL': 0.0,       # Injured List
        'IL+': 0.0,      # Extended IL
        'D': 0.0,        # Doubtful
        'Doubtful': 0.0,
        'DTD': 1.0,      # Day-to-Day - count (changed!)
        'GTD': 1.0,      # Game-Time Decision - count (changed!)
        'Q': 1.0,        # Questionable - count (changed!)
        'Questionable': 1.0,
        'P': 1.0,        # Probable - count
        'Probable': 1.0,
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
    
    def predict_matchup(self, league_key: str, week: int = None) -> MatchupPrediction:
        """Generate full matchup prediction combining actual results + projections.
        
        Uses 30-day averages for projections when available.
        """
        
        # Get my team
        my_team_info = self.api.get_my_team(league_key)
        if not my_team_info:
            raise Exception("Could not find your team in this league")
        
        # Get current matchup to find opponent AND current actual stats
        matchup = self.api.get_matchup(my_team_info['team_key'], week)
        if not matchup or not matchup.get('opponent'):
            raise Exception("Could not find matchup for this week")
        
        week_num = int(matchup.get('week', 0))
        
        # Get ACTUAL current stats from the matchup (what Yahoo shows)
        my_actual_stats = matchup.get('my_team', {}).get('stats', {})
        opponent_actual_stats = matchup.get('opponent', {}).get('stats', {})
        
        # Get rosters
        my_roster = self.api.get_team_roster(my_team_info['team_key'], week)
        opponent_roster = self.api.get_team_roster(matchup['opponent']['team_key'], week)
        
        # Get 30-day averages for all players (better for projections)
        all_player_keys = [p['player_key'] for p in my_roster + opponent_roster]
        player_averages = self.api.get_player_stats_last30(all_player_keys)
        
        # Fallback to roster stats if 30-day not available
        for player in my_roster + opponent_roster:
            if player['player_key'] not in player_averages and player.get('stats'):
                player_averages[player['player_key']] = player['stats']
        
        print(f"[DEBUG] Got averages for {len(player_averages)} players")
        
        # Project each team (only for REMAINING games)
        my_projection = self._project_team_with_actuals(
            my_team_info['team_key'],
            my_team_info['name'],
            my_roster,
            player_averages,
            my_actual_stats
        )
        
        opponent_projection = self._project_team_with_actuals(
            matchup['opponent']['team_key'],
            matchup['opponent']['name'],
            opponent_roster,
            player_averages,
            opponent_actual_stats
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
            confidence=confidence
        )
    
    def _project_team_with_actuals(self, team_key: str, team_name: str, 
                                    roster: List[Dict], player_averages: Dict,
                                    actual_stats: Dict) -> TeamProjection:
        """Project team stats: actual results + remaining games projection.
        
        New algorithm:
        1. For each remaining day, calculate which players will contribute
        2. Only count players in ACTIVE positions (not BN, IL, IL+)
        3. Apply injury rules: Probable/Questionable = count, Doubtful/Out = skip
        4. If <= 10 eligible players on a day, use all
        5. If > 10 eligible players, use only starting positions (no UTIL)
        6. Final = Actual from Yahoo + sum of daily projections
        """
        
        today = datetime.now()
        week_start, week_end = get_week_dates_range()
        
        # Get today's games for all teams (for display)
        todays_games = get_todays_games()
        
        # Calculate days remaining (today is included if games haven't started yet)
        # For simplicity, we count today as a "remaining" day
        days_remaining = []
        current_day = today.replace(hour=0, minute=0, second=0, microsecond=0)
        
        while current_day <= week_end:
            days_remaining.append(current_day)
            current_day += timedelta(days=1)
        
        print(f"[DEBUG] Days remaining in week: {len(days_remaining)}")
        
        # Build player info with positions and injury status
        player_info = []
        for player in roster:
            team_abbr = player.get('team', '')
            total_games = get_team_games_this_week(team_abbr) if team_abbr else 3
            
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
            
            player_info.append({
                'player_key': player['player_key'],
                'name': player.get('name', 'Unknown'),
                'team_abbr': team_abbr,
                'position': player.get('position', ''),
                'roster_position': roster_position,
                'status': status,
                'injury_note': player.get('injury_note', ''),
                'injury_factor': injury_factor,
                'is_on_il': is_on_il,
                'avg_stats': avg_stats,
                'total_games': total_games,
                'games_counted': 0,  # Track how many games we project
                'game_today': game_today,  # Today's game info
            })
        
        # Calculate daily projections
        daily_projections = {cat: 0.0 for cat in self.COUNTING_STATS}
        daily_fg_data = {'fgm': 0, 'fga': 0, 'ftm': 0, 'fta': 0}
        
        for day in days_remaining:
            # Get teams playing on this day
            teams_playing = get_teams_playing_on_date(day)
            
            # If API failed, assume all teams play (use all eligible players)
            if not teams_playing:
                teams_playing = None  # Will match all teams
            
            # Filter to eligible players for this day
            eligible_players = []
            for p in player_info:
                # Skip if on IL/IL+ or injured (Doubtful/Out)
                if p['injury_factor'] == 0.0:
                    continue
                
                # Skip if not in active position
                if p['roster_position'] in INACTIVE_POSITIONS:
                    continue
                
                # Skip if team not playing today
                if teams_playing is not None and p['team_abbr'] not in teams_playing:
                    continue
                
                eligible_players.append(p)
            
            # Determine which players to use based on count
            if len(eligible_players) <= MAX_DAILY_STARTERS:
                # Use all eligible players
                players_to_count = eligible_players
            else:
                # Too many eligible - only use starting positions (not UTIL, not BN)
                starting_positions = ['PG', 'SG', 'G', 'SF', 'PF', 'F', 'C']
                players_to_count = [
                    p for p in eligible_players 
                    if p['roster_position'] in starting_positions
                ]
                
                # If still not enough after filtering, fall back to all eligible
                if len(players_to_count) < MAX_DAILY_STARTERS:
                    players_to_count = eligible_players[:MAX_DAILY_STARTERS]
            
            print(f"[DEBUG] Day {day.strftime('%Y-%m-%d')}: {len(players_to_count)} players counted")
            
            # Add daily stats for these players
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
                        # Already per-game average
                        per_game = raw_value
                    else:
                        # Total stats - divide by games played
                        per_game = raw_value / games_played
                    
                    daily_projections[cat] += per_game * p['injury_factor']
                
                # Track FG/FT data for percentage calculations
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
                
                # Estimate attempts from points
                est_fga = pts / 2.1 if pts > 0 else 8
                est_fta = pts / 6 if pts > 0 else 3
                
                daily_fg_data['fgm'] += est_fga * fg_pct * p['injury_factor']
                daily_fg_data['fga'] += est_fga * p['injury_factor']
                daily_fg_data['ftm'] += est_fta * ft_pct * p['injury_factor']
                daily_fg_data['fta'] += est_fta * p['injury_factor']
        
        # Build player projections for display
        player_projections = []
        for p in player_info:
            # Calculate this player's projected stats based on games counted
            proj_stats = self._project_player_stats(
                p['avg_stats'], 
                p['games_counted'], 
                p['injury_factor']
            )
            
            player_projections.append(PlayerProjection(
                player_key=p['player_key'],
                name=p['name'],
                team=p['team_abbr'],
                position=p['position'],
                roster_position=p['roster_position'],
                status=p['status'],
                injury_note=p['injury_note'],
                games_this_week=p['total_games'],
                avg_stats=self._convert_stat_ids_to_names(p['avg_stats']),
                projected_stats=proj_stats,
                injury_adjustment=p['injury_factor'],
                is_on_il=p['is_on_il'],
                game_today=p.get('game_today')
            ))
        
        # Final totals = Actual from Yahoo + Projection for remaining
        final_totals = {}
        for cat in self.STAT_CATEGORIES.keys():
            stat_id = self.STAT_CATEGORIES[cat]
            actual_value = actual_stats.get(stat_id) or actual_stats.get(str(stat_id)) or 0
            try:
                actual_value = float(actual_value)
            except:
                actual_value = 0
            
            if cat in self.COUNTING_STATS:
                # Counting stats: actual + projected remaining
                final_totals[cat] = actual_value + daily_projections.get(cat, 0)
            else:
                # Rate stats (FG%, FT%): use actual from Yahoo if available
                if actual_value > 0 and actual_value < 1:
                    actual_value = actual_value * 100
                
                if actual_value > 0:
                    final_totals[cat] = actual_value
                else:
                    # Calculate from daily projections
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
            total_projected=final_totals
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
            games = get_team_games_this_week(team_abbr) if team_abbr else 3
            
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
                game_today=game_today
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
        
        # #region agent log - Hypothesis A,C: Check stat lookup and values
        import json
        try:
            with open(r'c:\Users\yoel\NBA_Fantasy\.cursor\debug.log', 'a') as f:
                f.write(json.dumps({"hypothesisId":"A,C","location":"predictor.py:_project_player_stats","message":"Input stats","data":{"games":games,"injury_adj":injury_adj,"avg_stats_keys":list(avg_stats.keys())[:10],"sample_stat_12":avg_stats.get('12') or avg_stats.get(12)},"timestamp":__import__('time').time()}) + '\n')
        except: pass
        # #endregion
        
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
        
        # #region agent log - Hypothesis A,C: Check projected output
        try:
            with open(r'c:\Users\yoel\NBA_Fantasy\.cursor\debug.log', 'a') as f:
                f.write(json.dumps({"hypothesisId":"A,C","location":"predictor.py:_project_player_stats:end","message":"Projected stats","data":{"games_played":games_played,"projected":projected},"timestamp":__import__('time').time()}) + '\n')
        except: pass
        # #endregion
        
        # Store games for rate stat calculations
        projected['_games'] = games * injury_adj
        
        return projected
    
    def _aggregate_team_stats(self, players: List[PlayerProjection]) -> Dict[str, float]:
        """Aggregate projected stats for all players on a team"""
        totals = {cat: 0.0 for cat in self.STAT_CATEGORIES.keys()}
        
        # #region agent log - Hypothesis B: Check aggregation input
        import json
        try:
            with open(r'c:\Users\yoel\NBA_Fantasy\.cursor\debug.log', 'a') as f:
                sample_player = players[0] if players else None
                f.write(json.dumps({"hypothesisId":"B","location":"predictor.py:_aggregate_team_stats","message":"Aggregating stats","data":{"num_players":len(players),"sample_projected":sample_player.projected_stats if sample_player else None},"timestamp":__import__('time').time()}) + '\n')
        except: pass
        # #endregion
        
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
        
        # #region agent log - Hypothesis B: Check final totals
        import json
        try:
            with open(r'c:\Users\yoel\NBA_Fantasy\.cursor\debug.log', 'a') as f:
                f.write(json.dumps({"hypothesisId":"B","location":"predictor.py:_aggregate_team_stats:end","message":"Final totals","data":{"totals":totals},"timestamp":__import__('time').time()}) + '\n')
        except: pass
        # #endregion
        
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
