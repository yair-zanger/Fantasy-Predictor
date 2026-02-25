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

from yahoo_auth import auth
from yahoo_api import api
from predictor import predictor, PlayoffWeekError
from config import CATEGORIES, DEBUG_MODE

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
    Also warm up league caches if the user is authenticated.
    """
    while True:
        time.sleep(10 * 60) # 10 minutes
        try:
            from basketball_reference import fetch_all_nba_season_averages
            from nba_schedule import _fetch_and_cache_full_schedule
            fetch_all_nba_season_averages()
            _fetch_and_cache_full_schedule()
            debug_print("[Cache 24/7] Refreshed BBRef + NBA schedule")
            
            # Also keep Yahoo API data and Matchup predictions warm
            if auth.is_authenticated():
                try:
                    leagues = api.get_user_leagues()
                    debug_print("[Cache 24/7] Triggering background pre-warm for authenticated user...")
                    _warm_league_caches(leagues)
                except Exception as e:
                    debug_print(f"[Cache 24/7] Failed to warm Yahoo caches (token might be expired): {e}")
                    
        except Exception as e:
            debug_print(f"[Cache 24/7] Refresh error: {e}")


app = Flask(__name__)
# Use a stable secret key so sessions survive across serverless cold starts.
# Set FLASK_SECRET_KEY env var in Vercel dashboard.
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))


# Make auth available in all templates
@app.context_processor
def inject_auth():
    return dict(auth=auth)


@app.route('/')
def index():
    """Home page"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
    return redirect(url_for('dashboard'))


@app.route('/login')
def login():
    """Show login page"""
    if auth.is_authenticated():
        return redirect(url_for('dashboard'))
    
    return render_template('login.html')


@app.route('/auth/start')
def auth_start():
    """Start OAuth flow"""
    auth_url = auth.get_auth_url()
    return redirect(auth_url)


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
            return redirect(url_for('dashboard'))
        except Exception as e:
            return render_template('error.html', error=str(e))
    
    return redirect(url_for('login'))


@app.route('/dashboard')
def dashboard():
    """Main dashboard - league selection"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
    try:
        leagues = api.get_user_leagues()
        # Warm cache in background so next pages (predict, standings) are instant
        threading.Thread(target=_warm_league_caches, args=(leagues,), daemon=True).start()
        return render_template('dashboard.html', leagues=leagues)
    except Exception as e:
        # Token might be expired, try to refresh
        if auth.refresh_access_token():
            try:
                leagues = api.get_user_leagues()
                threading.Thread(target=_warm_league_caches, args=(leagues,), daemon=True).start()
                return render_template('dashboard.html', leagues=leagues)
            except Exception as e2:
                return render_template('error.html', error=str(e2))
        return render_template('error.html', error=str(e))


@app.route('/league/<league_key>')
def league_view(league_key):
    """View league details and prediction"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
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
def predict(league_key):
    """Generate and show prediction"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
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
        # Playoff week - show friendly message
        return render_template('playoff.html',
                             league_key=league_key,
                             selected_week=selected_week,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             playoff_start_week=playoff_start_week,
                             message=e.message)
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/standings/<league_key>')
def standings(league_key):
    """Show league standings with week navigation"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
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


@app.route('/predict/<league_key>/all')
def predict_all(league_key):
    """Show predictions for all matchups in the league"""
    if not auth.is_authenticated():
        return redirect(url_for('login'))
    
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
        # Playoff week - show friendly message
        return render_template('playoff_all.html',
                             league_key=league_key,
                             selected_week=selected_week,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             playoff_start_week=playoff_start_week,
                             message=e.message)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template('error.html', error=str(e))


@app.route('/api/predict/<league_key>')
def api_predict(league_key):
    """API endpoint for prediction"""
    if not auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401
    
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
    # Clear Flask session (works on Vercel + local)
    from flask import session
    session.pop('yahoo_token', None)
    session.pop('pkce_code_verifier', None)
    session.modified = True

    # Also clear in-memory state
    auth.access_token = None
    auth.refresh_token = None
    auth.token_expiry = None

    # Remove local token file if present (local dev)
    if os.path.exists('yahoo_token.json'):
        os.remove('yahoo_token.json')

    return redirect(url_for('login'))


@app.route('/api/clear-roster-cache')
def clear_roster_cache():
    """Clear roster and transactions cache to force refresh from Yahoo.
    Useful after making roster changes (add/drop players).
    """
    from yahoo_api import clear_cache_by_pattern
    
    count_roster = clear_cache_by_pattern('roster:')
    count_trans = clear_cache_by_pattern('transactions:')
    count_acq = clear_cache_by_pattern('acquisition_dates:')
    count_il = clear_cache_by_pattern('il_history:')
    
    total = count_roster + count_trans + count_acq + count_il
    
    # Also wipe all prediction caches so they recalculate with the new rosters
    try:
        predictor._prediction_cache.clear()
        debug_print("[Cache] Wiped predictor cache from user request")
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
