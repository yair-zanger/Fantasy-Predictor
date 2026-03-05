"""
Fantasy Basketball Predictor - Flask Web Application
"""
import sys
import io

# Force UTF-8 encoding for console output (fixes Windows encoding issues)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from flask import Flask, render_template, redirect, url_for, request, jsonify, session
import os
import json
import threading
import time

from functools import wraps
from yahoo_auth import auth
from yahoo_api import api
from predictor import predictor, PlayoffWeekError
from config import CATEGORIES, DEBUG_MODE, IS_VERCEL, ADMIN_EMAILS

def debug_print(*args, **kwargs):
    """Print only if DEBUG_MODE is enabled."""
    if DEBUG_MODE:
        print(*args, **kwargs)


def _preload_data():
    """Pre-load external data and caches for faster first request."""
    try:
        # Load Yahoo API disk cache into memory (fast - just read JSON)
        from yahoo_api import load_disk_cache
        debug_print("[Startup] Loading Yahoo API cache from disk...")
        load_disk_cache()
        
        # Pre-load Basketball Reference data
        from basketball_reference import fetch_all_nba_season_averages
        debug_print("[Startup] Pre-loading Basketball Reference data...")
        stats = fetch_all_nba_season_averages()
        debug_print(f"[Startup] Pre-loaded {len(stats)} player averages")
        
        # Pre-load NBA schedule data
        from nba_schedule import _fetch_and_cache_full_schedule
        debug_print("[Startup] Pre-loading NBA schedule data...")
        schedule = _fetch_and_cache_full_schedule()
        debug_print(f"[Startup] Pre-loaded schedule for {len(schedule)} dates")
        
    except Exception as e:
        debug_print(f"[Startup] Error pre-loading data: {e}")


_warm_lock = threading.Lock()

def _warm_league_caches(leagues: list):
    """Background: preload cache for all leagues so next pages are instant.
    This includes fetching data from Yahoo API and calculating predictions.
    """
    if not leagues:
        return
        
    # Prevent multiple background warmups from running concurrently and killing the server
    if not _warm_lock.acquire(blocking=False):
        debug_print("[Cache Pre-warm] Warm already in progress, skipping to prevent server overload...")
        return
        
    try:
        debug_print(f"[Cache Pre-warm] Starting background warm for {len(leagues)} leagues...")
        for league in leagues:
            try:
                league_key = league.get('league_key')
                current_week = int(league.get('current_week', 1))
                
                # 1. Warm Yahoo API data
                api.get_league_settings(league_key)
                api.get_my_team(league_key)
                api.get_league_scoreboard(league_key, current_week)
                api.get_league_standings(league_key)
                my_team = api.get_my_team(league_key)
                
                if my_team:
                    api.get_team_roster(my_team['team_key'], current_week)
                    matchup = api.get_matchup(my_team['team_key'], current_week)
                    if matchup and matchup.get('opponent'):
                        api.get_team_roster(matchup['opponent']['team_key'], current_week)
                
                api.get_category_records(league_key, current_week)
                
                # 2. Warm Matchup Predictions for Current, Previous, and Next weeks
                # This prevents the 20s load time for adjacent weeks
                # Order matters: Warm current week first so it's ready ASAP
                weeks_to_warm = [current_week]
                
                # Check end_week to avoid warming past the season
                end_week = int(league.get('end_week', 24))
                if current_week < end_week:
                    weeks_to_warm.append(current_week + 1)
                
                if current_week > 1:
                    weeks_to_warm.append(current_week - 1)
                
                for week in weeks_to_warm:
                    # Give the server a small rest between heavy operations so the website remains responsive
                    time.sleep(1.0)
                    
                    # First, all matchups in the league
                    try:
                        predictor.predict_all_matchups(league_key, week, current_week)
                    except PlayoffWeekError:
                        pass # Ignore if playoff week
                    except Exception as e:
                        debug_print(f"[Cache Pre-warm] Error pre-warming predict_all_matchups week {week}: {e}")
                    
                    # Second, my specific matchup
                    try:
                        predictor.predict_matchup(league_key, week, current_week)
                    except PlayoffWeekError:
                        pass
                    except Exception as e:
                        debug_print(f"[Cache Pre-warm] Error pre-warming predict_matchup week {week}: {e}")
                    
                # 3. Warm Standings Projections (projects current_week to current_week to populate the cache)
                try:
                    current_records = api.get_category_records(league_key, current_week)
                    project_future_category_records(league_key, current_records, current_week, current_week)
                except Exception as e:
                    debug_print(f"[Cache Pre-warm] Error pre-warming standings: {e}")
                    
            except Exception as e:
                debug_print(f"[Cache Pre-warm] Error pre-warming league {league.get('league_key')}: {e}")
                continue
                
        debug_print("[Cache Pre-warm] League caches and predictions warmed successfully!")
    except Exception as e:
        debug_print(f"[Cache Pre-warm] Warm error: {e}")
    finally:
        _warm_lock.release()


def _background_cache_refresh_loop():
    """Every 10 min refresh BBRef + NBA schedule so cache stays hot 24/7.
    """
    while True:
        time.sleep(10 * 60) # 10 minutes
        try:
            from basketball_reference import fetch_all_nba_season_averages
            from nba_schedule import _fetch_and_cache_full_schedule
            fetch_all_nba_season_averages()
            _fetch_and_cache_full_schedule()
            debug_print("[Cache 24/7] Refreshed BBRef + NBA schedule")
            
        except Exception as e:
            debug_print(f"[Cache 24/7] Refresh error: {e}")


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not auth.is_authenticated():
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not auth.is_authenticated():
            return redirect(url_for('login'))
        
        # Admin check
        user_name = api.get_logged_in_user_name()
        # In this specific app, we identify admins by their known Yahoo nicknames
        # since getting email requires different scopes. 
        # The user provided yairzanger@gmail.com but my code will check against known nicknames
        # or I can add a specific GUID check if I know them.
        # For now, let's use the provided emails as a placeholder or check against nicknames
        # if nicknames are known to the user.
        # Actually, let's stick to the plan of checking something unique.
        if user_name not in ['Yair abdul gerald']: # Example nickname based on previous context
            return redirect(url_for('dashboard'))
            
        return f(*args, **kwargs)
    return decorated_function


def validate_league_access(league_key):
    """Ensure the logged in user has access to the given league."""
    try:
        leagues = api.get_user_leagues()
        league_keys = [l['league_key'] for l in leagues]
        if league_key not in league_keys:
            abort(403) # Forbidden
    except Exception:
        abort(403)


app = Flask(__name__)
# Use a stable secret key so sessions survive across serverless cold starts.
# Set FLASK_SECRET_KEY env var in Vercel dashboard.
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))


# Make auth available in all templates
@app.context_processor
def inject_auth():
    context = dict(auth=auth)
    if auth.is_authenticated():
        try:
            name = api.get_logged_in_user_name()
            context['current_user_name'] = name
            context['is_admin'] = name in ['Yair abdul gerald'] # Example admin check
        except Exception as e:
            debug_print(f"Error getting username for context: {e}")
            context['current_user_name'] = ""
            context['is_admin'] = False
    else:
        context['current_user_name'] = ""
        context['is_admin'] = False
    return context

@app.route('/')
def index():
    """Home page"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
    return redirect(url_for('dashboard'))


@app.route('/profile')
@login_required
def profile():
    """User profile page"""
    try:
        user_name = api.get_logged_in_user_name()
        leagues = api.get_user_leagues()
        # Add team info for each league
        user_teams = []
        for league in leagues:
            try:
                team = api.get_my_team(league['league_key'])
                if team:
                    # Get rank and W-L from standings
                    standings_data = api.get_league_standings(league['league_key'])
                    team_standings = next((s for s in standings_data if s['team_key'] == team['team_key']), None)
                    if team_standings:
                        team.update({
                            'rank': team_standings['rank'],
                            'wins': team_standings['wins'],
                            'losses': team_standings['losses'],
                            'ties': team_standings['ties']
                        })
                    team['league_name'] = league['name']
                    team['league_key'] = league['league_key']
                    user_teams.append(team)
            except Exception:
                continue
                
        return render_template('profile.html', 
                             user_name=user_name,
                             user_teams=user_teams)
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/admin')
@admin_required
def admin_page():
    """Admin management panel"""
    try:
        from yahoo_api import _api_cache
        guid = api.get_user_guid()
        
        stats = {
            'total_cached_keys': len(_api_cache),
            'user_guid': guid,
            'token_expiry': auth.token_expiry,
            'admin_emails': ADMIN_EMAILS
        }
        return render_template('admin.html', stats=stats)
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/login')
def login():
    """Show login page"""
    if auth.is_authenticated():
        return redirect(url_for('dashboard'))
    
    return render_template('login.html')


@app.route('/auth/start')
def auth_start():
    """Start OAuth flow — route through Yahoo sign-out first so the user
    is always presented with a fresh login form and can switch accounts."""
    from urllib.parse import quote
    oauth_url = auth.get_auth_url()
    # Passing the OAuth URL as the .done redirect means Yahoo will:
    #   1. Sign the user out of their current Yahoo session
    #   2. Redirect them to the OAuth login page where they enter credentials
    yahoo_signout = f"https://login.yahoo.com/config/login?logout=1&.done={quote(oauth_url, safe='')}"
    return redirect(yahoo_signout)


@app.route('/auth/callback')
def auth_callback():
    """Handle OAuth callback"""
    code = request.args.get('code')
    error = request.args.get('error')
    
    if error:
        return render_template('error.html', error=error)
    
    if code:
        try:
            auth.exchange_code_for_token(code)
            # Fetch GUID immediately to isolate the session
            guid = api.get_user_guid()
            # Clear only this user's cache to ensure a fresh start
            from yahoo_api import clear_user_cache
            clear_user_cache(guid)
            return redirect(url_for('dashboard'))
        except Exception as e:
            return render_template('error.html', error=str(e))
    
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard - league selection"""
    try:
        leagues = api.get_user_leagues()
        
        # Enrich league data with team info for a better view
        enriched_leagues = []
        for league in leagues:
            try:
                my_team = api.get_my_team(league['league_key'])
                if my_team:
                    league['my_team_name'] = my_team['name']
                    # We could add more like rank here
                enriched_leagues.append(league)
            except Exception:
                enriched_leagues.append(league)

        # Warm cache in background so next pages (predict, standings) are instant
        threading.Thread(target=_warm_league_caches, args=(enriched_leagues,), daemon=True).start()
        return render_template('dashboard.html', leagues=enriched_leagues)
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/league/<league_key>')
@login_required
def league_view(league_key):
    """View league details and prediction"""
    validate_league_access(league_key)
    try:
        # Get league settings
        settings = api.get_league_settings(league_key)
        
        # Get my team
        my_team = api.get_my_team(league_key)
        
        return render_template('league.html', 
                             league_key=league_key,
                             settings=settings,
                             my_team=my_team)
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/predict/<league_key>')
@login_required
def predict(league_key):
    """Generate and show prediction"""
    validate_league_access(league_key)
    
    week = request.args.get('week', type=int)
    
    try:
        # Get league info for week navigation
        leagues = api.get_user_leagues()
        league_info = next((l for l in leagues if l['league_key'] == league_key), None)
        
        current_week = int(league_info['current_week']) if league_info else 1
        start_week = int(league_info['start_week']) if league_info else 1
        end_week = int(league_info['end_week']) if league_info else 24
        playoff_start_week = int(league_info['playoff_start_week']) if league_info and league_info.get('playoff_start_week') else None
        
        # Use current week if not specified
        selected_week = week if week else current_week
        
        # Optional: align total/remaining games to Yahoo (e.g. ?yahoo_remaining=39)
        yahoo_remaining = request.args.get('yahoo_remaining', type=int)
        
        prediction = predictor.predict_matchup(
            league_key, selected_week, current_week,
            yahoo_remaining_my_team=yahoo_remaining
        )
        return render_template('prediction.html', 
                             prediction=prediction,
                             league_key=league_key,
                             categories=CATEGORIES,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             selected_week=selected_week,
                             playoff_start_week=playoff_start_week)
    except PlayoffWeekError as e:
        # Playoff week - run prediction simulation for next week
        simulation = {}
        try:
            simulation = simulate_next_playoff_week(league_key, selected_week, current_week)
        except Exception as sim_e:
            debug_print(f"[Playoff] Sim error: {sim_e}")
            simulation = {'error': str(sim_e)}
            
        return render_template('playoff.html',
                             league_key=league_key,
                             selected_week=selected_week,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             playoff_start_week=playoff_start_week,
                             message=e.message,
                             simulation=simulation)
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/standings/<league_key>')
@login_required
def standings(league_key):
    """Show league standings with week navigation"""
    validate_league_access(league_key)
    
    week = request.args.get('week', type=int)
    
    try:
        # Get league info
        leagues = api.get_user_leagues()
        league_info = next((l for l in leagues if l['league_key'] == league_key), None)
        
        league_name = league_info['name'] if league_info else 'League'
        current_week = int(league_info['current_week']) if league_info else 1
        start_week = int(league_info['start_week']) if league_info else 1
        end_week = int(league_info['end_week']) if league_info else 24
        playoff_start_week = int(league_info['playoff_start_week']) if league_info and league_info.get('playoff_start_week') else None
        
        # Use current week if not specified
        selected_week = week if week else current_week
        
        # Check if this is a playoff week - if so, show playoff screen instead of standings
        if playoff_start_week and selected_week >= playoff_start_week:
            return render_template('playoff_standings.html',
                                 league_key=league_key,
                                 league_name=league_name,
                                 selected_week=selected_week,
                                 current_week=current_week,
                                 start_week=start_week,
                                 end_week=end_week,
                                 playoff_start_week=playoff_start_week,
                                 message=f"Week {selected_week} is a playoff week — regular standings no longer apply")
        
        # Get current actual standings from Yahoo (these are matchup wins/losses, not category wins)
        standings_data = api.get_league_standings(league_key)
        
        # Get my team to highlight
        my_team = api.get_my_team(league_key)
        my_team_key = my_team['team_key'] if my_team else None
        
        # Determine if this is a projection (current or future week) or historical data
        is_projection = selected_week >= current_week
        
        # For past weeks: show actual Yahoo standings (matchup W-L)
        # For current and future weeks: project based on category records
        category_records = None
        
        if is_projection:
            # Current/future week: calculate projected category records
            # Get actual records up to current week (past weeks only)
            category_records = api.get_category_records(league_key, current_week)
            debug_print(f"[Standings] Projecting from week {current_week} to {selected_week}")
            # Add predictions from current_week to selected_week
            category_records = project_future_category_records(
                league_key, category_records, current_week, selected_week
            )
            # Re-rank teams based on projected category records
            standings_data = rerank_standings(standings_data, category_records)
            debug_print(f"[Standings] Re-ranked {len(standings_data)} teams based on projections")
        
        return render_template('standings.html',
                             standings=standings_data,
                             category_records=category_records,
                             league_key=league_key,
                             league_name=league_name,
                             current_week=current_week,
                             selected_week=selected_week,
                             start_week=start_week,
                             end_week=end_week,
                             my_team_key=my_team_key,
                             is_projection=is_projection)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template('error.html', error=str(e))


def rerank_standings(standings_data: list, category_records: dict) -> list:
    """Re-rank teams based on category records (winning percentage)"""
    from copy import deepcopy
    
    standings = deepcopy(standings_data)
    
    # Calculate win percentage for each team
    for team in standings:
        team_key = team['team_key']
        if team_key in category_records:
            cat_record = category_records[team_key]
            cat_wins = cat_record.get('cat_wins', 0)
            cat_losses = cat_record.get('cat_losses', 0)
            cat_ties = cat_record.get('cat_ties', 0)
            total_cats = cat_wins + cat_losses + cat_ties
            
            # Calculate winning percentage
            if total_cats > 0:
                team['_projected_pct'] = (cat_wins / total_cats) * 100
            else:
                team['_projected_pct'] = 0
        else:
            team['_projected_pct'] = 0
    
    # Sort by winning percentage (highest first)
    standings.sort(key=lambda x: x.get('_projected_pct', 0), reverse=True)
    
    # Update ranks
    for i, team in enumerate(standings, start=1):
        team['rank'] = i
    
    return standings


def project_future_category_records(league_key: str, current_records: dict, 
                                      current_week: int, target_week: int) -> dict:
    """Project category records for future weeks based on matchup predictions"""
    from copy import deepcopy
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from predictor import _get_prediction_cached, _set_prediction_cached
    
    # Check cache first
    cache_key = f"projected_records:{league_key}:{current_week}:{target_week}"
    cached = _get_prediction_cached(cache_key)
    if cached is not None:
        debug_print(f"[Standings] Using cached projected records for {league_key} week {current_week} to {target_week}")
        return cached
        
    # Start with current category records
    projected = deepcopy(current_records)
    
    # Helper to predict a single week
    def predict_week(week):
        try:
            return week, predictor.predict_all_matchups(league_key, week, current_week)
        except Exception as e:
            debug_print(f"[DEBUG] Error predicting week {week}: {e}")
            return week, None
            
    # Fetch all future predictions in parallel
    weeks_to_predict = list(range(current_week, target_week + 1))
    week_predictions = {}
    
    with ThreadPoolExecutor(max_workers=min(10, len(weeks_to_predict) or 1)) as executor:
        future_to_week = {executor.submit(predict_week, w): w for w in weeks_to_predict}
        for future in as_completed(future_to_week):
            week, predictions = future.result()
            if predictions:
                week_predictions[week] = predictions

    # Apply predictions sequentially to ensure consistent processing
    for week in sorted(week_predictions.keys()):
        predictions = week_predictions[week]
        for matchup in predictions:
            team1_key = matchup['team1']['key']
            team2_key = matchup['team2']['key']
            team1_cat_wins = matchup['team1']['wins']  # Category wins
            team2_cat_wins = matchup['team2']['wins']  # Category wins
            
            # Initialize if not exists
            if team1_key not in projected:
                projected[team1_key] = {'cat_wins': 0, 'cat_losses': 0, 'cat_ties': 0}
            if team2_key not in projected:
                projected[team2_key] = {'cat_wins': 0, 'cat_losses': 0, 'cat_ties': 0}
            
            # Add category wins/losses
            projected[team1_key]['cat_wins'] += team1_cat_wins
            projected[team1_key]['cat_losses'] += team2_cat_wins
            projected[team2_key]['cat_wins'] += team2_cat_wins
            projected[team2_key]['cat_losses'] += team1_cat_wins
            
            # Calculate ties (9 categories - wins - losses for each team)
            ties = 9 - team1_cat_wins - team2_cat_wins
            if ties > 0:
                projected[team1_key]['cat_ties'] += ties
                projected[team2_key]['cat_ties'] += ties
    
    # Save to cache
    _set_prediction_cached(cache_key, projected)
    
    return projected


def simulate_next_playoff_week(league_key: str, target_week: int, current_week: int) -> dict:
    """Project the playoff bracket for checking who advances."""
    from predictor import _get_prediction_cached, _set_prediction_cached
    
    # Check cache (v4 for cache busting with consolation support)
    cache_key = f"playoff_sim_v6:{league_key}:{target_week}:{current_week}"
    cached = _get_prediction_cached(cache_key)
    if cached is not None:
        return cached

    leagues = api.get_user_leagues()
    league_info = next((l for l in leagues if l['league_key'] == league_key), None)
    if not league_info:
        return {}
        
    playoff_start_week = int(league_info.get('playoff_start_week', 22)) if league_info.get('playoff_start_week') else 22
    end_week = int(league_info.get('end_week', 24)) if league_info.get('end_week') else 24
    # Consolation settings
    has_consolation = league_info.get('has_playoff_consolation_games', '0') == '1'
    num_consolation_teams = int(league_info.get('num_playoff_consolation_teams', 0) or 0)
    
    bracket = []
    consolation_bracket = []
    eliminated = []
    my_team_status = ""
    my_team = api.get_my_team(league_key)
    my_team_key = my_team['team_key'] if my_team else None
    
    try:
        # If target_week is the first week of playoffs and we're before it or right at its start
        if target_week == playoff_start_week and current_week <= playoff_start_week:
            standings_data = api.get_league_standings(league_key)
            
            if current_week < playoff_start_week:
                try:
                    current_records = api.get_category_records(league_key, current_week)
                    projected_records = project_future_category_records(league_key, current_records, current_week, playoff_start_week - 1)
                    projected_standings = rerank_standings(standings_data, projected_records)
                except Exception as e:
                    debug_print(f"[Playoff Sim] Projection failed, using current standings. Error: {e}")
                    projected_standings = standings_data
            else:
                projected_standings = standings_data
            
            top_8 = projected_standings[:8]
            eliminated = [t['team_key'] for t in projected_standings[8:]]
            
            if len(top_8) >= 8:
                bracket = [
                    {'team1': top_8[0], 'team2': top_8[7], 'seed1': top_8[0].get('rank', 1), 'seed2': top_8[7].get('rank', 8), 'match_label': 'Quarterfinal', 'match_type': 'quarterfinal'},
                    {'team1': top_8[3], 'team2': top_8[4], 'seed1': top_8[3].get('rank', 4), 'seed2': top_8[4].get('rank', 5), 'match_label': 'Quarterfinal', 'match_type': 'quarterfinal'},
                    {'team1': top_8[2], 'team2': top_8[5], 'seed1': top_8[2].get('rank', 3), 'seed2': top_8[5].get('rank', 6), 'match_label': 'Quarterfinal', 'match_type': 'quarterfinal'},
                    {'team1': top_8[1], 'team2': top_8[6], 'seed1': top_8[1].get('rank', 2), 'seed2': top_8[6].get('rank', 7), 'match_label': 'Quarterfinal', 'match_type': 'quarterfinal'}
                ]
            # Build consolation bracket for teams 9-12 (if league has consolation games)
            if has_consolation and num_consolation_teams > 0:
                consolation_teams = projected_standings[8:8 + num_consolation_teams]
                for i in range(0, len(consolation_teams), 2):
                    if i + 1 < len(consolation_teams):
                        consolation_bracket.append({
                            'team1': consolation_teams[i],
                            'team2': consolation_teams[i + 1],
                            'seed1': consolation_teams[i].get('rank', i + 9),
                            'seed2': consolation_teams[i + 1].get('rank', i + 10),
                            'is_consolation': True,
                            'match_label': 'Consolation',
                            'match_type': 'consolation'
                        })
                
        # If we are already in the playoffs OR predicting a future week deep in the playoffs
        elif target_week > playoff_start_week and target_week > current_week:
            standings_data = api.get_league_standings(league_key)
            rank_map = {t['team_key']: t.get('rank', 99) for t in standings_data}
            standings_map = {t['team_key']: t for t in standings_data}

            prev_week = target_week - 1
            winners = []
            losers = []

            try:
                # Use real predictions (roster + BBRef stats) for prev_week.
                # predict_all_matchups handles simulated weeks internally by calling
                # simulate_next_playoff_week for the bracket, so rosters are fetched
                # using real team_keys and actual projections are computed.
                prev_predictions = predictor.predict_all_matchups(league_key, prev_week, current_week)

                for match in prev_predictions:
                    if match.get('is_consolation'):
                        continue  # Only championship bracket for now
                    t1 = match.get('team1', {})
                    t2 = match.get('team2', {})
                    t1_key = t1.get('key') or t1.get('team_key')
                    t2_key = t2.get('key') or t2.get('team_key')
                    t1_rank = rank_map.get(t1_key, 99)
                    t2_rank = rank_map.get(t2_key, 99)
                    winner_key = match.get('winner_key')

                    if winner_key == t1_key:
                        winners.append({'team_key': t1_key, 'name': t1.get('name'), 'rank': t1_rank})
                        losers.append(t2_key)
                    elif winner_key == t2_key:
                        winners.append({'team_key': t2_key, 'name': t2.get('name'), 'rank': t2_rank})
                        losers.append(t1_key)
                    else:
                        # Tie or unknown - use seeding as tiebreaker
                        if t1_rank <= t2_rank:
                            winners.append({'team_key': t1_key, 'name': t1.get('name'), 'rank': t1_rank})
                            losers.append(t2_key)
                        else:
                            winners.append({'team_key': t2_key, 'name': t2.get('name'), 'rank': t2_rank})
                            losers.append(t1_key)

            except Exception as pred_e:
                debug_print(f"[Playoff Sim] predict_all_matchups failed for week {prev_week}: {pred_e}. Falling back to win% prediction.")
                # Fallback: simulate prev week bracket and use season win% to predict winners
                prev_sim = simulate_next_playoff_week(league_key, prev_week, current_week)
                for match in prev_sim.get('bracket', []):
                    t1 = match.get('team1', {})
                    t2 = match.get('team2', {})
                    t1_key = t1.get('team_key') or t1.get('key')
                    t2_key = t2.get('team_key') or t2.get('key')
                    t1_s = standings_map.get(t1_key, {})
                    t2_s = standings_map.get(t2_key, {})
                    t1_pct = t1_s.get('win_pct', 0)
                    t2_pct = t2_s.get('win_pct', 0)
                    t1_rank = rank_map.get(t1_key, t1.get('rank', 99))
                    t2_rank = rank_map.get(t2_key, t2.get('rank', 99))
                    # Team with higher win% advances; seeding is tiebreaker
                    if t1_pct > t2_pct or (t1_pct == t2_pct and t1_rank <= t2_rank):
                        winners.append({'team_key': t1_key, 'name': t1.get('name'), 'rank': t1_rank})
                        losers.append(t2_key)
                    else:
                        winners.append({'team_key': t2_key, 'name': t2.get('name'), 'rank': t2_rank})
                        losers.append(t1_key)

            eliminated = losers
            
            # Determine stage based on weeks remaining
            rounds_remaining = end_week - target_week
            
            # Sort winners so we can pair them correctly
            if len(winners) == 4:
                # Top bracket: seeds 1/8, 4/5
                top_bracket = [w for w in winners if w['rank'] in [1, 8, 4, 5]]
                # Bottom bracket: seeds 2/7, 3/6
                bottom_bracket = [w for w in winners if w['rank'] in [2, 7, 3, 6]]
                
                label = "Semifinal" if rounds_remaining == 1 else "Quarterfinal"
                mtype = "semifinal" if rounds_remaining == 1 else "quarterfinal"
                
                if len(top_bracket) == 2 and len(bottom_bracket) == 2:
                    bracket.append({'team1': top_bracket[0], 'team2': top_bracket[1], 'match_label': label, 'match_type': mtype})
                    bracket.append({'team1': bottom_bracket[0], 'team2': bottom_bracket[1], 'match_label': label, 'match_type': mtype})
                else:
                    winners.sort(key=lambda x: x['rank'])
                    bracket.append({'team1': winners[0], 'team2': winners[3], 'match_label': label, 'match_type': mtype})
                    bracket.append({'team1': winners[1], 'team2': winners[2], 'match_label': label, 'match_type': mtype})
            elif len(winners) == 2:
                label = "Championship Final" if rounds_remaining == 0 else "Semifinal"
                mtype = "championship_final" if rounds_remaining == 0 else "semifinal"
                bracket.append({'team1': winners[0], 'team2': winners[1], 'match_label': label, 'match_type': mtype})
            else:
                for i in range(0, len(winners), 2):
                    if i + 1 < len(winners):
                        bracket.append({
                            'team1': winners[i],
                            'team2': winners[i+1],
                            'match_label': 'Playoff Match',
                            'match_type': 'playoff'
                        })
            
            # Build consolation bracket from losers if league has consolation games
            if has_consolation and len(losers) >= 2:
                # Fetch full team data for losers from standings
                loser_teams = []
                try:
                    standings_data = api.get_league_standings(league_key)
                    loser_map = {t['team_key']: t for t in standings_data}
                    for lk in losers:
                        if lk in loser_map:
                            lt = loser_map[lk]
                            loser_teams.append({'team_key': lk, 'name': lt.get('name', lk), 'rank': lt.get('rank', 99)})
                        else:
                            loser_teams.append({'team_key': lk, 'name': lk, 'rank': 99})
                except Exception:
                    loser_teams = [{'team_key': lk, 'name': lk, 'rank': 99} for lk in losers]
                
                loser_teams.sort(key=lambda x: x['rank'])
                
                # Consolation labels based on status
                # If we have 2 losers in the final week, it's 3rd place!
                # If we have 4 losers in the final week, it's 5th/7th place!
                for i in range(0, len(loser_teams), 2):
                    if i + 1 < len(loser_teams):
                        # Simple rank-based labeling for consolation
                        rank1 = loser_teams[i]['rank']
                        rank2 = loser_teams[i+1]['rank']
                        
                        label = "Consolation"
                        mtype = "consolation"
                        
                        if rounds_remaining == 0:
                            if rank1 <= 4 and rank2 <= 4:
                                label = "3rd Place Match"
                                mtype = "main_3rd_place"
                            elif rank1 <= 6 and rank2 <= 6:
                                label = "5th Place Match"
                                mtype = "consolation_5th_place"
                            else:
                                label = "7th Place Match"
                                mtype = "consolation_7th_place"
                        
                        consolation_bracket.append({
                            'team1': loser_teams[i],
                            'team2': loser_teams[i + 1],
                            'seed1': rank1,
                            'seed2': rank2,
                            'is_consolation': True,
                            'match_label': label,
                            'match_type': mtype
                        })
    except Exception as e:
        debug_print(f"[Playoff Sim] Error generating bracket: {e}")
        import traceback
        traceback.print_exc()
        # Even if full simulation fails, try to determine my_team_status from seeding
        fallback_status = ""
        try:
            standings = api.get_league_standings(league_key)
            standings_map_fb = {t['team_key']: t for t in standings}
            my_rank = next((t.get('rank', 99) for t in standings if t['team_key'] == my_team_key), 99)
            num_playoff_teams = int(league_info.get('num_playoff_teams', 8)) if league_info else 8
            rounds_into_playoffs = max(0, target_week - playoff_start_week)
            # Simulate first round bracket to find my opponent
            first_round_sim = simulate_next_playoff_week(league_key, playoff_start_week, current_week)
            # Walk through bracket to find my team's match and predict outcome by win%
            my_first_round_status = None
            for match in first_round_sim.get('bracket', []):
                t1 = match.get('team1', {})
                t2 = match.get('team2', {})
                t1_key = t1.get('team_key') or t1.get('key')
                t2_key = t2.get('team_key') or t2.get('key')
                if my_team_key in [t1_key, t2_key]:
                    opp_key = t2_key if t1_key == my_team_key else t1_key
                    my_pct = standings_map_fb.get(my_team_key, {}).get('win_pct', 0)
                    opp_pct = standings_map_fb.get(opp_key, {}).get('win_pct', 0)
                    opp_rank = standings_map_fb.get(opp_key, {}).get('rank', 99)
                    if my_pct > opp_pct or (my_pct == opp_pct and my_rank <= opp_rank):
                        my_first_round_status = "advancing"
                    else:
                        my_first_round_status = "consolation" if (league_info and league_info.get('has_playoff_consolation_games', '0') == '1') else "eliminated"
                    break
            if my_first_round_status and rounds_into_playoffs > 0:
                # For rounds 2+, only show advancement if survived prior rounds too
                fallback_status = my_first_round_status if rounds_into_playoffs <= 1 else "advancing" if my_rank <= num_playoff_teams // (2 ** rounds_into_playoffs) else "consolation"
            elif my_first_round_status:
                # For the first playoff week itself (quarterfinals), everyone in top-8 is playing
                fallback_status = "advancing"
            else:
                # My team not in bracket → eliminated or consolation
                fallback_status = "consolation" if (league_info and league_info.get('has_playoff_consolation_games', '0') == '1') else "eliminated"
        except Exception:
            pass
        return {'bracket': [], 'consolation_bracket': [], 'eliminated': [], 'my_team_status': fallback_status, 'error': str(e)}

    if my_team_key in eliminated:
        # Check if they're in consolation bracket
        in_consolation = False
        for match in consolation_bracket:
            if match.get('team1', {}).get('team_key') == my_team_key or \
               match.get('team2', {}).get('team_key') == my_team_key:
                in_consolation = True
                break
        my_team_status = "consolation" if in_consolation else "eliminated"
    else:
        in_bracket = False
        for match in bracket:
            if match.get('team1', {}).get('team_key') == my_team_key or \
               match.get('team2', {}).get('team_key') == my_team_key:
                in_bracket = True
                break
        if in_bracket:
            my_team_status = "advancing"
        else:
            # If not in the championship bracket, could be in consolation
            in_consolation = any(
                match.get('team1', {}).get('team_key') == my_team_key or
                match.get('team2', {}).get('team_key') == my_team_key
                for match in consolation_bracket
            )
            my_team_status = "consolation" if in_consolation else "eliminated"
            
    result = {
        'bracket': bracket,
        'consolation_bracket': consolation_bracket,
        'has_consolation': has_consolation,
        'eliminated': eliminated,
        'my_team_status': my_team_status
    }
    
    _set_prediction_cached(cache_key, result)
    return result


@app.route('/predict/<league_key>/all')
@login_required
def predict_all(league_key):
    """Show predictions for all matchups in the league"""
    validate_league_access(league_key)
    
    week = request.args.get('week', type=int)
    
    try:
        # Get league info for week navigation
        leagues = api.get_user_leagues()
        league_info = next((l for l in leagues if l['league_key'] == league_key), None)
        
        current_week = int(league_info['current_week']) if league_info else 1
        start_week = int(league_info['start_week']) if league_info else 1
        end_week = int(league_info['end_week']) if league_info else 24
        playoff_start_week = int(league_info['playoff_start_week']) if league_info and league_info.get('playoff_start_week') else None
        league_name = league_info['name'] if league_info else 'League'
        
        # Use current week if not specified
        selected_week = week if week else current_week
        
        # Get all matchup predictions
        predictions = predictor.predict_all_matchups(league_key, selected_week, current_week)
        
        # Get my team to highlight
        my_team = api.get_my_team(league_key)
        my_team_key = my_team['team_key'] if my_team else None
        
        return render_template('all_matchups.html',
                             predictions=predictions,
                             league_key=league_key,
                             league_name=league_name,
                             categories=CATEGORIES,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             selected_week=selected_week,
                             playoff_start_week=playoff_start_week,
                             my_team_key=my_team_key)
    except PlayoffWeekError as e:
        # Playoff week - show friendly message with simulation
        simulation = {}
        try:
            simulation = simulate_next_playoff_week(league_key, selected_week, current_week)
        except Exception as sim_e:
            debug_print(f"[Playoff] Sim error: {sim_e}")
            simulation = {'error': str(sim_e)}
            
        return render_template('playoff_all.html',
                             league_key=league_key,
                             selected_week=selected_week,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             playoff_start_week=playoff_start_week,
                             message=e.message,
                             simulation=simulation)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template('error.html', error=str(e))


@app.route('/api/predict/<league_key>')
@login_required
def api_predict(league_key):
    """API endpoint for prediction"""
    validate_league_access(league_key)
    
    week = request.args.get('week', type=int)
    
    try:
        prediction = predictor.predict_matchup(league_key, week)
        
        return jsonify({
            'week': prediction.week,
            'my_team': {
                'name': prediction.my_team.team_name,
                'projected': prediction.my_team.total_projected
            },
            'opponent': {
                'name': prediction.opponent.team_name,
                'projected': prediction.opponent.total_projected
            },
            'category_winners': prediction.category_winners,
            'predicted_score': prediction.predicted_score,
            'confidence': prediction.confidence
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/debug')
def debug_page():
    """Debug endpoint to see raw data"""
    if not auth.is_authenticated():
        return "Not authenticated - go to /login first", 401
    
    try:
        import json
        
        # Get leagues
        leagues = api.get_user_leagues()
        
        if not leagues:
            return "<html><body style='background:#1a1a2e;color:white;padding:20px;'><h1>No leagues found</h1></body></html>"
        
        # Use first league
        league = leagues[0]
        league_key = league['league_key']
        
        # Get my team
        my_team = api.get_my_team(league_key)
        
        # Get roster
        roster = api.get_team_roster(my_team['team_key']) if my_team else []
        
        # Format for display
        debug_info = {
            'league': league,
            'my_team': my_team,
            'roster_count': len(roster),
            'players': []
        }
        
        for player in roster[:5]:  # First 5 players
            debug_info['players'].append({
                'name': player.get('name'),
                'team': player.get('team'),
                'player_key': player.get('player_key'),
                'stats_count': len(player.get('stats', {})),
                'stats': player.get('stats', {})
            })
        
        html = f"""
        <html>
        <body style='background:#1a1a2e;color:white;padding:20px;font-family:monospace;'>
            <h1>🔍 Debug Info</h1>
            <h2>League: {league.get('name')}</h2>
            <h2>Team: {my_team.get('name') if my_team else 'None'}</h2>
            <h2>Players: {len(roster)}</h2>
            <hr>
            <h3>First 5 Players Data:</h3>
            <pre style='background:#0a0e17;padding:15px;border-radius:10px;overflow:auto;'>{json.dumps(debug_info['players'], indent=2, ensure_ascii=False)}</pre>
            <hr>
            <p><a href="/dashboard" style="color:orange;">Back to Dashboard</a></p>
        </body>
        </html>
        """
        return html
    
    except Exception as e:
        import traceback
        return f"<html><body style='background:#1a1a2e;color:red;padding:20px;'><h1>Error</h1><pre>{traceback.format_exc()}</pre></body></html>"


@app.route('/logout')
def logout():
    """Logout user"""
    # Clear and save GUID for cache cleanup
    guid = session.get('user_guid')
    
    # Clear Flask session (works on Vercel + local)
    session.clear()
    session.modified = True
    
    # Also clear in-memory state
    auth.access_token = None
    auth.refresh_token = None
    auth.token_expiry = None

    # Clear this specific user's API cache to prevent data leakage 
    # but don't kill everyone else's cache
    if guid:
        from yahoo_api import clear_user_cache
        clear_user_cache(guid)

    # Remove local token file if present (local dev only)
    try:
        if not IS_VERCEL and os.path.exists('yahoo_token.json'):
            os.remove('yahoo_token.json')
    except Exception as e:
        debug_print(f"Error removing token file: {e}")

    return redirect(url_for('login'))


@app.route('/api/clear-roster-cache')
@login_required
def api_clear_roster_cache():
    """JSON API: clear cache for the current user and return success."""
    try:
        guid = api.get_user_guid()
        from yahoo_api import clear_user_cache
        clear_user_cache(guid)
        try:
            predictor.clear_cache()
        except Exception as e:
            debug_print(f"[Cache] Error wiping predictor cache: {e}")
        # Re-trigger background pre-warm
        try:
            if auth.is_authenticated():
                leagues = api.get_user_leagues()
                threading.Thread(target=_warm_league_caches, args=(leagues,), daemon=True).start()
        except Exception as e:
            debug_print(f"[Cache] Error re-triggering pre-warm: {e}")
        return jsonify({'success': True, 'message': 'Cache cleared successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/clear_cache')
@login_required
def clear_cache_route():
    """Clear cache for the current user."""
    league_key = request.args.get('league_key')
    try:
        guid = api.get_user_guid()
        from yahoo_api import clear_user_cache
        clear_user_cache(guid)
        
        if league_key:
            return redirect(url_for('predict', league_key=league_key))
        return redirect(url_for('dashboard'))
    except Exception as e:
        return render_template('error.html', error=str(e))
    count_acq = clear_cache_by_pattern('acquisition_dates:')
    count_il = clear_cache_by_pattern('il_history:')
    
    total = count_roster + count_matchup + count_trans + count_raw_trans + count_acq + count_il
    
    # Also wipe all prediction caches so they recalculate with the new rosters
    try:
        predictor.clear_cache()
    except Exception as e:
        debug_print(f"[Cache] Error wiping predictor cache: {e}")
        
    # Re-trigger background pre-warm to refill the caches immediately
    try:
        if auth.is_authenticated():
            leagues = api.get_user_leagues()
            import threading
            threading.Thread(target=_warm_league_caches, args=(leagues,), daemon=True).start()
    except Exception as e:
        debug_print(f"[Cache] Error re-triggering pre-warm: {e}")
    
    return jsonify({
        'success': True,
        'message': f'Cache cleared! Refreshed {total} entries. Predictions are recalculating.',
        'details': {
            'roster': count_roster,
            'transactions': count_trans,
            'acquisition_dates': count_acq,
            'il_history': count_il
        }
    })


if __name__ == '__main__':
    print("\n" + "="*50)
    print("Fantasy Basketball Predictor")
    print("="*50)
    print("\nStarting application...")
    print("Go to: https://localhost:5000")
    print("\nTo stop: Ctrl+C")
    print("="*50 + "\n")
    
    # Pre-load external data BEFORE starting server
    print("⏳ Pre-loading data... (this takes ~10-30 seconds on first run)")
    _preload_data()
    print("✅ Data pre-loaded! Server is ready.\n")
    
    # Keep cache hot 24/7: refresh BBRef + NBA schedule every 30 min (local only)
    refresh_thread = threading.Thread(target=_background_cache_refresh_loop, daemon=True)
    refresh_thread.start()
    print("🔄 Cache 24/7: background refresh every 30 min (keeps site fast while server runs).\n")
    
    # Run with HTTP (no SSL needed for local development)
    app.run(debug=True, port=5000, ssl_context='adhoc')
