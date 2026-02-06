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

from yahoo_auth import auth
from yahoo_api import api
from predictor import predictor, PlayoffWeekError
from config import CATEGORIES


def _preload_data():
    """Pre-load external data in background."""
    try:
        # Pre-load Basketball Reference data
        from basketball_reference import fetch_all_nba_season_averages
        print("[Startup] Pre-loading Basketball Reference data...")
        stats = fetch_all_nba_season_averages()
        print(f"[Startup] Pre-loaded {len(stats)} player averages")
        
        # Pre-load NBA schedule data
        from nba_schedule import _fetch_and_cache_full_schedule
        print("[Startup] Pre-loading NBA schedule data...")
        schedule = _fetch_and_cache_full_schedule()
        print(f"[Startup] Pre-loaded schedule for {len(schedule)} dates")
        
    except Exception as e:
        print(f"[Startup] Error pre-loading data: {e}")

app = Flask(__name__)
app.secret_key = os.urandom(24)


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
        return render_template('dashboard.html', leagues=leagues)
    except Exception as e:
        # Token might be expired, try to refresh
        if auth.refresh_access_token():
            try:
                leagues = api.get_user_leagues()
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
        
        prediction = predictor.predict_matchup(league_key, selected_week, current_week)
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
        
        # Use current week if not specified
        selected_week = week if week else current_week
        
        # Get current actual standings from Yahoo
        standings_data = api.get_league_standings(league_key)
        
        # Get my team to highlight
        my_team = api.get_my_team(league_key)
        my_team_key = my_team['team_key'] if my_team else None
        
        # Calculate category records based on selected week
        is_projection = selected_week >= current_week
        
        if selected_week < current_week:
            # Past week: show actual records up to and including selected week
            category_records = api.get_category_records(league_key, selected_week + 1)
        else:
            # Current or future week: show actual records + predictions
            # Get actual records up to current week (before current week started)
            category_records = api.get_category_records(league_key, current_week)
            # Add predictions from current_week to selected_week
            category_records = project_future_category_records(
                league_key, category_records, current_week, selected_week
            )
        
        # Re-rank teams based on projected category records
        if is_projection and category_records:
            standings_data = rerank_standings(standings_data, category_records)
        
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
    
    # Start with current category records
    projected = deepcopy(current_records)
    
    # For each week from current to target, add predicted category results
    for week in range(current_week, target_week + 1):
        try:
            predictions = predictor.predict_all_matchups(league_key, week, current_week)
            
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
                    
        except Exception as e:
            print(f"[DEBUG] Error predicting week {week}: {e}")
            continue
    
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
                             my_team_key=my_team_key)
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
    # Clear token file
    if os.path.exists('yahoo_token.json'):
        os.remove('yahoo_token.json')
    
    # Reset auth state
    auth.access_token = None
    auth.refresh_token = None
    auth.token_expiry = None
    
    return redirect(url_for('login'))


if __name__ == '__main__':
    print("\n" + "="*50)
    print("Fantasy Basketball Predictor")
    print("="*50)
    print("\nStarting application...")
    print("Go to: http://localhost:5000")
    print("\nTo stop: Ctrl+C")
    print("="*50 + "\n")
    
    # Pre-load external data in background
    # This makes the first prediction much faster
    preload_thread = threading.Thread(target=_preload_data, daemon=True)
    preload_thread.start()
    
    # Run with HTTP (no SSL needed for local development)
    app.run(debug=True, port=5000)
