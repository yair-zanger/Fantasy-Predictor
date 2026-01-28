"""
Fantasy Basketball Predictor - Flask Web Application
"""
from flask import Flask, render_template, redirect, url_for, request, jsonify, session
import os
import json

from yahoo_auth import auth
from yahoo_api import api
from predictor import predictor
from config import CATEGORIES

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
        
        # Use current week if not specified
        selected_week = week if week else current_week
        
        prediction = predictor.predict_matchup(league_key, selected_week)
        return render_template('prediction.html', 
                             prediction=prediction,
                             league_key=league_key,
                             categories=CATEGORIES,
                             current_week=current_week,
                             start_week=start_week,
                             end_week=end_week,
                             selected_week=selected_week)
    except Exception as e:
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
    print("🏀 Fantasy Basketball Predictor")
    print("="*50)
    print("\nפותח את האפליקציה...")
    print("לך ל: https://localhost:5000")
    print("\n⚠️  הדפדפן יציג אזהרת אבטחה - זה בסדר!")
    print("   לחץ 'Advanced' ואז 'Proceed to localhost'")
    print("\nלעצירה: Ctrl+C")
    print("="*50 + "\n")
    
    # Run with HTTPS using adhoc SSL certificate
    app.run(debug=True, port=5000, ssl_context='adhoc')
